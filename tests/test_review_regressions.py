"""Review PoCs: each test asserts the CORRECT behaviour, so a failure = the bug is real."""
import io, json, time, sqlite3, uuid, threading
from datetime import date
from pathlib import Path
from PIL import Image
import pytest
import server

def ids(client, auth, q=""):
    out = []
    for b in client.get("/api/timeline/buckets?" + q, headers=auth).get_json():
        out += client.get(f"/api/timeline/bucket?{q}&timeBucket={b['timeBucket']}", headers=auth).get_json()["id"]
    return out

@pytest.fixture
def setup(client, auth, lib):
    r = client.post("/ui/folders", headers=auth, json={"path": str(lib / "Trip 2019")})
    assert r.status_code == 202
    return lib / "Trip 2019"

def aid(p): return str(uuid.uuid5(server.NS, "file:" + str(p)))
def unlock(client, auth): assert client.post("/api/auth/session/unlock", headers=auth, json={"pinCode": "12341234"}).status_code == 204
def lock(client, auth): client.post("/api/auth/session/lock", headers=auth)

def test_hidden_live_clip_locked(client, auth, lib):
    d = lib / "Trip 2019"; (d / "img0.mov").write_bytes(b"\0" * 1000)
    client.post("/ui/folders", headers=auth, json={"path": str(d)})
    still, clip = aid(d / "img0.jpg"), aid(d / "img0.mov")
    assert client.post("/ui/hide", headers=auth, json={"id": still, "hidden": True}).status_code == 200
    lock(client, auth)
    assert client.get(f"/api/assets/{still}", headers=auth).status_code == 403
    assert client.get(f"/api/assets/{clip}/original", headers=auth).status_code == 403

def test_hidden_face_crop_locked(client, auth, setup):
    a = aid(setup / "img0.jpg")
    with server.app.app_context():
        c = server.connect(); cur = c.execute("INSERT INTO faces(asset_id,x,y,w,h,score,emb) VALUES(?,?,?,?,?,?,?)", (a, .1, .1, .5, .5, .9, b"\0"*512)); c.commit(); fid = cur.lastrowid
    client.post("/ui/hide", headers=auth, json={"id": a, "hidden": True}); lock(client, auth)
    assert client.get(f"/ui/faces/{fid}/crop", headers=auth).status_code == 403

def test_hidden_in_trash_locked(client, auth, setup):
    a = aid(setup / "img0.jpg")
    client.post("/ui/hide", headers=auth, json={"id": a, "hidden": True}); unlock(client, auth)
    assert client.delete(f"/ui/assets/{a}", headers=auth, json={"confirm": True}).status_code == 200
    lock(client, auth)
    names = [x["name"] for x in client.get("/ui/trash", headers=auth).get_json()]
    assert "img0.jpg" not in names and client.get(f"/ui/trash/{a}/thumbnail", headers=auth).status_code == 403

def test_convert_of_hidden_stays_hidden(client, auth, setup):
    a = aid(setup / "img0.jpg")
    client.post("/ui/hide", headers=auth, json={"id": a, "hidden": True}); unlock(client, auth)
    r = client.post("/ui/convert", headers=auth, json={"id": a, "op": "img_pct", "percent": 50}); assert r.status_code == 201
    assert r.get_json()["assetId"] not in ids(client, auth)

def test_memories_new_year():
    yr, days = server._this_day(1, today=date(2026, 1, 1))
    assert not (yr == "2025" and "12-30" in days), "Dec 30 matched in year 2025, i.e. ~12 months away"

def test_sync_fp_detects_hidden_swap(client, auth, setup):
    a, b = aid(setup / "img0.jpg"), aid(setup / "img1.jpg")
    client.post("/ui/hide", headers=auth, json={"id": a, "hidden": True})
    with server.app.app_context(): f1 = server._sync_fp(server.connect())
    unlock(client, auth); client.post("/ui/hide", headers=auth, json={"id": a, "hidden": False}); client.post("/ui/hide", headers=auth, json={"id": b, "hidden": True})
    with server.app.app_context(): f2 = server._sync_fp(server.connect())
    assert f1 != f2

def test_tiff_resize_keeps_exif(client, auth, lib):
    d = lib / "Trip 2019"; im = Image.new("RGB", (200, 100)); ex = Image.Exif(); ex[0x010F] = "Canon"; ex[0x0132] = "2019:01:02 03:04:05"
    im.save(d / "t.tif", exif=ex.tobytes())
    client.post("/ui/folders", headers=auth, json={"path": str(d)})
    r = client.post("/ui/convert", headers=auth, json={"id": aid(d / "t.tif"), "op": "img_pct", "percent": 50}); assert r.status_code == 201
    with Image.open(d / r.get_json()["name"]) as o: assert o.getexif().get(0x010F) == "Canon"

def test_batch_delete_partial_on_hidden(client, auth, setup):
    a, b = aid(setup / "img0.jpg"), aid(setup / "img1.jpg")
    client.post("/ui/hide", headers=auth, json={"id": b, "hidden": True}); lock(client, auth)
    r = client.post("/ui/assets/delete", headers=auth, json={"ids": [a, b], "confirm": True})
    assert r.status_code == 200, f"got {r.status_code}; img0 still in library: {(setup/'img0.jpg').exists()}"

def test_favorite_dropped_on_delete(client, auth, setup):
    a = aid(setup / "img0.jpg"); client.post("/ui/favorite", headers=auth, json={"id": a, "favorite": True})
    client.delete(f"/ui/assets/{a}", headers=auth, json={"confirm": True})
    assert not server.fav_load()

def test_token_expires(client, auth):
    with server.app.app_context():
        c = server.connect(); c.execute("UPDATE tokens SET created=0"); c.commit()
    assert client.get("/api/users/me", headers=auth).status_code == 401

def test_thumbnail_follows_file_change(client, auth, setup):
    a = aid(setup / "img0.jpg"); t1 = client.get(f"/api/assets/{a}/thumbnail", headers=auth).data
    time.sleep(0.01); Image.new("RGB", (400, 300), (255, 0, 0)).save(setup / "img0.jpg")
    client.post("/ui/rescan", headers=auth)
    assert client.get(f"/api/assets/{a}/thumbnail", headers=auth).data != t1

def test_trash_thumb_unknown_id(client, auth):
    assert client.get("/ui/trash/nope/thumbnail", headers=auth).status_code == 404

def test_search_bad_size(client, auth):
    assert client.post("/api/search/metadata", headers=auth, json={"size": "x"}).status_code == 400

def test_people_chips_skip_hidden_people(client, auth, setup):
    a = aid(setup / "img0.jpg")
    with server.app.app_context():
        c = server.connect(); c.execute("INSERT INTO persons(id,name,hidden,centroid,n) VALUES('p1','Ex',1,?,1)", (b"\0"*512,))
        c.execute("INSERT INTO faces(asset_id,x,y,w,h,score,emb,person_id) VALUES(?,.1,.1,.3,.3,.9,?,'p1')", (a, b"\0"*512)); c.commit()
    assert all(f["personId"] != "p1" for f in client.get(f"/ui/assets/{a}/faces", headers=auth).get_json())

def test_people_dangling_cover(client, auth, setup):
    a = aid(setup / "img0.jpg")
    with server.app.app_context():
        c = server.connect(); fid = c.execute("INSERT INTO faces(asset_id,x,y,w,h,score,emb,person_id) VALUES(?,.1,.1,.3,.3,.9,?,'p2')", (a, b"\0"*512)).lastrowid
        c.execute("INSERT INTO persons(id,name,centroid,n,cover) VALUES('p2','Bob',?,1,?)", (b"\0"*512, fid)); c.commit()
    client.delete(f"/ui/assets/{a}", headers=auth, json={"confirm": True})
    server.faces_pass(limit=0)
    with server.app.app_context():
        c = server.connect(); r = c.execute("SELECT n, cover FROM persons WHERE id='p2'").fetchone()
        assert r is None or (r[0] == 0 or c.execute("SELECT 1 FROM faces WHERE id=?", (r[1],)).fetchone())

def test_login_race_limit(client):
    codes = []
    def go():
        with server.app.test_client() as c: codes.append(c.post("/api/auth/login", json={"email": "x@y", "password": "bad"}).status_code)
    ts = [threading.Thread(target=go) for _ in range(30)]; [t.start() for t in ts]; [t.join() for t in ts]
    assert codes.count(401) <= 5, f"{codes.count(401)} wrong passwords checked, limit is 5"

def test_unpick_overlap_keeps_child(client, auth, lib):
    client.post("/ui/folders", headers=auth, json={"path": str(lib / "Home")})
    client.post("/ui/folders", headers=auth, json={"path": str(lib / "Home" / "Kids")})
    home = str(uuid.uuid5(server.NS, "dir:" + str((lib / "Home").resolve())))
    client.delete(f"/ui/folders/{home}", headers=auth)
    kids = str(uuid.uuid5(server.NS, "dir:" + str((lib / "Home" / "Kids").resolve())))
    assert client.get(f"/api/albums/{kids}", headers=auth).get_json()["assetCount"] == 1

def test_restore_live_partner_conflict(client, auth, lib):
    d = lib / "Trip 2019"; (d / "img0.mov").write_bytes(b"\0" * 1000)
    client.post("/ui/folders", headers=auth, json={"path": str(d)})
    s = aid(d / "img0.jpg"); client.delete(f"/ui/assets/{s}", headers=auth, json={"confirm": True})
    (d / "img0.mov").write_bytes(b"x")
    r = client.post(f"/ui/trash/{s}/restore", headers=auth)
    assert not ((d / "img0.jpg").exists() and r.status_code == 409), "still restored, 409 returned, clip stranded in trash and not listed"
