"""Regression tests for the fixes of the 2026-11-06 review (card 20261106-182535-photos-bugs), one per fix
that tests/test_review_regressions.py does not already cover."""
import os
import shutil
import subprocess
import time
import uuid
from datetime import date

import pytest
from PIL import Image

import convert
import server


def aid(p):
    return str(uuid.uuid5(server.NS, "file:" + str(p)))


def pick(client, auth, d):
    assert client.post("/ui/folders", headers=auth, json={"path": str(d)}).status_code == 202


def unlock(client, auth):
    assert client.post("/api/auth/session/unlock", headers=auth, json={"pinCode": "12341234"}).status_code == 204


# --- auth (#5, #7, #41) ---
def test_login_sets_own_cookie_and_old_name_still_reads(client):
    r = client.post("/api/auth/login", json={"email": "owner@example.com", "password": "s3cret"})
    cookies = r.headers.getlist("Set-Cookie")
    assert any(c.startswith("photos_session=") for c in cookies)
    assert not any(c.startswith("immich_access_token=") and "Expires=Thu, 01 Jan 1970" not in c for c in cookies)
    tok = r.get_json()["accessToken"]
    client.delete_cookie("photos_session")
    client.set_cookie("immich_access_token", tok)
    assert client.get("/api/users/me").status_code == 200


def test_session_revoke(client, auth):
    other = client.post("/api/auth/login", json={"email": "owner@example.com", "password": "s3cret"}).get_json()["accessToken"]
    client.delete_cookie("photos_session")
    ss = client.get("/api/sessions", headers=auth).get_json()
    assert len(ss) == 2
    victim = next(s for s in ss if not s["current"])
    assert client.delete(f"/api/sessions/{victim['id']}", headers=auth).status_code == 204
    assert client.get("/api/users/me", headers={"Authorization": "Bearer " + other}).status_code == 401
    assert client.get("/api/users/me", headers=auth).status_code == 200


def test_success_gives_the_attempt_back(client):
    for _ in range(10):  # ten good logins in a row are never rate-limited
        assert client.post("/api/auth/login", json={"email": "owner@example.com", "password": "s3cret"}).status_code == 200


def test_password_guesses_at_pin_endpoints_count_against_login(client, auth):
    for _ in range(5):
        assert client.post("/api/auth/session/unlock", headers=auth, json={"password": "wrong"}).status_code == 400
    r = client.post("/api/auth/login", json={"email": "owner@example.com", "password": "bad"})
    assert r.status_code == 429


# --- headers (#6, #34) ---
def test_security_headers(client):
    h = client.get("/").headers
    assert h["X-Frame-Options"] == "DENY" and h["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]


def test_hidden_thumbnail_not_cached(client, auth, lib):
    d = lib / "Trip 2019"; pick(client, auth, d)
    a = aid(d / "img0.jpg")
    assert "no-store" not in client.get(f"/api/assets/{a}/thumbnail", headers=auth).headers.get("Cache-Control", "")
    client.post("/ui/hide", headers=auth, json={"id": a, "hidden": True}); unlock(client, auth)
    assert client.get(f"/api/assets/{a}/thumbnail", headers=auth).headers["Cache-Control"] == "no-store"


# --- trash and restore keep hidden / favorite (#3, #14) ---
def test_restored_hidden_photo_is_hidden_again(client, auth, lib):
    d = lib / "Trip 2019"; pick(client, auth, d)
    a = aid(d / "img0.jpg")
    client.post("/ui/hide", headers=auth, json={"id": a, "hidden": True}); unlock(client, auth)
    client.delete(f"/ui/assets/{a}", headers=auth, json={"confirm": True})
    assert not server.fav_load("hidden.json")
    assert client.post(f"/ui/trash/{a}/restore", headers=auth).status_code == 200
    assert [v["path"] for v in server.fav_load("hidden.json").values()] == [str(d / "img0.jpg")]


def test_trash_thumb_of_video_is_404_not_500(client, auth, lib):
    d = lib / "Trip 2019"; (d / "clip.mov").write_bytes(b"\0" * 100); pick(client, auth, d)
    v = aid(d / "clip.mov")
    client.delete(f"/ui/assets/{v}", headers=auth, json={"confirm": True})
    assert client.get(f"/ui/trash/{v}/thumbnail", headers=auth).status_code == 404


def test_put_assets_checks_every_id_first(client, auth, lib):
    d = lib / "Trip 2019"; pick(client, auth, d)
    a, b = aid(d / "img0.jpg"), aid(d / "img1.jpg")
    client.post("/ui/hide", headers=auth, json={"id": b, "hidden": True})
    client.post("/api/auth/session/lock", headers=auth)
    assert client.put("/api/assets", headers=auth, json={"ids": [a, b], "isFavorite": True}).status_code == 403
    assert not server.fav_load()  # a was not changed either


# --- scanning (#9, #10, #13, #8) ---
def test_unreadable_folder_keeps_its_photos(client, auth, lib):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    d = lib / "Trip 2019"; sub = d / "sub"; sub.mkdir()
    Image.new("RGB", (40, 30)).save(sub / "x.jpg")
    pick(client, auth, d)
    sub.chmod(0)
    try:
        client.post("/ui/rescan", headers=auth)
        with server.app.app_context():
            assert server.db().execute("SELECT 1 FROM assets WHERE path=?", (str(sub / "x.jpg"),)).fetchone()
    finally:
        sub.chmod(0o755)


def test_scan_stops_when_its_folder_is_unpicked(client, auth, lib, monkeypatch):
    d = lib / "Trip 2019"
    fid = server.aid(str(d.resolve()))
    real = server.read_meta

    def slow(folder_id, p, st=None):  # un-pick the album while its scan is reading files
        with server.app.app_context():
            server.db().execute("DELETE FROM folders WHERE id=?", (fid,)); server.db().commit()
        return real(folder_id, p, st)
    monkeypatch.setattr(server, "read_meta", slow)
    pick(client, auth, d)
    with server.app.app_context():
        assert server.db().execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0


def test_video_and_file_dates_are_local_time(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Lisbon"); time.tzset()
    try:
        ts = 1751371200  # 2025-07-01 12:00 UTC = 13:00 in Lisbon (summer)
        assert server.local_iso(ts) == "2025-07-01T13:00:00.000Z"
    finally:
        monkeypatch.delenv("TZ"); time.tzset()


def test_stale_cache_files_are_pruned(client, auth, lib):
    d = lib / "Trip 2019"; pick(client, auth, d)
    a = aid(d / "img0.jpg")
    client.get(f"/api/assets/{a}/thumbnail", headers=auth)
    thumbs = server.data_dir() / "thumbs"
    (thumbs / f"{a}-250.webp").write_bytes(b"old-style name")
    assert len(list(thumbs.glob(f"{a}-*"))) == 2
    client.delete(f"/ui/assets/{aid(d / 'img1.jpg')}", headers=auth, json={"confirm": True})
    server.geo_pass(limit=0)  # a worker pass cleans up once something was removed
    left = [f.name for f in thumbs.glob(f"{a}-*")]
    assert len(left) == 1 and left[0] != f"{a}-250.webp"


# --- memories (#12, #16) ---
def test_memory_window_crosses_new_year():
    yr, days = server._this_day(1, today=date(2026, 1, 1))
    assert yr == "2025" and days[0] == "2024-12-29" and days[-1] == "2025-01-04"
    assert server._this_day(1, today=date(2028, 2, 29))[1][3] == "2027-02-28"


def test_memory_years_one_query(client, auth, lib):
    d = lib / "Trip 2019"; pick(client, auth, d)
    t = date.today()
    with server.app.app_context():
        c = server.db()
        c.execute("UPDATE assets SET taken=? WHERE name='img0.jpg'", (f"{t.year - 3}-{t:%m-%d}T10:00:00.000Z",))
        c.commit()
        assert [(ago, n) for ago, _, n in server._memory_years()] == [(3, 1)]


# --- sync (#20) ---
def test_sync_derived_changes_wait(client, auth, lib, monkeypatch):
    d = lib / "Trip 2019"; pick(client, auth, d)
    th = "t"
    with server.app.app_context():
        c = server.connect()
        f1 = server._sync_fp(c, th)
        server._sync_last[th] = (*server._sync_parts(c), time.time())
        c.execute("INSERT INTO geo VALUES(?,?,?,?,?,?,?)", (aid(d / "img0.jpg"), 0, 1, 2, "X", "Y", "PT")); c.commit()
        assert server._sync_fp(c, th) == f1  # only derived data changed, and recently: no full resend yet
        server._sync_last[th] = (server._sync_last[th][0], server._sync_last[th][1], 0)
        assert server._sync_fp(c, th) != f1


# --- conversions (#24, #25) ---
def test_jpeg_resize_keeps_xmp_and_sets_pixel_size(tmp_path):
    src = tmp_path / "a.jpg"
    ex = Image.Exif(); ex.get_ifd(0x8769)[0xA002] = 400; ex.get_ifd(0x8769)[0xA003] = 300
    Image.new("RGB", (400, 300)).save(src, exif=ex.tobytes(), xmp=b"<x:xmpmeta xmlns:x='adobe:ns:meta/'>hi</x:xmpmeta>")
    out = convert.convert_image(src, "img_pct", {"percent": 50}, tmp_path)
    with Image.open(out) as o:
        assert b"hi" in (o.info.get("xmp") or b"")
        assert o.getexif().get_ifd(0x8769)[0xA002] == 200


def test_resize_output_is_capped(tmp_path):
    src = tmp_path / "big.png"
    Image.new("L", (3000, 2600)).save(src)
    with pytest.raises(convert.BadRequest):
        convert.convert_image(src, "img_pct", {"percent": 400}, tmp_path)


# --- small fixes (#18, #37, #44) ---
def test_video_mime_types():
    import mimetypes
    assert mimetypes.guess_type("a.mkv")[0] == "video/x-matroska" and mimetypes.guess_type("a.3gp")[0] == "video/3gpp"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_ui_playback_reports_and_transcodes_in_background(client, auth, lib):
    d = lib / "vids"; d.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=176x144:rate=10:duration=1",
                    "-c:v", "h263", str(d / "old.3gp")], check=True)
    pick(client, auth, d)
    v = aid(d / "old.3gp")
    assert client.get(f"/ui/playback/{v}?check=1", headers=auth).get_json() == {"native": False, "web": False}
    for _ in range(100):
        r = client.get(f"/ui/playback/{v}", headers=auth)
        if r.status_code == 200:
            break
        assert r.status_code == 202
        time.sleep(0.1)
    assert r.status_code == 200 and r.mimetype == "video/mp4"


# --- follow-ups: strict CSP (#6), thumbnail pre-generation (#26), SHA-1 checksums (#38), re-clustering (#35) ---
def test_page_has_no_inline_script(client):
    import re
    html = client.get("/").get_data(as_text=True)
    assert '<script src="/app.js"></script>' in html and not re.search(r"<script>|\son[a-z]+=", html)
    assert "'unsafe-inline'" not in client.get("/").headers["Content-Security-Policy"].split("script-src")[1].split(";")[0]
    js = client.get("/app.js")
    assert js.status_code == 200 and js.mimetype == "text/javascript" and "function render" in js.get_data(as_text=True)
    assert not re.search(r"\son[a-z]+=\"", js.get_data(as_text=True))  # no inline handlers built into markup either


def test_thumbs_are_made_in_the_background(client, auth, lib):
    d = lib / "Trip 2019"; pick(client, auth, d)
    assert server.thumbs_pass(pause=0) == 2
    with server.app.app_context():
        for a in server.db().execute("SELECT * FROM assets").fetchall():
            if a["path"].startswith(str(d)):
                assert server.thumb_path(a, 250).exists()
    assert server.thumbs_pass(pause=0) == 0  # all made: the next pass has nothing to do


def test_checksum_is_base64_sha1(client, auth, lib):
    import base64, hashlib
    d = lib / "Trip 2019"; pick(client, auth, d)
    a = aid(d / "img0.jpg")
    assert client.get(f"/api/assets/{a}", headers=auth).get_json()["checksum"] == a  # not hashed yet
    while server.checksum_pass():
        pass
    want = base64.b64encode(hashlib.sha1((d / "img0.jpg").read_bytes()).digest()).decode()
    assert client.get(f"/api/assets/{a}", headers=auth).get_json()["checksum"] == want


def _person(c, pid, name, vec, faces_):
    import numpy as np
    v = np.asarray(vec, np.float32); v /= np.linalg.norm(v)
    c.execute("INSERT INTO persons(id, name, centroid, n) VALUES(?,?,?,?)", (pid, name, v.tobytes(), len(faces_)))
    for asset in faces_:
        c.execute("INSERT INTO faces(asset_id,x,y,w,h,score,emb,person_id) VALUES(?,.1,.1,.3,.3,.9,?,?)", (asset, v.tobytes(), pid))


def test_recluster_joins_only_unnamed_and_respects_not_this_person(client, auth, lib):
    d = lib / "Trip 2019"; pick(client, auth, d)
    a0, a1 = aid(d / "img0.jpg"), aid(d / "img1.jpg")
    base = [1.0] + [0.0] * 127
    near = [1.0, 0.3] + [0.0] * 126  # cosine ~0.96 with base
    with server.app.app_context():
        c = server.db()
        _person(c, "named", "Ana", base, [a0])
        _person(c, "u1", "", base, [a0])
        _person(c, "u2", "", near, [a1])
        _person(c, "u3", "", near, [a1])
        _person(c, "far", "", [0.0, 0.0, 1.0] + [0.0] * 125, [a1])
        fid = c.execute("SELECT id FROM faces WHERE person_id='u3'").fetchone()[0]
        c.execute("INSERT INTO face_not VALUES(?, 'u1')", (fid,))  # the user moved u3's face out of u1 once
        c.commit()
    assert server.recluster_pass() == 1  # u2 and u3 (the same person) are joined
    with server.app.app_context():
        c = server.db()
        left = {r[0]: r[1] for r in c.execute("SELECT id, n FROM persons")}
        grp = lambda f: c.execute("SELECT person_id FROM faces WHERE id=?", (f,)).fetchone()[0]
        u1face = c.execute("SELECT id FROM faces WHERE person_id='u1'").fetchone()
        assert u1face is not None and grp(fid) != "u1"   # the face moved out of u1 never goes back with it
    assert left["named"] == 1 and left["far"] == 1  # a named person is never touched, an unrelated group stays
    assert server.recluster_pass() == 0              # stable: a second run changes nothing


def test_live_photo_autoplay_defaults_on_after_3s():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "app.js").read_text()
    assert 'const liveAuto = () => localStorage.photos_liveauto !== "0";' in js   # on by default, only "0" turns it off
    assert "LIVE_DELAY_MS = 3000" in js and "playLive(true); }, slides ? 300 : LIVE_DELAY_MS)" in js  # a slideshow plays it at once

