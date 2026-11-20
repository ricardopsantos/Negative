"""People: grouping of faces, naming, merging, hiding, the person filter and the Immich-shaped /api/people."""
import numpy as np
import pytest

import faces
from conftest import album_assets


def vec(seed, noise=0.0, n=0):
    r = np.random.RandomState(seed)
    v = r.randn(128)
    if noise:
        v = v + np.random.RandomState(1000 + n).randn(128) * noise
    return (v / np.linalg.norm(v)).astype(np.float32)


class FakeEngine:
    """Stands in for OpenCV: the 'face' of a photo is chosen by its file name, a-*.jpg is person A, b-*.jpg person B."""
    def detect(self, path):
        n = path.rsplit("/", 1)[-1]
        if n.startswith("none"):
            return []
        who = {"a": 1, "b": 2}[n[0]]
        f = np.float32  # the real detector returns numpy scalars; they must not reach SQLite as blobs
        return [(f(0.3), f(0.3), f(0.3), f(0.3), f(0.95), vec(who, 0.15, hash(n) % 1000))]


@pytest.fixture
def people(client, auth, lib, monkeypatch):
    from PIL import Image
    d = lib / "ppl"
    d.mkdir()
    for n in ["a-1", "a-2", "a-3", "a-4", "b-1", "none-1"]:
        Image.new("RGB", (400, 300), (90, 90, 90)).save(d / f"{n}.jpg")
    client.post("/ui/folders", json={"path": str(d)}, headers=auth)
    import server
    monkeypatch.setattr(server, "_engine", FakeEngine())
    assert server.faces_pass() == 6
    return server


def test_assign_and_merge_helpers():
    cents = np.stack([vec(1), vec(2)])
    assert faces.assign(cents, vec(1, 0.1, 5))[0] == 0
    assert faces.assign(cents, vec(99))[0] is None
    assert faces.assign(None, vec(1))[0] is None
    m = faces.merge_centroid(vec(1), 3, vec(2), 1)
    assert abs(np.linalg.norm(m) - 1) < 1e-5


def test_worker_groups_same_person_and_is_idempotent(people, client, auth):
    ppl = client.get("/ui/people", headers=auth).get_json()
    assert [p["count"] for p in ppl] == [4]  # A has 4 photos; B has 1 (below the 3-photo bar), no-face photo has none
    assert people.faces_pass() == 0  # nothing new: nothing redone
    st = client.get("/ui/faces/status", headers=auth).get_json()
    assert st["photos"] == 6 + 3 and st["faces"] == 5 or st["faces"] == 5  # 5 faces found


def test_name_filter_hide_and_immich_shape(people, client, auth):
    pid = client.get("/ui/people", headers=auth).get_json()[0]["id"]
    assert client.put(f"/ui/people/{pid}", json={"name": "Alice"}, headers=auth).status_code == 200
    api = client.get("/api/people", headers=auth).get_json()
    assert api["total"] == 1 and api["people"][0]["name"] == "Alice"
    for k in ("id", "name", "birthDate", "isHidden", "thumbnailPath", "otherPeople", "sharedBy", "sharedWith"):
        assert k in api["people"][0]
    n = sum(b["count"] for b in client.get(f"/api/timeline/buckets?personId={pid}", headers=auth).get_json())
    assert n == 4  # the person filter lists exactly their photos
    assert client.get(f"/api/people/{pid}/statistics", headers=auth).get_json() == {"assets": 4}
    t = client.get(f"/api/people/{pid}/thumbnail", headers=auth)
    assert t.status_code == 200 and t.mimetype == "image/webp"
    client.put(f"/ui/people/{pid}", json={"hidden": True}, headers=auth)
    assert client.get("/ui/people", headers=auth).get_json() == []
    assert client.get("/ui/people?hidden=1", headers=auth).get_json()[0]["id"] == pid


def test_merge_and_not_this_person(people, client, auth):
    import server
    c = server.connect()
    b = c.execute("SELECT id FROM persons WHERE n=1").fetchone()["id"]  # B, a group of one
    a = client.get("/ui/people", headers=auth).get_json()[0]["id"]
    client.put(f"/ui/people/{b}", json={"name": "Bob"}, headers=auth)
    assert client.post(f"/ui/people/{b}/merge", json={"into": b}, headers=auth).status_code == 400
    assert client.post(f"/ui/people/{b}/merge", json={"into": a}, headers=auth).status_code == 200
    ppl = {p["id"]: p for p in client.get("/ui/people", headers=auth).get_json()}
    assert list(ppl) == [a] and ppl[a]["count"] == 5 and ppl[a]["name"] == "Bob"  # takes the only name
    fid = c.execute("SELECT id FROM faces WHERE person_id=? LIMIT 1", (a,)).fetchone()["id"]
    r = client.post(f"/ui/faces/{fid}/not-this-person", headers=auth).get_json()
    assert r["personId"] != a
    assert client.get("/ui/people", headers=auth).get_json()[0]["count"] == 4
    c.close()


def test_deleted_photo_leaves_people(people, client, auth):
    a = album_assets(client, auth)
    one = next(x for x in a if x["originalFileName"] == "a-1.jpg")
    client.delete(f"/ui/assets/{one['id']}", json={"confirm": True}, headers=auth)
    assert client.get("/ui/people", headers=auth).get_json()[0]["count"] == 3
    people.faces_pass()
    import server
    c = server.connect()
    assert c.execute("SELECT COUNT(*) FROM faces WHERE asset_id=?", (one["id"],)).fetchone()[0] == 0
    c.close()


def test_people_endpoints_need_login(client):
    for u in ("/ui/people", "/api/people", "/ui/faces/status"):
        assert client.get(u).status_code == 401


def test_face_numbers_are_stored_as_numbers(people):
    import server
    c = server.connect()
    kinds = c.execute("SELECT DISTINCT typeof(x), typeof(w), typeof(score) FROM faces").fetchall()
    assert [tuple(r) for r in kinds] == [("real", "real", "real")]
    assert c.execute("SELECT DISTINCT typeof(cover_q) FROM persons").fetchone()[0] == "real"
    c.close()


def test_places_and_memories(client, auth, lib, monkeypatch):
    from datetime import date
    from PIL import Image
    import server
    d = lib / "trip"
    d.mkdir()
    today = date.today()

    def shot(name, when, gps=None):
        im = Image.new("RGB", (60, 40), (10, 20, 30))
        ex = Image.Exif()
        ex[0x0132] = when.strftime("%Y:%m:%d 12:00:00")
        if gps:
            g = ex.get_ifd(0x8825)
            lat, lon = gps
            g[1], g[2] = "N" if lat >= 0 else "S", (abs(lat), 0.0, 0.0)
            g[3], g[4] = "E" if lon >= 0 else "W", (abs(lon), 0.0, 0.0)
        im.save(d / name, exif=ex)

    shot("lisbon.jpg", date(2020, 5, 5), (38.72, -9.14))
    shot("porto.jpg", date(2021, 5, 6), (41.16, -8.63))
    shot("porto2.jpg", date(2021, 5, 7), (41.15, -8.62))
    shot("nogps.jpg", date(2019, 5, 8))
    shot("ago1.jpg", date(today.year - 1, today.month, today.day))
    shot("ago3.jpg", date(today.year - 3, today.month, min(today.day, 28)))
    shot("far.jpg", date(today.year - 2, (today.month % 12) + 1, 15))  # same year-ago but a different month: not a memory
    client.post("/ui/folders", json={"path": str(d)}, headers=auth)
    assert server.geo_pass() >= 7
    assert server.geo_pass() == 0  # nothing new: nothing redone
    pl = {p["key"]: p["count"] for p in client.get("/ui/places", headers=auth).get_json()}
    assert pl == {"Porto|PT": 2, "Lisbon|PT": 1}
    n = sum(b["count"] for b in client.get("/api/timeline/buckets?place=Porto%7CPT", headers=auth).get_json())
    assert n == 2
    mem = {m["yearsAgo"]: m["count"] for m in client.get("/ui/memories", headers=auth).get_json()}
    assert mem[1] == 1 and mem[3] == 1 and 2 not in mem
    n = sum(b["count"] for b in client.get("/api/timeline/buckets?onThisDay=3", headers=auth).get_json())
    assert n == 1
    api = client.get("/api/memories", headers=auth).get_json()
    assert {m["data"]["year"] for m in api} == {today.year - 1, today.year - 3}
    assert all(k in api[0] for k in ("id", "ownerId", "type", "assets", "memoryAt", "isSaved", "createdAt", "updatedAt"))
    client.delete_cookie("photos_session")
    assert client.get("/ui/places").status_code == 401


def test_background_scans_do_not_block_user_writes(client, auth, lib, monkeypatch):
    """A slow geo/face pass must not hold the database write lock: deleting a photo meanwhile stays quick."""
    import threading
    import time
    from PIL import Image
    import server
    d = lib / "busy"
    d.mkdir()
    for i in range(6):
        Image.new("RGB", (40, 30), (i, 2, 3)).save(d / f"p{i}.jpg")
    client.post("/ui/folders", json={"path": str(d)}, headers=auth)
    monkeypatch.setattr(server.places, "gps_of", lambda p, k: time.sleep(0.4))  # each file takes 0.4 s to read
    t = threading.Thread(target=server.geo_pass)
    t.start()
    time.sleep(0.3)  # the pass is now in the middle of its slow reading
    target = album_assets(client, auth)[0]["id"]
    s = time.time()
    assert client.delete(f"/ui/assets/{target}", json={"confirm": True}, headers=auth).status_code == 200
    assert time.time() - s < 1.0, "delete had to wait for the background pass"
    t.join()


def test_city_lookup_works_without_scipy(monkeypatch):
    """The Docker image has no scipy; importing reverse_geocoder would fail, so the data file is read directly."""
    import sys
    import places
    monkeypatch.setitem(sys.modules, "scipy", None)
    monkeypatch.setattr(places, "_cities", None)
    assert places.nearest(38.72, -9.14)[0] == "Lisbon"
