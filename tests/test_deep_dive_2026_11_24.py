"""PH96-PH100 — deep dive 2026-11-24: trash keeps both files of one path, ffmpeg cannot deadlock a conversion, phone
playback never waits for a transcode, a missing file gets no empty checksum, face edits on hidden photos need the PIN."""
import json
from pathlib import Path

from PIL import Image

from conftest import album_assets


def _pick(client, auth, lib, sub="Trip 2019"):
    client.post("/ui/folders", json={"path": str(lib / sub)}, headers=auth)
    alb = [a for a in client.get("/api/albums", headers=auth).get_json() if a["albumName"] == sub][0]
    return alb, album_assets(client, auth, alb["id"])


def test_ph96_second_delete_of_the_same_path_keeps_the_first_in_trash(client, auth, lib, tmp_path):
    alb, assets = _pick(client, auth, lib)
    a = assets[0]
    p = Path(a["originalPath"])
    first = p.read_bytes()
    assert client.delete(f"/ui/assets/{a['id']}", json={"confirm": True}, headers=auth).status_code == 200
    Image.new("RGB", (64, 64), (250, 10, 10)).save(p)  # a new file arrives under the same name (same asset id)
    client.post("/ui/rescan", headers=auth)
    assert client.get(f"/api/assets/{a['id']}", headers=auth).status_code == 200
    second = p.read_bytes()
    assert client.delete(f"/ui/assets/{a['id']}", json={"confirm": True}, headers=auth).status_code == 200
    items = client.get("/ui/trash", headers=auth).get_json()
    assert len(items) == 2 and len({i["id"] for i in items}) == 2
    kept = {(tmp_path / "data" / "trash" / i["id"] / p.name).read_bytes() for i in items}
    assert kept == {first, second}  # the first deletion was not overwritten
    for i in items:  # both come back: one under its name, the other as "img0 (1).jpg"
        assert client.post(f"/ui/trash/{i['id']}/restore", json={"rename": True}, headers=auth).status_code == 200
    assert p.exists() and p.with_name(p.stem + " (1)" + p.suffix).exists()


def _fake_ffmpeg(tmp_path, monkeypatch, body):
    """A stand-in ffmpeg on PATH (nice finds it there): `body` runs with $last = the output file."""
    import os
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    f = b / "ffmpeg"
    f.write_text("#!/bin/sh\nfor a; do last=$a; done\n" + body)
    f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")


def test_ph97_a_chatty_ffmpeg_cannot_deadlock_a_video_conversion(tmp_path, monkeypatch):
    import threading
    import convert
    # 300 KB of decode errors on stderr (a damaged video) before any progress on stdout
    _fake_ffmpeg(tmp_path, monkeypatch, "head -c 300000 /dev/zero | tr '\\0' e >&2\necho out_time_us=1000000\nprintf x > \"$last\"\n")
    src = tmp_path / "clip.mov"
    src.write_bytes(b"not really a video")
    got = {}
    t = threading.Thread(target=lambda: got.update(out=convert.convert_video(src, "vid_mp4", {}, tmp_path, lambda n: None)), daemon=True)
    t.start()
    t.join(30)
    assert not t.is_alive(), "convert_video hung on a full stderr pipe"
    assert got["out"].name == "clip.mp4" and got["out"].read_bytes() == b"x"


def test_ph97_ffmpeg_error_text_still_reported(tmp_path, monkeypatch):
    import pytest
    import convert
    _fake_ffmpeg(tmp_path, monkeypatch, "echo 'moov atom not found' >&2\nexit 1\n")
    src = tmp_path / "clip.mov"
    src.write_bytes(b"x")
    with pytest.raises(OSError, match="moov atom not found"):
        convert.convert_video(src, "vid_mp4", {}, tmp_path, lambda n: None)


def test_ph98_phone_playback_never_waits_for_a_whole_encode(client, auth, lib, monkeypatch):
    import shutil
    import subprocess
    import threading
    import time
    import pytest
    import server
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    d = lib / "vids"
    d.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=176x144:rate=10:duration=1",
                    "-c:v", "h263", "-pix_fmt", "yuv420p", str(d / "old.3gp")], check=True)
    client.post("/ui/folders", json={"path": str(d)}, headers=auth)
    a = next(x for x in album_assets(client, auth) if x["originalFileName"] == "old.3gp")
    go = threading.Event()

    def slow(src, out):  # an encode that takes as long as we say
        go.wait(20)
        out.write_bytes(b"\0\0\0\x18ftypmp42" + b"\0" * 64)
    monkeypatch.setattr(server, "_transcode", slow)
    monkeypatch.setattr(server, "PLAYBACK_WAIT", 0.5)
    t0 = time.time()
    r = client.get(f"/api/assets/{a['id']}/video/playback", headers=auth)
    assert r.status_code == 503 and r.headers["Retry-After"] and time.time() - t0 < 5
    go.set()
    for _ in range(40):
        r = client.get(f"/api/assets/{a['id']}/video/playback", headers=auth)
        if r.status_code == 200:
            break
        time.sleep(0.25)
    assert r.status_code == 200 and r.mimetype == "video/mp4" and r.data[4:8] == b"ftyp"


def test_ph98_failed_encode_still_sends_the_original(client, auth, lib, monkeypatch):
    import shutil
    import subprocess
    import pytest
    import server
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    d = lib / "vids"
    d.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=176x144:rate=10:duration=1",
                    "-c:v", "h263", "-pix_fmt", "yuv420p", str(d / "bad.3gp")], check=True)
    client.post("/ui/folders", json={"path": str(d)}, headers=auth)
    a = next(x for x in album_assets(client, auth) if x["originalFileName"] == "bad.3gp")

    def broken(src, out):
        raise OSError("ffmpeg failed: test")
    monkeypatch.setattr(server, "_transcode", broken)
    r = client.get(f"/api/assets/{a['id']}/video/playback", headers=auth)
    assert r.status_code == 200 and r.data == (d / "bad.3gp").read_bytes()


def test_ph99_a_missing_file_gets_no_empty_checksum(client, auth, lib, monkeypatch):
    import server
    server._sha_absent.clear()
    _, assets = _pick(client, auth, lib)
    gone, there = Path(assets[0]["originalPath"]), Path(assets[1]["originalPath"])
    real_isfile = server.os.path.isfile
    monkeypatch.setattr(server.os.path, "isfile", lambda p: False if str(p) == str(gone) else real_isfile(p))  # disk away
    while server.checksum_pass():
        pass
    c = server.connect()
    sha = {r["path"]: r["sha1"] for r in c.execute("SELECT path, sha1 FROM assets")}
    c.close()
    assert sha[str(gone)] is None and sha[str(there)]  # skipped, not marked ''; the others are still hashed
    monkeypatch.setattr(server.os.path, "isfile", real_isfile)  # disk back (after the 10-minute retry wait)
    server._sha_absent.clear()
    server.checksum_pass()
    c = server.connect()
    assert c.execute("SELECT sha1 FROM assets WHERE path=?", (str(gone),)).fetchone()[0]
    c.close()


def test_ph99_unreadable_files_are_not_one_asset_on_the_phone(client, auth, lib):
    import server
    _, assets = _pick(client, auth, lib)
    c = server.connect()
    c.execute("UPDATE assets SET sha1=''")  # two unreadable files: '' is no hash, so not duplicates of each other
    c.commit()
    ok = "1"
    assert server._sync_dupes(c, ok) == {}
    c.close()


def test_ph100_not_this_person_on_a_hidden_photo_needs_the_pin(client, auth, lib):
    import numpy as np
    import server
    _, assets = _pick(client, auth, lib)
    a = assets[0]
    c = server.connect()
    emb = np.ones(128, np.float32) / np.sqrt(128)
    c.execute("INSERT INTO persons(id, centroid, n) VALUES('p1', ?, 1)", (emb.tobytes(),))
    fid = c.execute("INSERT INTO faces(asset_id,x,y,w,h,score,emb,person_id) VALUES(?,?,?,?,?,?,?,?)",
                    (a["id"], .1, .1, .2, .2, .9, emb.tobytes(), "p1")).lastrowid
    c.commit()
    c.close()
    assert client.post("/ui/hide", json={"id": a["id"], "hidden": True}, headers=auth).status_code == 200
    client.post("/api/auth/session/lock", headers=auth)
    assert client.post(f"/ui/faces/{fid}/not-this-person", json={}, headers=auth).status_code == 403
    assert client.post("/api/auth/session/unlock", json={"pinCode": "12341234"}, headers=auth).status_code == 204
    assert client.post(f"/ui/faces/{fid}/not-this-person", json={}, headers=auth).status_code == 200


def test_ph100_every_ffprobe_call_has_the_protocol_whitelist():
    import re
    src = (Path(__file__).resolve().parent.parent / "server.py").read_text()
    calls = re.findall(r'\["ffprobe",[^\]]*', src)
    assert calls and all("FF_SAFE" in x or "protocol_whitelist" in x for x in calls)
