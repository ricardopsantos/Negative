"""PH111-PH115 — deep dive round 4 (2026-11-25)."""
import os
import time
from pathlib import Path

from conftest import album_assets


def _fake_ffmpeg(tmp_path, monkeypatch, body):
    """A stand-in ffmpeg/ffprobe on PATH: `body` runs with $last = the last argument (the output file)."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    for name in ("ffmpeg", "ffprobe"):
        f = b / name
        f.write_text("#!/bin/sh\nfor a; do last=$a; done\n" + (body if name == "ffmpeg" else 'echo \'{"streams":[{"codec_type":"video"}],"format":{"duration":"1"}}\'\n'))
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")


def test_ph111_a_video_hidden_while_it_converts_gives_a_hidden_copy(client, auth, lib, tmp_path, monkeypatch):
    d = lib / "vids"
    d.mkdir()
    (d / "clip.mov").write_bytes(b"\0\0\0\x14ftypqt  " + b"\0" * 64)
    go = tmp_path / "go"
    _fake_ffmpeg(tmp_path, monkeypatch, f'while [ ! -e "{go}" ]; do sleep 0.05; done\nprintf mp4 > "$last"\n')
    client.post("/ui/folders", json={"path": str(d)}, headers=auth)
    a = next(x for x in album_assets(client, auth) if x["originalFileName"] == "clip.mov")
    r = client.post("/ui/convert", json={"id": a["id"], "op": "vid_mp4"}, headers=auth)
    assert r.status_code == 202
    jid = r.get_json()["job"]
    assert client.post("/ui/hide", json={"id": a["id"], "hidden": True}, headers=auth).status_code == 200  # while it runs
    client.post("/api/auth/session/lock", headers=auth)
    go.write_text("")
    for _ in range(200):
        job = client.get(f"/ui/convert/{jid}", headers=auth).get_json()
        if job["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert job["state"] == "done", job
    assert (d / "clip.mp4").exists()
    shown = {x["originalFileName"] for x in album_assets(client, auth)}
    assert "clip.mp4" not in shown and "clip.mov" not in shown  # the copy of a hidden video is hidden too
    assert client.get(f"/api/assets/{job['assetId']}", headers=auth).status_code == 403


def test_ph112_a_new_pin_locks_every_other_unlocked_session(client, auth, lib):
    import server
    client.post("/ui/folders", json={"path": str(lib / "Trip 2019")}, headers=auth)
    a = album_assets(client, auth)[0]["id"]
    client.post("/ui/hide", json={"id": a, "hidden": True}, headers=auth)
    other = server.app.test_client()  # a second device (its own cookie jar)
    tok = other.post("/api/auth/login", json={"email": "owner@example.com", "password": "s3cret"}).get_json()["accessToken"]
    them = {"Authorization": "Bearer " + tok}
    for c, h in ((client, auth), (other, them)):
        assert c.post("/api/auth/session/unlock", json={"pinCode": "12341234"}, headers=h).status_code == 204
        assert c.get(f"/api/assets/{a}", headers=h).status_code == 200
    # someone else knew the PIN: it is changed here...
    assert client.put("/api/auth/pin-code", json={"pinCode": "12341234", "newPinCode": "908172"}, headers=auth).status_code == 204
    assert other.get(f"/api/assets/{a}", headers=them).status_code == 403  # ...and their open session is locked at once
    assert client.get(f"/api/assets/{a}", headers=auth).status_code == 200  # the one that changed it stays open
    assert other.post("/api/auth/session/unlock", json={"pinCode": "12341234"}, headers=them).status_code == 400
    assert other.post("/api/auth/session/unlock", json={"pinCode": "908172"}, headers=them).status_code == 204
    # going back to the default PIN locks the others too
    assert client.delete("/api/auth/pin-code", json={"pinCode": "908172"}, headers=auth).status_code == 204
    assert other.get(f"/api/assets/{a}", headers=them).status_code == 403


def _path_of(asset_id):
    import server
    with server.app.app_context():
        return server.db().execute("SELECT path FROM assets WHERE id=?", (asset_id,)).fetchone()[0]


def test_ph113_favorite_and_hide_work_while_the_original_is_missing(client, auth, lib):
    import json
    import server
    client.post("/ui/folders", json={"path": str(lib / "Trip 2019")}, headers=auth)
    a, b = (x["id"] for x in album_assets(client, auth)[:2])
    pa, pb = _path_of(a), _path_of(b)
    os.rename(pa, pa + ".away")  # the disk is unplugged: the library still shows (PH93)
    os.rename(pb, pb + ".away")
    assert client.post("/ui/favorite", json={"id": a, "favorite": True}, headers=auth).status_code == 200
    assert client.get(f"/api/assets/{a}", headers=auth).get_json()["isFavorite"] is True
    assert client.post("/ui/hide", json={"id": b, "hidden": True}, headers=auth).status_code == 200
    assert client.get(f"/api/assets/{b}", headers=auth).status_code == 403  # hidden at once, not a 500 leaving it shown
    hid = json.loads((server.data_dir() / "hidden.json").read_text())
    assert [v["path"] for v in hid.values()] == [pb]  # durable too: a rescan keeps it hidden
    assert client.put("/api/assets", json={"ids": [a], "isFavorite": False}, headers=auth).status_code == 204


def test_ph114_odd_nested_fields_are_a_400_not_a_500(client, auth):
    raw = lambda url, body: client.post(url, data=body, content_type="application/json", headers=auth).status_code
    for bad in ([1], "ab", 5, None, {"a": 1}):
        assert client.post("/api/assets/bulk-upload-check", json={"assets": bad}, headers=auth).status_code == 400
    for bad in (1, None, True):
        assert client.post("/api/sync/stream", json={"types": bad}, headers=auth).status_code == 400
        assert client.post("/api/sync/ack", json={"acks": bad}, headers=auth).status_code == 400
    assert client.post("/api/assets/bulk-upload-check", json={"assets": [{"id": "x"}]}, headers=auth).get_json()["results"][0]["id"] == "x"
    r = client.post("/api/search/metadata", json={"page": 1e308}, headers=auth)
    assert r.status_code == 200 and r.get_json()["assets"]["items"] == []  # past the end: empty, not an OFFSET overflow
    assert raw("/api/search/metadata", '{"page": 1e999}') == 400  # JSON 1e999 is inf: int() raised OverflowError
    assert raw("/api/search/smart", '{"size": 1e999}') == 400
    assert raw("/ui/people/hide-unnamed", '{"below": 1e999, "confirm": true}') == 400


def test_ph115_a_bulk_mark_writes_the_file_once_and_hashes_in_the_background(client, auth, lib, monkeypatch):
    import json
    import server
    from PIL import Image
    Image.new("RGB", (400, 300), (7, 7, 7)).save(lib / "Home/Kids/img0.jpg")  # not a byte copy of Trip 2019/img0.jpg
    for f in ("Trip 2019", "Home/Kids"):
        client.post("/ui/folders", json={"path": str(lib / f)}, headers=auth)
    ids = [x["id"] for x in album_assets(client, auth)]
    assert len(ids) == 3
    saves, later = [], []
    real_save, real_later = server.fav_save, server._hash_later
    monkeypatch.setattr(server, "fav_save", lambda d, name="favorites.json": (saves.append(name), real_save(d, name)))
    monkeypatch.setattr(server, "_hash_later", lambda name, items: later.extend(items))
    monkeypatch.setattr(server, "HASH_INLINE_MAX", os.path.getsize(_path_of(ids[0])))  # room for one file inline
    assert client.put("/api/assets", json={"ids": ids + ids[:1], "isFavorite": True}, headers=auth).status_code == 204
    assert saves == ["favorites.json"]  # once for the whole list, not once per id
    favs = json.loads((server.data_dir() / "favorites.json").read_text())
    assert len(favs) == 3 and sum(k.startswith("pending:") for k in favs) == 2 == len(later)
    real_later("favorites.json", later)  # the background pass gives them their content hashes
    favs = json.loads((server.data_dir() / "favorites.json").read_text())
    assert len(favs) == 3 and not any(k.startswith("pending:") for k in favs)
    saves.clear()
    assert client.put("/api/assets", json={"ids": ids, "visibility": "locked"}, headers=auth).status_code == 204
    assert saves == ["hidden.json"]
    assert all(client.get(f"/api/assets/{i}", headers=auth).status_code == 403 for i in ids)


def test_ph116_marks_survive_copies_missing_files_and_come_back_hashed(client, auth, lib):
    import json
    import server
    for f in ("Trip 2019", "Home/Kids"):
        client.post("/ui/folders", json={"path": str(lib / f)}, headers=auth)
    by = {_path_of(x["id"]): x["id"] for x in album_assets(client, auth)}
    t0, t1, k0 = (str(lib / p) for p in ("Trip 2019/img0.jpg", "Trip 2019/img1.jpg", "Home/Kids/img0.jpg"))
    assert open(t0, "rb").read() == open(k0, "rb").read()  # a byte copy in another album
    for p in (t0, k0):
        assert client.post("/ui/favorite", json={"id": by[p], "favorite": True}, headers=auth).status_code == 200
    assert all(client.get(f"/api/assets/{by[p]}", headers=auth).get_json()["isFavorite"] for p in (t0, k0))  # the 2nd kept the 1st
    favs = lambda: json.loads((server.data_dir() / "favorites.json").read_text())
    os.rename(t1, t1 + ".away")
    client.post("/ui/favorite", json={"id": by[t1], "favorite": True}, headers=auth)  # missing: pending (PH113)
    assert sum(k.startswith("pending:") for k in favs()) == 1
    d = favs()
    d["deadbeef"] = dict(path=str(lib / "moved.jpg"), name="moved.jpg", size=os.path.getsize(t0))  # lost: relink hunts it
    server.fav_save(d)
    os.rename(t0, t0 + ".away")  # a same-size candidate that is missing right now
    os.rename(t1 + ".away", t1)  # the pending file is back
    c = server.connect()
    try:
        server.fav_relink(c)  # raised FileNotFoundError on the missing candidate
    finally:
        c.close()
    assert not any(k.startswith("pending:") for k in favs())  # hashed now that it is back
    assert {v["path"] for v in favs().values()} >= {t0, t1, k0}
