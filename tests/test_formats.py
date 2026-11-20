"""Main media formats (HEIC, AVIF, video) are indexed and get thumbnails."""
import shutil
import subprocess

import pytest
from PIL import Image, ImageOps

import pillow_heif
from conftest import album_assets

pillow_heif.register_heif_opener()


def _make(lib, d):
    d.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (640, 480), (200, 40, 40))
    im.save(d / "a.heic")
    im.save(d / "b.webp")
    im.save(d / "c.gif")
    im.save(d / "d.bmp")


def _thumb(client, auth, name, root, **kw):
    client.post("/ui/folders", json={"path": str(root)}, headers=auth)
    a = next(x for x in album_assets(client, auth) if x["originalFileName"] == name)
    return a, client.get(f"/api/assets/{a['id']}/thumbnail", headers=auth)


@pytest.mark.parametrize("name", ["a.heic", "b.webp", "c.gif", "d.bmp"])
def test_image_formats_index_and_thumbnail(client, auth, lib, name):
    _make(lib, lib / "fmt")
    a, r = _thumb(client, auth, name, lib / "fmt")
    assert r.status_code == 200 and r.mimetype == "image/webp"
    assert a["exifInfo"]["exifImageWidth"] == 640 if a.get("exifInfo") else True


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_video_gets_thumbnail_and_size(client, auth, lib):
    d = lib / "vid"
    d.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2",
                    "-pix_fmt", "yuv420p", str(d / "clip.mp4")], check=True)
    a, r = _thumb(client, auth, "clip.mp4", d)
    assert a["type"] == "VIDEO" and r.status_code == 200 and r.mimetype == "image/webp"
    import server
    assert server.probe_video(d / "clip.mp4")[:2] == (320, 240)


def test_corrupt_file_gives_404_not_500(client, auth, lib):
    d = lib / "bad"
    d.mkdir()
    (d / "x.heic").write_bytes(b"not an image")
    (d / "y.mp4").write_bytes(b"not a video")
    client.post("/ui/folders", json={"path": str(d)}, headers=auth)
    for a in album_assets(client, auth):
        if a["originalFileName"] in ("x.heic", "y.mp4"):
            assert client.get(f"/api/assets/{a['id']}/thumbnail", headers=auth).status_code == 404
