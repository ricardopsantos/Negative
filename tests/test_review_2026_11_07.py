"""Review of 2026-11-07 (Tasks cards "issues" and "improve"): PH63-PH87, server side."""
import io
import json
import os
import time
from pathlib import Path

from PIL import Image

from conftest import album_assets
from test_mobile import ALL, lines
from test_upload_albums import jpeg, up

PIN = {"pinCode": "12341234"}


def pick(client, auth, path):
    assert client.post("/ui/folders", json={"path": str(path)}, headers=auth).status_code == 202


def server():
    import server as s
    return s


# PH63: the sidebar count is the timeline's count, not a sum of albums
def test_counts_nested_album_once_and_no_upload_album(client, auth, lib):
    pick(client, auth, lib / "Home")
    pick(client, auth, lib / "Home" / "Kids")  # nested inside Home: its photo must not count twice
    pick(client, auth, lib / "Trip 2019")
    up(client, auth, "IMG_9.jpg", jpeg())
    albums = client.get("/api/albums", headers=auth).get_json()
    assert sum(a["assetCount"] for a in albums) == 5  # 1 + 1 + 2 + 1 (upload): the old sum was wrong
    c = client.get("/ui/counts", headers=auth).get_json()
    assert c["all"] == 3 and c["trash"] == 0
    home = next(a for a in albums if a["albumName"] == "Home")
    assert home["nested"] == ["Kids"] and next(a for a in albums if a["albumName"] == "Kids")["insideOf"] == ["Home"]
    one = client.get("/api/albums/" + next(a["id"] for a in albums if a["isUploadFolder"]), headers=auth).get_json()
    assert one["isUploadFolder"] is True  # PH65: the single-album endpoint agrees with the list


# PH66: formats are counted in SQL and still per scope
def test_formats_grouped(client, auth, lib):
    pick(client, auth, lib / "Trip 2019")
    Image.new("RGB", (20, 20)).save(lib / "Trip 2019" / "x.PNG")
    client.post("/ui/rescan", headers=auth)
    assert client.get("/ui/formats", headers=auth).get_json() == [{"format": "jpg", "count": 2}, {"format": "png", "count": 1}]


# PH67: expired logins are swept daily
def test_daily_sweep_drops_expired_logins(client, auth):
    s = server()
    with s.app.app_context():
        s.db().execute("INSERT INTO tokens VALUES('old', ?)", (time.time() - 100 * 86400,))
        s.db().execute("INSERT INTO sessions VALUES('old', 'sid-old', 0, 0, 'phone')")
        s.db().execute("INSERT INTO sessions VALUES('orphan', 'sid-orphan', 0, 0, 'phone')")
        s.db().commit()
    s.daily_sweep()
    with s.app.app_context():
        assert s.db().execute("SELECT COUNT(*) FROM tokens WHERE hash='old'").fetchone()[0] == 0
        assert s.db().execute("SELECT COUNT(*) FROM sessions WHERE hash IN ('old','orphan')").fetchone()[0] == 0
        assert s.db().execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 1  # this login stays
    assert client.get("/api/users/me", headers=auth).status_code == 200


# PH68: a date from the file or folder name when there is no EXIF date
def test_dates_from_names(tmp_path, monkeypatch, lib):
    s = server()
    monkeypatch.setenv("PHOTOS_BROWSE_ROOT", str(lib))
    now = time.time()
    p = lambda rel: lib / rel
    assert s.name_date(p("x/IMG_20190102_131415.jpg"), now) == ("2019-01-02T13:14:15.000Z", "name")
    assert s.name_date(p("x/WhatsApp Image 2018-05-06 at 10.11.12.jpeg"), now) == ("2018-05-06T10:11:12.000Z", "name")
    assert s.name_date(p("x/VID-20170809-WA0001.mp4"), now)[0][:10] == "2017-08-09"
    assert s.name_date(p("1987/scan 4.jpg"), now) == ("1987-07-01T12:00:00.000Z", "folder")
    assert s.name_date(p("2019-06 Trip/a.jpg"), now) == ("2019-06-15T12:00:00.000Z", "folder")
    assert s.name_date(p("x/PXL_20991231_000000.jpg"), now)[1] != "name"  # never in the future
    m87 = time.mktime((1987, 3, 4, 5, 6, 7, 0, 0, -1))
    assert s.name_date(p("1987/scan.jpg"), m87) == (s.local_iso(m87), "file")  # the mtime is inside the year: more precise


def test_scanned_photo_without_exif_is_dated_by_its_name(client, auth, lib):
    d = lib / "Old"
    d.mkdir()
    Image.new("RGB", (30, 30)).save(d / "IMG_20100304_050607.jpg")
    pick(client, auth, d)
    a = album_assets(client, auth)[0]
    assert a["localDateTime"] == "2010-03-04T05:06:07.000Z"
    info = {r[0]: r[1] for sec in client.get("/ui/metadata/" + a["id"], headers=auth).get_json() for r in sec["rows"]}
    assert info["Date from"] == "the file name" and info["Date shown"] == "4 Mar 2010, 05:06"  # PH84


# PH84: Info shows human values
def test_info_human_values(client, auth, lib):
    pick(client, auth, lib / "Trip 2019")
    a = album_assets(client, auth)[0]
    rows = {r[0]: r[1] for sec in client.get("/ui/metadata/" + a["id"], headers=auth).get_json() for r in sec["rows"]}
    assert rows["Size"].endswith(" KB") or rows["Size"].endswith(" bytes")
    assert rows["Folder"] == "Trip 2019" and "Path" not in rows  # relative to the library, not the container path
    s = server()
    assert s.human_size(17835) == "17.4 KB" and s.nice_date("2019-08-23T10:00:00.000Z") == "23 Aug 2019, 10:00"


# PH69: re-reading an unchanged file keeps its checksum; the first upload's checksum is never lost to the scan
def test_upload_checksum_survives_the_scan(client, auth, lib):
    data = jpeg((1, 2, 3))
    assert up(client, auth, "evil.jpg", data).status_code == 201
    client.post("/ui/rescan", headers=auth)
    s = server()
    s.apply_albums(force=True)
    with s.app.app_context():
        assert s.db().execute("SELECT sha1 FROM assets WHERE name='evil.jpg'").fetchone()[0]
    assert up(client, auth, "b.jpg", data).get_json()["status"] == "duplicate"


# PH71: names cut by bytes, control characters gone, content checked
def test_upload_names_and_content(client, auth, lib):
    r = up(client, auth, "é" * 150 + ".jpg", jpeg())
    assert r.status_code == 201
    n = r.get_json()["name"]
    assert len(n.encode()) <= 200 and n.endswith(".jpg")
    r = up(client, auth, "tab%09name%0Aline.jpg", jpeg((9, 9, 9)))
    assert r.status_code == 201 and r.get_json()["name"] == "tab name line.jpg"
    r = up(client, auth, "x.jpg", os.urandom(4000))
    assert r.status_code == 415 and not (lib / "_Organize" / "x.jpg").exists()
    assert not [p for p in (lib / "_Organize").iterdir() if p.name.startswith(".")]


# PH72: the upload album stays behind the PIN in Trash, face crops and the phone sync
def test_upload_album_private_everywhere(client, auth, lib):
    pick(client, auth, lib / "Trip 2019")
    assert up(client, auth, "IMG_7.jpg", jpeg((70, 70, 70))).status_code == 201
    s = server()
    with s.app.app_context():
        a = s.db().execute("SELECT id FROM assets WHERE name='IMG_7.jpg'").fetchone()["id"]
        cur = s.db().execute("INSERT INTO faces(asset_id,x,y,w,h,score,emb) VALUES(?,?,?,?,?,?,?)", (a, .1, .1, .5, .5, .9, b"\0" * 512))
        s.db().commit()
        fid = cur.lastrowid
    assert client.get(f"/ui/faces/{fid}/crop", headers=auth).status_code == 403
    got = lines(client.post("/api/sync/stream", json={"types": ALL}, headers=auth))
    assert a not in [g["data"]["id"] for g in got if g["type"] == "AssetV2"]
    assert len([g for g in got if g["type"] == "AssetV2"]) == 2
    client.post("/api/auth/session/unlock", json=PIN, headers=auth)
    assert client.delete(f"/ui/assets/{a}", json={"confirm": True}, headers=auth).status_code == 200
    assert [t["name"] for t in client.get("/ui/trash", headers=auth).get_json()] == ["IMG_7.jpg"]
    client.post("/api/auth/session/lock", headers=auth)
    assert client.get("/ui/trash", headers=auth).get_json() == []
    assert client.get(f"/ui/trash/{a}/thumbnail", headers=auth).status_code == 403
    assert client.get("/ui/counts", headers=auth).get_json()["trash"] == 0


# PH73: trash thumbnails are cached, purge is not in the GET
def test_trash_thumbnail_cached(client, auth, lib, tmp_path):
    pick(client, auth, lib / "Trip 2019")
    a = album_assets(client, auth)[0]
    client.delete(f"/ui/assets/{a['id']}", json={"confirm": True}, headers=auth)
    r = client.get(f"/ui/trash/{a['id']}/thumbnail", headers=auth)
    assert r.status_code == 200 and "max-age" in r.headers["Cache-Control"]
    assert (tmp_path / "data" / "trash" / a["id"] / ".thumb.webp").exists()
    assert client.get("/ui/counts", headers=auth).get_json()["trash"] == 1


# PH74: restore under a free name, restore all, delete forever, empty with a typed word
def test_trash_management(client, auth, lib):
    pick(client, auth, lib / "Trip 2019")
    a, b = album_assets(client, auth)
    for x in (a, b):
        client.delete(f"/ui/assets/{x['id']}", json={"confirm": True}, headers=auth)
    Path(a["originalPath"]).write_bytes(jpeg())  # something else took the name
    assert client.post(f"/ui/trash/{a['id']}/restore", headers=auth, json={}).status_code == 409
    r = client.post(f"/ui/trash/{a['id']}/restore", headers=auth, json={"rename": True}).get_json()
    assert r["renamed"] and r["name"].endswith(" (1).jpg") and Path(a["originalPath"]).with_name(r["name"]).exists()
    assert client.post("/ui/trash/delete", json={"all": True, "confirm": "yes"}, headers=auth).status_code == 400
    assert client.post("/ui/trash/delete", json={"all": True, "confirm": "EMPTY"}, headers=auth).get_json() == {"deleted": 1}
    assert client.get("/ui/trash", headers=auth).get_json() == []


def test_trash_restore_all(client, auth, lib):
    pick(client, auth, lib / "Trip 2019")
    for x in album_assets(client, auth):
        client.delete(f"/ui/assets/{x['id']}", json={"confirm": True}, headers=auth)
    assert client.post("/ui/trash/restore", json={}, headers=auth).get_json() == {"restored": 2, "failed": 0}
    assert len(album_assets(client, auth)) == 2


# PH75/PH70: recently added
def test_recently_added_sort(client, auth, lib):
    pick(client, auth, lib / "Trip 2019")
    s = server()
    first = album_assets(client, auth)
    with s.app.app_context():
        s.db().execute("UPDATE assets SET added=1000")
        s.db().commit()
    assert up(client, auth, "IMG_2001.jpg", jpeg((4, 4, 4))).status_code == 201
    client.post("/api/auth/session/unlock", json=PIN, headers=auth)
    pick(client, auth, lib / "Home")
    b = client.get("/api/timeline/buckets?sort=added", headers=auth).get_json()
    assert b[0]["timeBucket"][:7] == time.strftime("%Y-%m")  # the photo just added is first, whatever its capture date
    assert len(first) == 2


# PH79: album order and cover in albums.yml
def test_album_order_and_cover(client, auth, lib, tmp_path):
    pick(client, auth, lib / "Trip 2019")
    pick(client, auth, lib / "Home")
    al = {a["albumName"]: a for a in client.get("/api/albums", headers=auth).get_json()}
    assert client.post("/ui/albums/order", json={"ids": [al["Home"]["id"], al["Trip 2019"]["id"]]}, headers=auth).status_code == 200
    assert [a["albumName"] for a in client.get("/api/albums", headers=auth).get_json()] == ["Home", "Trip 2019"]
    trip = album_assets(client, auth, al["Trip 2019"]["id"])
    other = [x for x in trip if x["id"] != al["Trip 2019"]["albumThumbnailAssetId"]][0]
    assert client.post(f"/ui/folders/{al['Trip 2019']['id']}/cover", json={"assetId": other["id"]}, headers=auth).status_code == 200
    got = {a["albumName"]: a for a in client.get("/api/albums", headers=auth).get_json()}
    assert got["Trip 2019"]["albumThumbnailAssetId"] == other["id"]
    y = (tmp_path / "data" / "albums.yml").read_text()
    assert "covers:" in y and "img" in y
    home_photo = album_assets(client, auth, al["Home"]["id"])[0]
    assert client.post(f"/ui/folders/{al['Trip 2019']['id']}/cover", json={"assetId": home_photo["id"]}, headers=auth).status_code == 400


# PH77: map points and the photos of a spot
def test_map(client, auth, lib):
    pick(client, auth, lib / "Trip 2019")
    a, b = album_assets(client, auth)
    s = server()
    with s.app.app_context():
        s.db().execute("INSERT OR REPLACE INTO geo VALUES(?,?,?,?,?,?,?)", (a["id"], 0, 38.72, -9.14, "Lisbon", "", "PT"))
        s.db().commit()
    m = client.get("/ui/map", headers=auth).get_json()
    assert m == {"id": [a["id"]], "lat": [38.72], "lon": [-9.14]}
    bk = client.get("/api/timeline/buckets?bbox=38,-10,39,-9", headers=auth).get_json()
    assert sum(x["count"] for x in bk) == 1


# PH80: the dashboard
def test_stats(client, auth, lib):
    pick(client, auth, lib / "Trip 2019")
    st = client.get("/ui/stats", headers=auth).get_json()
    assert st["total"]["photos"] == 2 and st["albums"][0]["photos"] == 2 and st["formats"][0]["format"] == "jpg"
    assert {"thumbnails", "faces", "search", "places", "checksums"} <= set(st["workers"]) and st["oldest"]["name"]


# PH78: the lock indicator knows the minutes left
def test_auth_status_seconds_left(client, auth):
    assert client.get("/api/auth/status", headers=auth).get_json()["secondsLeft"] == 0
    client.post("/api/auth/session/unlock", json=PIN, headers=auth)
    assert 800 < client.get("/api/auth/status", headers=auth).get_json()["secondsLeft"] <= 900


# PH83: hide every small unnamed group at once, with an undo
def test_hide_unnamed_and_undo(client, auth, lib):
    pick(client, auth, lib / "Trip 2019")
    s = server()
    with s.app.app_context():
        a = s.db().execute("SELECT id FROM assets LIMIT 1").fetchone()["id"]
        for pid, nm in (("p1", ""), ("p2", "Ana")):
            s.db().execute("INSERT INTO persons(id, name, centroid, n) VALUES(?,?,?,1)", (pid, nm, b"\0" * 512))
            s.db().execute("INSERT INTO faces(asset_id,x,y,w,h,score,emb,person_id) VALUES(?,0,0,1,1,1,?,?)", (a, b"\0" * 512, pid))
        s.db().commit()
    assert client.post("/ui/people/hide-unnamed", json={"below": 3}, headers=auth).status_code == 400  # needs confirm
    r = client.post("/ui/people/hide-unnamed", json={"below": 3, "confirm": True}, headers=auth).get_json()
    assert r == {"hidden": 1, "ids": ["p1"]}  # the named one stays
    client.post("/ui/people/show", json={"ids": r["ids"]}, headers=auth)
    with s.app.app_context():
        assert s.db().execute("SELECT hidden FROM persons WHERE id='p1'").fetchone()[0] == 0


# PH85: memories window and this month in past years
def test_memories_window_and_month(client, auth, lib):
    d = lib / "Mem"
    d.mkdir()
    from datetime import date, timedelta
    t = date.today()
    for i, when in enumerate((t.replace(year=t.year - 2) - timedelta(days=6), t.replace(year=t.year - 3, day=1))):
        im = Image.new("RGB", (20, 20), (i * 90, 0, 0))
        ex = im.getexif()
        ex[0x0132] = when.strftime("%Y:%m:%d 12:00:00")
        im.save(d / f"m{i}.jpg", exif=ex)
    pick(client, auth, d)
    assert [m["yearsAgo"] for m in client.get("/ui/memories?days=7", headers=auth).get_json()] == [2] + ([3] if t.day <= 8 else [])
    assert 3 in [m["yearsAgo"] for m in client.get("/ui/memories/month", headers=auth).get_json()]


# PH81: the watcher indexes a new file and forgets a deleted one, never when the folder itself is gone
def test_watch_apply(client, auth, lib):
    pick(client, auth, lib / "Trip 2019")
    s = server()
    new = lib / "Trip 2019" / "new.jpg"
    Image.new("RGB", (10, 10)).save(new)
    s.watch_note(str(new))
    s.watch_note(str(lib / "Trip 2019" / ".syncthing.new.jpg.tmp"))  # temp files are ignored
    assert s.watch_apply(settle=60) == 0  # not settled yet
    assert s.watch_apply(now=time.time() + 10) == 1
    assert len(album_assets(client, auth)) == 3
    new.unlink()
    s.watch_note(str(new))
    s.watch_apply(now=time.time() + 10)
    assert len(album_assets(client, auth)) == 2
    assert client.get("/ui/scan/info", headers=auth).get_json()["watching"]["events"] >= 3


# PH87: installable app with a share target
def test_manifest_share_target_and_vendor(client, auth, lib):
    m = client.get("/manifest.webmanifest").get_json(force=True)
    assert m["share_target"]["action"] == "/ui/share" and m["icons"][0]["src"] == "/icon-192.png"
    assert client.get("/icon-512.png").status_code == 200 and client.get("/sw.js").status_code == 200
    assert client.get("/vendor/leaflet.js").status_code == 200 and client.get("/vendor/../server.py").status_code == 404
    r = server().app.test_client().post("/ui/share", data={"photos": (io.BytesIO(jpeg((5, 50, 5))), "shared.jpg")},
                                        content_type="multipart/form-data")
    assert r.status_code == 303 and r.headers["Location"] == "/#/all" and not (lib / "_Organize").exists()  # not signed in
    r = client.post("/ui/share", data={"photos": [(io.BytesIO(jpeg((5, 50, 5))), "shared.jpg"), (io.BytesIO(b"junk"), "bad.jpg")]},
                    content_type="multipart/form-data", headers={**auth, "Origin": "null", "Sec-Fetch-Site": "none"})
    assert r.status_code == 303 and r.headers["Location"] == "/#/shared/1/0/1"
    assert (lib / "_Organize" / "shared.jpg").exists()


# PH66: the smart-search ranking is computed once per query
def test_smart_search_cached(client, auth, lib, monkeypatch):
    s = server()
    calls = []
    monkeypatch.setattr(s.clipmod, "available", lambda: True)
    monkeypatch.setattr(s, "_clip_matrix", lambda: ([], None))
    monkeypatch.setattr(s, "_smart_rank", lambda q, ids, m, limit: calls.append(q) or [])
    s._smart_cache.clear()
    with s.app.test_request_context():
        s.smart_ids("beach")
        s.smart_ids("beach")
        s.smart_ids("snow")
    assert calls == ["beach", "snow"]


def test_best_match_order(client, auth, lib, monkeypatch):
    pick(client, auth, lib / "Trip 2019")
    a, b = album_assets(client, auth)
    s = server()
    monkeypatch.setattr(s, "smart_ids", lambda q, limit=250: [b["id"], a["id"]])
    bk = client.get("/api/timeline/buckets?search=x&sort=match", headers=auth).get_json()
    assert [x["timeBucket"] for x in bk] == ["match:0"]
    col = client.get("/api/timeline/bucket?search=x&sort=match&timeBucket=match:0", headers=auth).get_json()
    assert col["id"] == [b["id"], a["id"]]


def test_access_log_config():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gconf", Path(__file__).resolve().parent.parent / "gunicorn.conf.py")
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    h = g.logconfig_dict["handlers"]["file"]
    assert h["filename"].endswith(os.path.join("logs", "access.log")) and h["backupCount"] == 5
    assert "file" in g.logconfig_dict["loggers"]["gunicorn.access"]["handlers"]
    json.dumps(g.logconfig_dict)


def test_status_poll_does_not_keep_the_album_open(client, auth, monkeypatch):
    s = server()
    client.post("/api/auth/session/unlock", json=PIN, headers=auth)
    h = next(iter(s._unlocked))
    s._unlocked[h] = time.time() + 5
    client.get("/api/auth/status", headers=auth)
    client.get("/ui/counts", headers=auth)
    assert s._unlocked[h] < time.time() + 6  # looking does not slide the 15 minutes


def test_pin_number_pad():
    root = Path(__file__).resolve().parent.parent
    html, js = (root / "index.html").read_text(), (root / "app.js").read_text()
    assert all(f'data-d="{d}"' in html for d in "0123456789") and 'data-d="back"' in html and 'id="pinDots"' in html
    assert "function pinKey(d)" in js and "i.value.length < 12" in js
