# summary: Face and smart-search features are hidden when off (PHOTOS_FACES / PHOTOS_CLIP = 0) or their models are missing: the server says so, and the web page reads it.
from pathlib import Path

APP = (Path(__file__).resolve().parent.parent / "app.js").read_text()


def test_features_follow_the_switches(client, monkeypatch):
    import faces, clip
    monkeypatch.setattr(faces, "available", lambda: True)
    monkeypatch.setattr(clip, "available", lambda: True)
    f = client.get("/api/server/features").get_json()
    assert f["facialRecognition"] and f["smartSearch"]
    monkeypatch.setenv("PHOTOS_FACES", "0")
    monkeypatch.setenv("PHOTOS_CLIP", "0")
    f = client.get("/api/server/features").get_json()
    assert not f["facialRecognition"] and not f["smartSearch"]


def test_missing_models_count_as_off(client, monkeypatch):
    import faces, clip
    monkeypatch.setattr(faces, "available", lambda: False)
    monkeypatch.setattr(clip, "available", lambda: False)
    f = client.get("/api/server/features").get_json()
    assert not f["facialRecognition"] and not f["smartSearch"]


def test_web_page_hides_what_is_off():
    # read once at start, the same answer the phone app gets
    assert 'api("/api/server/features")' in APP and "facialRecognition" in APP and "smartSearch" in APP
    assert '$("searchf").hidden = !FEAT.search' in APP and '$("vtag").hidden = !FEAT.faces' in APP
    # sidebar, View menu, Explore, viewer chips, Info, Tag (menus, key T) all check it
    assert '(FEAT.faces ? ROW("people", "People") : "")' in APP
    assert '...(FEAT.faces ? [{l: "People", run: view("people")}] : [])' in APP
    assert '(FEAT.faces ? sec("People"' in APP
    assert "if (!FEAT.faces) return;" in APP
    assert "function startTag() {\n  if (!FEAT.faces ||" in APP
    assert APP.count('...(FEAT.faces ? [{l: "Tag person..."') == 2
    # an old link to a hidden page falls back to All photos
    assert '(!FEAT.search && route.startsWith("search/"))) route = "all"' in APP
