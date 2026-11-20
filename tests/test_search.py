"""PH57 smart search: CLIP vectors per photo, found by words; the model itself is replaced by a fake."""
import numpy as np
import pytest
from PIL import Image

import clip as clipmod
from conftest import album_assets
from test_favorites_trash import picked


def unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


class FakeClip:
    """Photos named sun-*.jpg 'look like' vector A, sea-*.jpg vector B; the word 'sunset' is A, 'sea' is B."""
    A, B = unit([1, 0, 0, 0]), unit([0, 1, 0, 0])

    def image(self, im):
        return self.A if im.getpixel((0, 0))[0] > 128 else self.B

    def words(self, q):
        return {"sunset": self.A, "sea": self.B}.get(q, unit([0, 0, 1, 0]))


@pytest.fixture
def clipped(client, auth, lib, monkeypatch):
    d = lib / "shots"
    d.mkdir()
    for n, red in (("sun-1", 250), ("sun-2", 240), ("sea-1", 10)):
        Image.new("RGB", (400, 300), (red, 90, 90)).save(d / f"{n}.jpg")
    client.post("/ui/folders", json={"path": str(d)}, headers=auth)
    import server
    monkeypatch.setattr(server, "_clip_engine", FakeClip())
    monkeypatch.setattr(clipmod, "available", lambda: True)
    server._clip_cache = None
    assert server.clip_pass() >= 3
    return server


def names(client, auth, q):
    r = client.post("/api/search/smart", json={"query": q}, headers=auth).get_json()["assets"]
    return [a["originalFileName"] for a in r["items"]], r["total"]


def test_rank_keeps_close_matches_only():
    m = np.stack([unit([1, 0, 0, 0]), unit([0.9, 0.3, 0, 0]), unit([0, 1, 0, 0])])
    got = clipmod.rank(m, unit([1, 0, 0, 0]))
    assert [i for i, _ in got] == [0, 1]
    assert clipmod.rank(None, unit([1, 0, 0, 0])) == [] and clipmod.rank(np.zeros((0, 4), np.float32), unit([1, 0, 0, 0])) == []


def test_preprocess_shape_and_range():
    out = clipmod.preprocess(Image.new("RGB", (640, 480), (255, 255, 255)))
    assert out.shape == (1, 3, 224, 224) and out.dtype == np.float32


def test_words_find_the_matching_photos(clipped, client, auth):
    got, total = names(client, auth, "sunset")
    assert sorted(got) == ["sun-1.jpg", "sun-2.jpg"] and total == 2
    assert names(client, auth, "sea")[0] == ["sea-1.jpg"]
    assert names(client, auth, "unrelated") == ([], 0) and names(client, auth, "") == ([], 0)


def test_hidden_photos_never_match(clipped, client, auth):
    sun = next(a for a in album_assets(client, auth) if a["originalFileName"] == "sun-1.jpg")
    with clipped.app.app_context():
        path = clipped.db().execute("SELECT path FROM assets WHERE id=?", (sun["id"],)).fetchone()["path"]
        clipped.db().execute("INSERT INTO hidden VALUES(?)", (path,))
        clipped.db().commit()
    assert names(client, auth, "sunset")[0] == ["sun-2.jpg"]


def test_clip_pass_is_idempotent_and_status_counts(clipped, client, auth):
    assert clipped.clip_pass() == 0
    st = client.get("/ui/search/status", headers=auth).get_json()
    assert st["enabled"] is True and st["scanned"] == st["photos"] >= 3


def test_timeline_accepts_the_search_filter(clipped, client, auth):
    b = client.get("/api/timeline/buckets?search=sunset", headers=auth).get_json()
    assert sum(x["count"] for x in b) == 2


def test_search_needs_login(client):
    assert client.post("/api/search/smart", json={"query": "x"}).status_code in (401, 403)
