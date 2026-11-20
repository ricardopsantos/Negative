"""PH91 - hover preview of video and Live Photo thumbnails."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_ph91_hover_preview_wiring():
    js, html = (ROOT / "app.js").read_text(), (ROOT / "index.html").read_text()
    assert "(hover: hover) and (pointer: fine)" in js          # mouse only
    assert 'addEventListener("mouseover"' in js and "hoverStop" in js and 'addEventListener("scroll", hoverStop' in js
    assert "v.loop = true" in js and "v.muted = true" in js    # loops; muted only as the fallback
    assert "playVideo(v, id, false" in js                      # no conversion started for a preview
    assert "video.hv" in html and "pointer-events:none" in html.split("video.hv")[1].split("}")[0]
