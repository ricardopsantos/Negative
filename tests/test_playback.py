"""PH39: videos browsers cannot decode are played from a cached H.264/AAC MP4; the original is untouched."""
import shutil
import subprocess

import pytest

from conftest import album_assets, tree_state

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def _clip(path, vcodec):
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=176x144:rate=10:duration=1",
                    "-c:v", vcodec, "-pix_fmt", "yuv420p", str(path)], check=True)


def _playback(client, auth, root, name):
    client.post("/ui/folders", json={"path": str(root)}, headers=auth)
    a = next(x for x in album_assets(client, auth) if x["originalFileName"] == name)
    return client.get(f"/api/assets/{a[chr(105)+chr(100)]}/video/playback", headers=auth)


def test_h263_3gp_is_transcoded_to_mp4(client, auth, lib):
    d = lib / "vids"; d.mkdir()
    _clip(d / "old.3gp", "h263")
    before = tree_state(d)
    r = _playback(client, auth, d, "old.3gp")
    assert r.status_code == 200 and r.mimetype == "video/mp4" and r.data[4:8] == b"ftyp"
    assert r.data != (d / "old.3gp").read_bytes()
    assert tree_state(d) == before


def test_h264_mp4_is_sent_as_is(client, auth, lib):
    d = lib / "vids"; d.mkdir()
    _clip(d / "ok.mp4", "libx264")
    r = _playback(client, auth, d, "ok.mp4")
    assert r.status_code == 200 and r.data == (d / "ok.mp4").read_bytes()
