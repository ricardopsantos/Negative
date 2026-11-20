"""Conversions write NEW files beside the original, never touch it, never overwrite; formats can be filtered."""
import shutil
import subprocess
import time

import pytest
from PIL import Image

import pillow_heif
from conftest import album_assets, tree_state

pillow_heif.register_heif_opener()
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


@pytest.fixture
def media(lib, client, auth):
    d = lib / "conv"
    d.mkdir()
    photo = Image.effect_noise((800, 600), 80).convert("RGB")
    ex = Image.Exif()
    ex[0x010F] = "TestCam"
    photo.save(d / "p.heic", exif=ex)
    Image.new("RGBA", (400, 300), (255, 0, 0, 128)).save(d / "a.png")
    photo.save(d / "b.bmp")
    photo.save(d / "j.jpg")
    client.post("/ui/folders", json={"path": str(d)}, headers=auth)
    return d


def aid(client, auth, name):
    return next(a["id"] for a in album_assets(client, auth) if a["originalFileName"] == name)


def conv(client, auth, name, op, **kw):
    return client.post("/ui/convert", json={"id": aid(client, auth, name), "op": op, **kw}, headers=auth)


def test_heic_to_png_and_jpg_and_original_untouched(client, auth, media):
    before = tree_state(media)
    r = conv(client, auth, "p.heic", "heic_png")
    assert r.status_code == 201 and r.get_json()["name"] == "p.png"
    assert Image.open(media / "p.png").size == (800, 600)
    lo = conv(client, auth, "p.heic", "heic_jpg", quality=10).get_json()["name"]
    hi = conv(client, auth, "p.heic", "heic_jpg", quality=95).get_json()["name"]
    assert lo == "p.jpg" and hi == "p 1.jpg"  # never overwrites: the second name is new
    assert (media / lo).stat().st_size < (media / hi).stat().st_size
    assert Image.open(media / hi).getexif().get(0x010F) == "TestCam"  # metadata survives
    after = tree_state(media)
    for k, v in before.items():
        assert after[k] == v  # every original byte-identical, same mtime


def test_png_alpha_to_jpg_bmp_to_png(client, auth, media):
    assert conv(client, auth, "a.png", "png_jpg", quality=80).status_code == 201
    assert Image.open(media / "a.jpg").mode == "RGB"
    assert conv(client, auth, "b.bmp", "bmp_png").status_code == 201
    assert Image.open(media / "b.png").format == "PNG"


def test_resize_percent_and_pixels(client, auth, media):
    assert conv(client, auth, "j.jpg", "img_pct", percent=50).get_json()["name"] == "j (50%).jpg"
    assert Image.open(media / "j (50%).jpg").size == (400, 300)
    conv(client, auth, "j.jpg", "img_px", width=200)
    assert Image.open(media / "j (200x).jpg").size == (200, 150)  # height follows the ratio
    conv(client, auth, "j.jpg", "img_px", width=100, height=100)
    assert Image.open(media / "j (100x100).jpg").size == (100, 75)  # fits inside the box


def test_validation(client, auth, media):
    assert conv(client, auth, "j.jpg", "heic_png").status_code == 400  # wrong source format
    assert conv(client, auth, "j.jpg", "img_pct", percent=0).status_code == 400
    assert conv(client, auth, "j.jpg", "img_px").status_code == 400
    assert conv(client, auth, "p.heic", "heic_jpg", quality=101).status_code == 400
    assert conv(client, auth, "j.jpg", "nope").status_code == 400
    ops = client.get(f"/ui/convert/ops?id={aid(client, auth, 'p.heic')}", headers=auth).get_json()
    assert {o["op"] for o in ops} == {"heic_png", "heic_jpg"}
    client.delete_cookie("photos_session")
    assert client.post("/ui/convert", json={"id": "x", "op": "heic_png"}).status_code == 401


def test_format_filter_and_counts(client, auth, media):
    n = {f["format"]: f["count"] for f in client.get("/ui/formats", headers=auth).get_json()}
    assert n["heic"] == 1 and n["png"] == 1 and n["bmp"] == 1 and n["jpg"] >= 1
    b = client.get("/api/timeline/buckets?format=heic,png", headers=auth).get_json()
    assert sum(x["count"] for x in b) == 2
    assert client.get("/api/timeline/buckets?format=bogus", headers=auth).get_json() != []  # unknown = no filter


@needs_ffmpeg
def test_video_conversions(client, auth, lib):
    d = lib / "vids"
    d.mkdir()
    for ext in ("mov", "avi", "flv", "wmv"):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=1",
                        "-f", "lavfi", "-i", "sine=duration=1", "-shortest", str(d / f"v.{ext}")], check=True)
    client.post("/ui/folders", json={"path": str(d)}, headers=auth)
    before = tree_state(d)
    for ext in ("mov", "avi", "flv", "wmv"):
        r = conv(client, auth, f"v.{ext}", "vid_mp4")
        assert r.status_code == 202
        jid = r.get_json()["job"]
        for _ in range(100):
            j = client.get(f"/ui/convert/{jid}", headers=auth).get_json()
            if j["state"] in ("done", "error"):
                break
            time.sleep(0.2)
        assert j["state"] == "done", j
    assert (d / "v.mp4").exists() and (d / "v 1.mp4").exists()
    import server
    assert server.probe_video(d / "v.mp4")[:2] == (320, 240)
    mp4 = next(a for a in album_assets(client, auth) if a["originalFileName"] == "v.mp4")
    r = client.post("/ui/convert", json={"id": mp4["id"], "op": "mp4_pct", "percent": 50}, headers=auth)
    jid = r.get_json()["job"]
    while client.get(f"/ui/convert/{jid}", headers=auth).get_json()["state"] not in ("done", "error"):
        time.sleep(0.2)
    assert server.probe_video(d / "v (50%).mp4")[:2] == (160, 120)
    r = client.post("/ui/convert", json={"id": mp4["id"], "op": "mp4_px", "width": 128}, headers=auth)
    jid = r.get_json()["job"]
    while client.get(f"/ui/convert/{jid}", headers=auth).get_json()["state"] not in ("done", "error"):
        time.sleep(0.2)
    assert server.probe_video(d / "v (128x).mp4")[:2] == (128, 96)
    after = tree_state(d)
    for k, v in before.items():
        assert after[k] == v


def test_metadata_and_rotate_and_download(client, auth, media):
    i = aid(client, auth, "p.heic")
    sec = {s["title"]: dict(s["rows"]) for s in client.get(f"/ui/metadata/{i}", headers=auth).get_json()}
    assert sec["File"]["Name"] == "p.heic" and sec["Image"]["Pixels"] == "800 x 600"
    assert sec["Camera and picture (EXIF)"]["Make"] == "TestCam"
    j = aid(client, auth, "j.jpg")
    assert client.post("/ui/rotate", json={"id": j, "direction": "right"}, headers=auth).get_json()["rotation"] == 90
    assert client.get(f"/api/assets/{j}", headers=auth).get_json()["width"] == 600  # w/h swap
    t = client.get(f"/api/assets/{j}/thumbnail?size=full", headers=auth)
    assert t.status_code == 200 and Image.open(__import__("io").BytesIO(t.data)).size == (600, 800)
    assert client.post("/ui/rotate", json={"id": j, "direction": "left"}, headers=auth).get_json()["rotation"] == 0
    full = client.get(f"/api/assets/{j}/thumbnail?size=full", headers=auth)
    assert full.data == (media / "j.jpg").read_bytes()  # unrotated JPEG is served as the original
    d = client.get(f"/api/assets/{j}/original?download=1", headers=auth)
    assert "attachment" in d.headers["Content-Disposition"] and "j.jpg" in d.headers["Content-Disposition"]
    assert client.get(f"/api/assets/{i}/thumbnail?size=full", headers=auth).mimetype == "image/jpeg"  # HEIC re-rendered


def _names(client, auth, **q):
    from urllib.parse import urlencode
    qs = urlencode(q)
    out = []
    for b in client.get("/api/timeline/buckets?" + qs, headers=auth).get_json():
        col = client.get("/api/timeline/bucket?" + qs + "&timeBucket=" + b["timeBucket"], headers=auth).get_json()
        out += [client.get(f"/api/assets/{i}", headers=auth).get_json()["originalFileName"] for i in col["id"]]
    return out


def test_sorting(client, auth, lib):
    d = lib / "srt"
    d.mkdir()
    import os
    for n, size, mt in (("b.jpg", 50, 1000), ("a.png", 300, 3000), ("c.bmp", 100, 2000)):
        Image.new("RGB", (size, size), (size % 255, 0, 0)).save(d / n)
        os.utime(d / n, (mt, mt))
    client.post("/ui/folders", json={"path": str(d)}, headers=auth)
    assert _names(client, auth, sort="name", order="asc") == ["a.png", "b.jpg", "c.bmp"]
    assert _names(client, auth, sort="name", order="desc") == ["c.bmp", "b.jpg", "a.png"]
    assert _names(client, auth, sort="size", order="asc") != _names(client, auth, sort="size", order="desc")
    by_size = _names(client, auth, sort="size", order="desc")
    assert by_size.index("c.bmp") < by_size.index("a.png") < by_size.index("b.jpg")  # BMP is uncompressed: largest on disk
    assert _names(client, auth, sort="type", order="asc") == ["c.bmp", "b.jpg", "a.png"]  # bmp, jpg, png
    assert _names(client, auth, sort="modified", order="asc") == ["b.jpg", "c.bmp", "a.png"]  # mtimes 1000 < 2000 < 3000
    b = client.get("/api/timeline/buckets?sort=name&order=asc", headers=auth).get_json()
    assert [(x["timeBucket"], x["label"]) for x in b] == [("grp:A:0", "A"), ("grp:B:0", "B"), ("grp:C:0", "C")]  # PH75: by letter
    t = client.get("/api/timeline/buckets?sort=type&order=asc", headers=auth).get_json()
    assert [x["label"] for x in t] == ["BMP", "JPG", "PNG"]
    z = client.get("/api/timeline/buckets?sort=size", headers=auth).get_json()
    assert z and all(x["label"] == "< 1 MB" for x in z)
    asc = client.get("/api/timeline/buckets?order=asc", headers=auth).get_json()
    desc = client.get("/api/timeline/buckets", headers=auth).get_json()
    assert [x["timeBucket"] for x in asc] == [x["timeBucket"] for x in desc][::-1]


@needs_ffmpeg
def test_live_photo_pairs_hides_clip_and_deletes_together(client, auth, lib):
    d = lib / "live"
    d.mkdir()
    Image.new("RGB", (200, 150), (5, 90, 40)).save(d / "IMG_1.jpg")
    Image.new("RGB", (200, 150), (50, 9, 40)).save(d / "plain.jpg")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1", "-pix_fmt", "yuv420p",
                    str(d / "IMG_1.MOV")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1", "-pix_fmt", "yuv420p",
                    str(d / "holiday.mov")], check=True)  # a clip with no same-name photo is an ordinary video
    client.post("/ui/folders", json={"path": str(d)}, headers=auth)
    names = sorted(a["originalFileName"] for a in album_assets(client, auth))
    assert names == ["IMG_1.jpg", "holiday.mov", "plain.jpg"]  # IMG_1.MOV is not a tile of its own
    alb = client.get("/api/albums", headers=auth).get_json()[0]
    assert alb["assetCount"] == 3
    still = next(a for a in album_assets(client, auth) if a["originalFileName"] == "IMG_1.jpg")
    assert still["livePhotoVideoId"]
    assert client.get(f"/api/assets/{still['livePhotoVideoId']}/video/playback", headers=auth).status_code == 200
    col = client.get("/api/timeline/bucket?timeBucket=" + client.get("/api/timeline/buckets", headers=auth).get_json()[0]["timeBucket"], headers=auth).get_json()
    assert sum(1 for v in col["livePhotoVideoId"] if v) == 1
    titles = [s["title"] for s in client.get(f"/ui/metadata/{still['id']}", headers=auth).get_json()]
    assert "Live Photo video" in titles
    plain = next(a for a in album_assets(client, auth) if a["originalFileName"] == "plain.jpg")
    assert plain["livePhotoVideoId"] is None
    # deleting the photo trashes the clip with it, and restoring the photo brings both back as a pair
    assert client.delete(f"/ui/assets/{still['id']}", json={"confirm": True}, headers=auth).status_code == 200
    assert not (d / "IMG_1.jpg").exists() and not (d / "IMG_1.MOV").exists()
    tr = client.get("/ui/trash", headers=auth).get_json()
    assert [t["name"] for t in tr] == ["IMG_1.jpg"]  # one trash entry, not two
    assert client.post(f"/ui/trash/{tr[0]['id']}/restore", headers=auth).status_code == 200
    assert (d / "IMG_1.jpg").exists() and (d / "IMG_1.MOV").exists()
    again = next(a for a in album_assets(client, auth) if a["originalFileName"] == "IMG_1.jpg")
    assert again["livePhotoVideoId"]
    # deleting just the clip leaves a plain photo
    assert client.delete(f"/ui/assets/{again['id']}/live-video", json={"confirm": True}, headers=auth).status_code == 200
    assert (d / "IMG_1.jpg").exists() and not (d / "IMG_1.MOV").exists()
    assert next(a for a in album_assets(client, auth) if a["originalFileName"] == "IMG_1.jpg")["livePhotoVideoId"] is None
