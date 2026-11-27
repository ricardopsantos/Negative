"""PH101-PH105 — deep dive round 2 (2026-11-24)."""
import time
import uuid
from pathlib import Path

from conftest import album_assets
from test_people import FakeEngine, people  # noqa: F401  (fixture)


def _pick(client, auth, lib, sub="Trip 2019"):
    client.post("/ui/folders", json={"path": str(lib / sub)}, headers=auth)
    alb = [a for a in client.get("/api/albums", headers=auth).get_json() if a["albumName"] == sub][0]
    return alb, album_assets(client, auth, alb["id"])


def test_ph101_library_queries_do_not_call_python_per_row(client, auth, lib, monkeypatch):
    import server
    _pick(client, auth, lib)
    c = server.connect()
    fid = c.execute("SELECT id FROM folders").fetchone()[0]
    rows = [(str(uuid.uuid4()), fid, f"{lib}/Trip 2019/x/{i}.jpg", f"{i}.jpg", "IMAGE", "2020-01-01T00:00:00.000Z",
             1.0, 1, 1, 1, server.INDEX_VER, None, "exif", 1.0) for i in range(2000)]
    server.write_rows(c, rows)
    c.commit()
    calls = []
    real = server._upload_prefix
    monkeypatch.setattr(server, "_upload_prefix", lambda: calls.append(1) or real())
    c2 = server.connect()  # the SQL functions look _upload_prefix up at call time
    n = c2.execute(f"SELECT COUNT(*) FROM assets WHERE {server.NOT_LIVE_VIDEO}").fetchone()[0]
    c2.close()
    c.close()
    assert n >= 2000 and len(calls) < 10  # once per statement, not once per row
    assert client.get("/ui/counts", headers=auth).get_json()["all"] == n


def _fake_clip(monkeypatch, server, ids):
    """Smart search without the model: every photo matches, in this order."""
    import numpy as np
    monkeypatch.setattr(server.clipmod, "available", lambda: True)
    monkeypatch.setattr(server, "_clip_matrix", lambda: (ids, np.zeros((len(ids), 2), np.float32)))
    monkeypatch.setattr(server, "_clip", lambda: type("E", (), {"words": lambda self, q: None})())
    monkeypatch.setattr(server.clipmod, "rank", lambda m, q, limit=250: [(i, 1.0 - i / 100) for i in range(len(ids))][:limit])
    server._smart_cache.clear()


def test_ph102_smart_search_never_returns_a_photo_hidden_since_the_last_search(client, auth, lib, monkeypatch):
    import server
    _, assets = _pick(client, auth, lib)
    a, b = assets[0]["id"], assets[1]["id"]
    _fake_clip(monkeypatch, server, [a, b])
    client.post("/ui/hide", json={"id": b, "hidden": True}, headers=auth)
    s = lambda: [x["id"] for x in client.post("/api/search/smart", json={"query": "beach"}, headers=auth).get_json()["assets"]["items"]]
    assert s() == [a]
    client.post("/ui/hide", json={"id": a, "hidden": True}, headers=auth)  # one hidden, one shown again: same hidden count
    client.post("/api/auth/session/unlock", json={"pinCode": "12341234"}, headers=auth)
    client.post("/ui/hide", json={"id": b, "hidden": False}, headers=auth)
    client.post("/api/auth/session/lock", headers=auth)
    assert s() == [b]


def test_ph103_memories_use_the_date_index(client, auth, lib):
    import server
    _pick(client, auth, lib)
    _, days = server._this_day(1, window=2)
    par = getattr(server, "_day_par", lambda d: d)(days)
    with server.app.app_context():
        plan = " ".join(r[3] for r in server.db().execute(
            f"EXPLAIN QUERY PLAN SELECT id FROM assets WHERE {server._day_sql(days)} AND {server.NOT_LIVE_VIDEO} ORDER BY taken LIMIT 200", par))
    assert "SEARCH assets USING INDEX assets_taken (taken>" in plan  # a range on the index, not a scan of every row per year
    c = server.connect()  # the range keeps exactly the days asked for, the first and last day included
    fid = c.execute("SELECT id FROM folders").fetchone()[0]
    from datetime import date, timedelta
    d0, d1 = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    near = {"before": f"{d0 - timedelta(days=1)}T23:59:59.000Z", "first": f"{d0}T00:00:00.000Z",
            "last": f"{d1}T23:59:59.999Z", "after": f"{d1 + timedelta(days=1)}T00:00:00.000Z"}
    rows = [(str(uuid.uuid4()), fid, f"{lib}/Trip 2019/{k}.jpg", f"{k}.jpg", "IMAGE", t, 1.0, 1, 1, 1,
             server.INDEX_VER, None, "exif", 1.0) for k, t in near.items()]
    server.write_rows(c, rows)
    c.commit()
    got = {r[0] for r in c.execute(f"SELECT name FROM assets WHERE {server._day_sql(days)}", server._day_par(days))}
    c.close()
    assert {"first.jpg", "last.jpg"} <= got and not got & {"before.jpg", "after.jpg"}


def test_ph104_favorites_count_matches_the_favorites_view(client, auth, lib):
    from PIL import Image
    Image.new("RGB", (400, 300), (200, 90, 160)).save(lib / "Trip 2019" / "img2.jpg")
    _, assets = _pick(client, auth, lib)
    a, b, c = assets[0], assets[1], assets[2]
    assert client.put("/api/assets", json={"ids": [a["id"], b["id"], c["id"]], "isFavorite": True}, headers=auth).status_code == 204
    client.post("/ui/hide", json={"id": b["id"], "hidden": True}, headers=auth)  # a hidden favorite is not shown...
    Path(c["originalPath"]).unlink()  # ...nor one whose file was removed outside the app
    client.post("/ui/rescan", headers=auth)
    shown = sum(x["count"] for x in client.get("/api/timeline/buckets?isFavorite=true", headers=auth).get_json())
    assert shown == 1
    assert client.get("/ui/counts", headers=auth).get_json()["favorites"] == 1  # the sidebar says what the view shows


def test_ph105_photos_are_never_marked_for_shared_caches(client, auth, lib):
    _, assets = _pick(client, auth, lib)
    a = assets[0]["id"]
    for url in (f"/api/assets/{a}/thumbnail", f"/api/assets/{a}/thumbnail?size=preview",
                f"/api/assets/{a}/thumbnail?size=full", f"/api/assets/{a}/original"):
        r = client.get(url, headers=auth)
        cc = r.headers.get("Cache-Control", "")
        assert r.status_code == 200 and "public" not in cc, (url, cc)
        assert "max-age" not in cc or "private" in cc, (url, cc)  # kept by the browser only


def test_ph106_merge_suggestions_do_not_query_once_per_group(client, auth, people, monkeypatch):
    import faces
    a = next(p["id"] for p in client.get("/ui/people", headers=auth).get_json() if p["count"] == 4)
    c = people.connect()
    b = c.execute("SELECT id FROM persons WHERE id != ? LIMIT 1", (a,)).fetchone()["id"]
    src = c.execute("SELECT * FROM faces WHERE person_id=?", (a,)).fetchall()
    cen = c.execute("SELECT centroid FROM persons WHERE id=?", (a,)).fetchone()[0]
    for k in range(25):  # 25 more unnamed groups, each in enough photos to be listed
        c.execute("INSERT INTO persons(id, centroid, n) VALUES(?,?,?)", (f"extra{k}", cen, len(src)))
        c.executemany("INSERT INTO faces(asset_id,x,y,w,h,score,emb,person_id) VALUES(?,?,?,?,?,?,?,?)",
                      [(f["asset_id"], f["x"], f["y"], f["w"], f["h"], f["score"], f["emb"], f"extra{k}") for f in src])
    c.commit()
    c.close()
    assert len(src) >= faces.MIN_SHOW
    client.put(f"/ui/people/{b}", json={"name": "Bia"}, headers=auth)
    monkeypatch.setattr(people, "SUGGEST_MIN", -1.0)
    seen = []
    real = people.connect

    def traced(*a, **k):
        conn = real(*a, **k)
        conn.set_trace_callback(seen.append)
        return conn
    monkeypatch.setattr(people, "connect", traced)
    sg = client.get("/ui/people/suggestions", headers=auth).get_json()
    assert len(sg) == 12 and {s["intoName"] for s in sg} == {"Bia"}
    assert len([q for q in seen if q.lstrip().upper().startswith("SELECT")]) < 10  # not one per group


def test_ph107_a_new_album_that_fails_leaves_no_folder_behind(client, auth, lib, tmp_path, monkeypatch):
    import server
    client.get("/api/albums", headers=auth)  # writes albums.yml
    yml = tmp_path / "data" / "albums.yml"
    good = yml.read_text()
    yml.write_text("albums: [unclosed\n")  # broken by a hand edit
    r = client.post("/ui/albums", json={"name": "Beach"}, headers=auth)
    assert r.status_code == 409 and "albums.yml" in r.get_json()["message"]
    assert not (lib / "Beach").exists()
    yml.write_text(good)  # fixed: the same name now works, and is not "already exists"
    assert client.post("/ui/albums", json={"name": "Beach"}, headers=auth).status_code == 201
    assert (lib / "Beach").is_dir()

    def full_disk(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(server, "write_albums", full_disk)
    r = client.post("/ui/albums", json={"name": "Snow"}, headers=auth)
    assert r.status_code >= 400 and not (lib / "Snow").exists()


def test_ph108_an_edited_photo_gets_a_new_tile_url(client, auth, lib):
    import os
    alb, assets = _pick(client, auth, lib)
    a = assets[0]
    tb = client.get(f"/api/timeline/buckets?albumId={alb['id']}", headers=auth).get_json()[0]["timeBucket"]
    bucket = lambda: client.get(f"/api/timeline/bucket?albumId={alb['id']}&timeBucket={tb}", headers=auth).get_json()
    b = bucket()
    ver = b["ver"][b["id"].index(a["id"])]
    cc = lambda v: client.get(f"/api/assets/{a['id']}/thumbnail" + (f"?v={v}" if v is not None else ""), headers=auth).headers["Cache-Control"]
    assert "max-age=86400" in cc(f"0.{ver}")  # the timeline's URL names this version: kept a day
    assert "max-age=300" in cc(None) and "max-age=300" in cc("0")  # covers and old-style URLs are checked again soon
    p = Path(a["originalPath"])
    from PIL import Image
    Image.new("RGB", (400, 300), (250, 250, 0)).save(p)  # edited in place, by another program
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))
    client.post("/ui/rescan", headers=auth)
    b = bucket()
    new = b["ver"][b["id"].index(a["id"])]
    assert new != ver and "max-age=300" in cc(f"0.{ver}")  # a new URL for the tile; the old one is not trusted for a day
    js = (Path(__file__).resolve().parent.parent / "app.js").read_text()
    assert "thumbnail?v=${m.rot[k]}.${m.ver[k]}" in js


def test_ph109_odd_json_bodies_are_a_400_not_a_500(client, auth, lib):
    import server
    server.app.config["PROPAGATE_EXCEPTIONS"] = False  # see what a client gets: a crash is a plain 500
    try:
        _, assets = _pick(client, auth, lib)
        a = assets[0]["id"]
        for meth, url in (("POST", "/api/auth/login"), ("POST", "/ui/hide"), ("POST", "/ui/favorite"), ("POST", "/ui/rotate"),
                          ("PUT", "/api/assets"), ("POST", "/api/sync/stream"), ("POST", "/api/sync/ack"),
                          ("POST", "/api/search/metadata"), ("POST", "/api/search/smart"), ("POST", "/ui/albums"),
                          ("DELETE", f"/ui/assets/{a}"), ("POST", "/ui/assets/delete"), ("POST", "/ui/trash/restore")):
            for body in ("x", 1, [1, 2], True):
                r = client.open(url, method=meth, json=body, headers=auth)
                assert r.status_code == 400, (meth, url, body, r.status_code)
        assert server.app.test_client().post("/ui/hide", json="x").status_code == 401  # no login (no cookie): that comes first
        for body in ({"path": 5}, {"path": None}, {"path": ["a"]}):
            assert client.post("/ui/folders", json=body, headers=auth).status_code == 400
        r = client.post("/api/search/metadata", json={"personIds": 5}, headers=auth)
        assert r.status_code == 200 and r.get_json()["assets"]["total"] >= 2  # a bad filter is ignored, like an unknown one
        assert client.post("/ui/hide", json={"id": a, "hidden": True}, headers=auth).status_code == 200  # objects still work
    finally:
        server.app.config["PROPAGATE_EXCEPTIONS"] = None


def test_ph110_far_back_memories_and_nul_paths_are_not_a_500(client, auth, lib):
    import server
    from datetime import date
    server.app.config["PROPAGATE_EXCEPTIONS"] = False
    try:
        _pick(client, auth, lib)
        y = date.today().year
        for ago in (y, y - 1, 99999999):  # year 0, year 1 (its window steps before 0001-01-01), far past
            for url in (f"/api/timeline/buckets?onThisDay={ago}", f"/api/timeline/bucket?onThisDay={ago}&timeBucket=2019-01-01",
                        f"/ui/formats?onThisDay={ago}"):
                r = client.get(url, headers=auth)
                assert r.status_code == 200, (url, r.status_code)
            assert client.get(f"/api/timeline/buckets?onThisDay={ago}", headers=auth).get_json() == []  # nothing that old
        assert client.get("/api/timeline/buckets?onThisDay=1", headers=auth).status_code == 200
        assert client.get("/ui/tree?path=%00", headers=auth).status_code == 400
        assert client.post("/ui/folders", json={"path": str(lib) + "\0x"}, headers=auth).status_code == 400
        assert client.get("/ui/tree", headers=auth).status_code == 200
    finally:
        server.app.config["PROPAGATE_EXCEPTIONS"] = None
