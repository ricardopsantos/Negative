"""PH125: second deep dive of 2026-11-26 — one test or more per fix, plus coverage around them."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_people import people  # noqa: F401  (fixture)

APP = (Path(__file__).parent.parent / "app.js").read_text()


@pytest.fixture
def lib_on(client, auth, lib):
    client.post("/ui/folders", json={"path": str(lib)}, headers=auth).get_data()


def _ids(client, auth, q=""):
    out = []
    for b in client.get("/api/timeline/buckets" + q, headers=auth).get_json():
        sep = "&" if q else "?"
        out += client.get(f"/api/timeline/bucket{q}{sep}timeBucket={b['timeBucket']}", headers=auth).get_json()["id"]
    return out


def _js(*names):
    out = []
    for n in names:
        m = re.search(rf"^(?:const {n} = [^\n]*|(?:async )?function {n}\(.*?^}})$", APP, re.S | re.M)
        assert m, n
        out.append(m.group(0))
    return "\n".join(out) + "\n"


def _node(src):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    return json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)


# 1. favorites.json is parsed once per change, not per call
def test_fav_paths_memoised(client, auth, lib_on, monkeypatch):
    import server
    with server.app.test_request_context():
        server.fav_paths()
        calls = []
        real = server.fav_load
        monkeypatch.setattr(server, "fav_load", lambda *a: (calls.append(1), real(*a))[1])
        for _ in range(5):
            server.fav_paths()
        assert calls == []


def test_fav_paths_sees_a_new_favorite(client, auth, lib_on):
    aid = _ids(client, auth)[0]
    assert client.get("/api/timeline/buckets?isFavorite=true", headers=auth).get_json() == []
    client.post("/ui/favorite", json={"id": aid, "favorite": True}, headers=auth)
    assert _ids(client, auth, "?isFavorite=true") == [aid]
    client.post("/ui/favorite", json={"id": aid, "favorite": False}, headers=auth)
    assert _ids(client, auth, "?isFavorite=true") == []


def test_fav_paths_returns_a_copy(client, auth, lib_on):
    import server
    with server.app.test_request_context():
        server.fav_paths().add("/x")
        assert "/x" not in server.fav_paths()


# 2. "." in the grid goes through the web app's favorite route and refreshes the counts
def test_fav_tile_uses_ui_route_and_refreshes():
    src = _js("favTile")
    assert '"/ui/favorite"' in src and "loadAlbums()" in src and "favMode && !on" in src


# 3. a reversed date range is swapped
def test_map_reversed_range_swapped():
    assert "fr.value > to.value" in APP


# 4. the date range reaches the opened spot
def _geo(server, ids):
    with server.app.app_context():
        c = server.connect()
        for i in ids:
            c.execute("INSERT OR REPLACE INTO geo(asset_id, mtime, lat, lon) VALUES(?, 0, 38.7, -9.1)", (i,))
        c.commit()


def test_spot_with_date_range(client, auth, lib_on):
    import server
    ids = _ids(client, auth)
    _geo(server, ids)
    days = sorted(client.get("/ui/map", headers=auth).get_json()["day"])
    assert len(_ids(client, auth, f"?bbox=38,-10,39,-9&from={days[0]}&to={days[-1]}")) == len(ids)
    assert len(_ids(client, auth, f"?bbox=38,-10,39,-9&from={days[-1]}&to={days[-1]}")) == days.count(days[-1])
    assert _ids(client, auth, "?bbox=38,-10,39,-9&from=1990-01-01&to=1990-12-31") == []
    assert _ids(client, auth, "?bbox=38,-10,39,-9&to=1990-12-31") == []


@pytest.mark.parametrize("bad", ["2020-1-1", "yesterday", "2020-01-01'--"])
def test_spot_bad_dates_are_400(client, auth, lib_on, bad):
    assert client.get(f"/api/timeline/buckets?from={bad}", headers=auth).status_code == 400


def test_spot_route_parses_range():
    assert 'q.set("from", f)' in APP and "${mapRange()}" in APP


# 5. the map is one scan and still leaves out hidden photos
def test_map_leaves_out_hidden(client, auth, lib_on):
    import server
    ids = _ids(client, auth)
    _geo(server, ids)
    client.post("/ui/hide", json={"id": ids[0], "hidden": True}, headers=auth)
    m = client.get("/ui/map", headers=auth).get_json()
    assert ids[0] not in m["id"] and len(m["id"]) == len(ids) - 1


# 6. dates in the user's own day
def test_local_day_is_local():
    got = _node(_js("localDay") + 'console.log(JSON.stringify([localDay(new Date(2026, 0, 5, 23, 59)), localDay(new Date(2026, 11, 31, 0, 1))]))')
    assert got == ["2026-01-05", "2026-12-31"]


def test_no_utc_today_left_in_people():
    assert "toISOString().slice(0, 10)" not in _js("personBar")


# 7/K. booleans are booleans
@pytest.mark.parametrize("field", ["isFavorite", "isHidden", "hidden"])
def test_person_flags_reject_strings(people, client, auth, field):  # noqa: F811
    pid = client.get("/ui/people", headers=auth).get_json()[0]["id"]
    assert client.put(f"/ui/people/{pid}", json={field: "false"}, headers=auth).status_code == 400
    assert client.put(f"/ui/people/{pid}", json={field: False}, headers=auth).status_code == 200


# 8. paused slideshow keeps its controls
def test_paused_slideshow_never_idles():
    assert "!slides.paused" in _js("slideIdle") and "slideIdle()" in _js("pauseSlides")


# 9. the ratio still follows rotation after the cheaper query
def test_ratio_with_rotation_query(client, auth, lib_on):
    import server
    ids = _ids(client, auth)
    with server.app.app_context():
        c = server.connect()
        c.executemany("INSERT OR REPLACE INTO rotations VALUES(?, 270)", [(i,) for i in ids])
        c.commit()
    r = sum(b["ratio"] for b in client.get("/api/timeline/buckets", headers=auth).get_json())
    assert abs(r - len(ids) * 300 / 400) < 0.02


# 10. the ThumbHash cache drops its oldest entry, not everything
def test_thumbhash_cache_evicts_oldest():
    src = _js("thumbHashURL")
    assert "thCache.clear()" not in src and "thCache.keys().next().value" in src


def test_birth_date_round_trip_through_phone_api(people, client, auth):  # noqa: F811
    pid = client.get("/ui/people", headers=auth).get_json()[0]["id"]
    assert client.put(f"/api/people/{pid}", json={"birthDate": "1999-12-31"}, headers=auth).get_json()["birthDate"] == "1999-12-31"
    assert next(p for p in client.get("/api/people", headers=auth).get_json()["people"] if p["id"] == pid)["birthDate"] == "1999-12-31"
