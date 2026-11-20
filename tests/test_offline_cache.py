"""PH93: with the library's disk unplugged, cached thumbnails and previews are still served."""
import io
import shutil

from PIL import Image

import server
from test_review_fixes import pick


def _assets(d):
    with server.app.app_context():
        return [a for a in server.db().execute("SELECT * FROM assets").fetchall() if a["path"].startswith(str(d))]


def _unplug(d, tmp_path):
    shutil.move(str(d), str(tmp_path / "unplugged"))


def test_cached_thumb_and_preview_served_with_disk_unplugged(client, auth, lib, tmp_path):
    d = lib / "Trip 2019"; pick(client, auth, d)
    assert server.thumbs_pass(pause=0) == 2 and server.thumbs_pass(pause=0, size=1440) == 2
    a = _assets(d)[0]
    _unplug(d, tmp_path)
    for q, px in (("", 250), ("?size=preview", 400)):
        r = client.get(f"/api/assets/{a['id']}/thumbnail{q}", headers=auth)
        assert r.status_code == 200 and r.mimetype == "image/webp"
        assert max(Image.open(io.BytesIO(r.data)).size) == px  # the 400x300 fixture is smaller than 1440
    r = client.get(f"/api/assets/{a['id']}/thumbnail?size=full", headers=auth)
    assert r.status_code == 200 and r.mimetype == "image/webp"  # the original stands in by its preview
    assert client.get(f"/api/assets/{a['id']}/original", headers=auth).status_code == 404


def test_other_size_stands_in_and_nothing_cached_is_404(client, auth, lib, tmp_path):
    d = lib / "Trip 2019"; pick(client, auth, d)
    assert server.thumbs_pass(pause=0) == 2  # grid only, no previews
    a, b = _assets(d)
    server.thumb_path(b, 250).unlink()
    _unplug(d, tmp_path)
    r = client.get(f"/api/assets/{a['id']}/thumbnail?size=preview", headers=auth)
    assert r.status_code == 200 and max(Image.open(io.BytesIO(r.data)).size) == 250
    assert client.get(f"/api/assets/{b['id']}/thumbnail", headers=auth).status_code == 404


def test_pass_skips_missing_files_without_marking_them_failed(client, auth, lib, tmp_path):
    d = lib / "Trip 2019"; pick(client, auth, d)
    moved = tmp_path / "unplugged"
    _unplug(d, tmp_path)
    assert server.thumbs_pass(pause=0) == 0
    shutil.move(str(moved), str(d))  # disk back: the same pass picks them up, no restart needed
    assert server.thumbs_pass(pause=0) == 2


def test_turning_needs_the_original(client, auth, lib, tmp_path):
    d = lib / "Trip 2019"; pick(client, auth, d)
    server.thumbs_pass(pause=0)
    a = _assets(d)[0]
    _unplug(d, tmp_path)
    r = client.post("/ui/rotate", json={"id": a["id"], "direction": "right"}, headers=auth)
    assert r.status_code == 409
    assert server.thumb_path(a, 250).exists()
