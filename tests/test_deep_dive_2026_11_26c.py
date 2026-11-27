"""PH126: third deep dive of 2026-11-26 — 20 fixes, each with its tests, plus coverage around them."""
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from test_api_conformance import check, schema_of
from test_people import people  # noqa: F401  (fixture)

APP = (Path(__file__).parent.parent / "app.js").read_text()
OLD = datetime(2015, 1, 1).timestamp()  # an mtime that never agrees with a folder date


@pytest.fixture
def srv(client):
    import server
    return server


@pytest.fixture
def lib_on(client, auth, lib):
    client.post("/ui/folders", json={"path": str(lib)}, headers=auth).get_data()


def _ids(client, auth):
    out = []
    for b in client.get("/api/timeline/buckets", headers=auth).get_json():
        out += client.get(f"/api/timeline/bucket?timeBucket={b['timeBucket']}", headers=auth).get_json()["id"]
    return out


def _geo(srv, rows):
    with srv.app.app_context():
        c = srv.connect()
        c.executemany("INSERT OR REPLACE INTO geo(asset_id, mtime, lat, lon, city, region, cc) VALUES(?,0,?,?,?,?,?)", rows)
        c.commit()


def _spec_ok(client, auth, method, url, body=None):
    r = client.open(url, method=method, json=body, headers=auth)
    code, schema = schema_of(method.lower(), url)
    assert r.status_code == code
    errs = []
    check(r.get_json(), schema, url, errs)
    assert not errs, errs
    return r.get_json()


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


# 1. day-and-month folder names (the library's own "2011 - 17 Set - Trip")
@pytest.mark.parametrize("folder,day", [
    ("2011 - 17 Set - Despedida Tiago", "2011-09-17"), ("2019 - 3 Dez - Natal", "2019-12-03"),
    ("2020 - 01 Fev", "2020-02-01"), ("2018-25 Out Lisboa", "2018-10-25"), ("2016 07 August trip", "2016-08-07"),
    ("2015 - 29 Abr", "2015-04-29"), ("2014 - 15 Mai", "2014-05-15"), ("2013 - 2 Ago", "2013-08-02")])
def test_day_month_folder_names(srv, lib, folder, day):
    d = lib / "Timeline" / folder[:4] / folder
    d.mkdir(parents=True)
    with srv.app.app_context():
        got, src = srv.name_date(d / "photo.jpg", OLD)
    assert (got[:10], src) == (day, "folder")


@pytest.mark.parametrize("folder", ["2011 - 31 Fev", "2011 - 17 Foo", "2011 - 40 Set"])
def test_impossible_day_month_falls_back(srv, lib, folder):
    d = lib / folder
    d.mkdir()
    with srv.app.app_context():
        got, src = srv.name_date(d / "photo.jpg", OLD)
    assert (got[:7], src) == ("2011-07", "folder")  # the year alone, as before


def test_index_version_bumped_so_old_rows_are_redated(srv):
    assert srv.INDEX_VER >= 4


def test_day_month_folder_dates_a_scanned_photo(client, auth, lib):
    d = lib / "Timeline" / "2011" / "2011 - 17 Set - Trip"
    d.mkdir(parents=True)
    Image.new("RGB", (40, 30)).save(d / "a.jpg")
    os.utime(d / "a.jpg", (OLD, OLD))
    client.post("/ui/folders", json={"path": str(lib / "Timeline")}, headers=auth).get_data()
    assert [b["timeBucket"][:7] for b in client.get("/api/timeline/buckets", headers=auth).get_json()] == ["2011-09"]


# 2. the phone's map
def test_map_markers_list_photos_with_gps(client, auth, lib_on, srv):
    ids = _ids(client, auth)
    _geo(srv, [(i, 38.7, -9.1, "Lisbon", "Lisboa", "PT") for i in ids[:2]])
    got = _spec_ok(client, auth, "GET", "/api/map/markers")
    assert sorted(m["id"] for m in got) == sorted(ids[:2]) and got[0]["city"] == "Lisbon" and got[0]["state"] == "Lisboa"


def test_map_markers_leave_out_hidden(client, auth, lib_on, srv):
    ids = _ids(client, auth)
    _geo(srv, [(i, 38.7, -9.1, "Lisbon", "", "PT") for i in ids])
    client.post("/ui/hide", json={"id": ids[0], "hidden": True}, headers=auth)
    assert ids[0] not in [m["id"] for m in client.get("/api/map/markers", headers=auth).get_json()]


def test_map_markers_filters(client, auth, lib_on, srv):
    ids = _ids(client, auth)
    _geo(srv, [(i, 38.7, -9.1, "Lisbon", "", "PT") for i in ids])
    client.post("/ui/favorite", json={"id": ids[0], "favorite": True}, headers=auth)
    assert [m["id"] for m in client.get("/api/map/markers?isFavorite=true", headers=auth).get_json()] == [ids[0]]
    assert client.get("/api/map/markers?fileCreatedBefore=1990-01-01T00:00:00.000Z", headers=auth).get_json() == []
    assert len(client.get("/api/map/markers?fileCreatedAfter=1990-01-01T00:00:00.000Z", headers=auth).get_json()) == len(ids)


def test_map_markers_empty_library(client, auth):
    assert client.get("/api/map/markers", headers=auth).get_json() == []


# 3. one photo per city
def test_search_cities_one_per_city(client, auth, lib_on, srv):
    ids = _ids(client, auth)
    _geo(srv, [(ids[0], 1, 1, "Porto", "", "PT"), (ids[1], 1, 1, "Porto", "", "PT"), (ids[2], 2, 2, "Braga", "", "PT")])
    got = _spec_ok(client, auth, "GET", "/api/search/cities")
    assert len(got) == 2 and {a["id"] for a in got} <= set(ids)


def test_search_cities_skip_hidden_only_city(client, auth, lib_on, srv):
    ids = _ids(client, auth)
    _geo(srv, [(ids[0], 1, 1, "Faro", "", "PT")])
    client.post("/ui/hide", json={"id": ids[0], "hidden": True}, headers=auth)
    assert client.get("/api/search/cities", headers=auth).get_json() == []


# 4. place search
def test_search_places_by_name(client, auth, lib_on, srv):
    ids = _ids(client, auth)
    _geo(srv, [(ids[0], 41.1, -8.6, "Porto", "Norte", "PT"), (ids[1], 38.7, -9.1, "Lisbon", "", "PT")])
    got = _spec_ok(client, auth, "GET", "/api/search/places?name=por")
    assert [p["name"] for p in got] == ["Porto"] and got[0]["admin1name"] == "Norte"


@pytest.mark.parametrize("q", ["", "%", "_", "zzz"])
def test_search_places_odd_names_match_nothing(client, auth, lib_on, srv, q):
    _geo(srv, [(_ids(client, auth)[0], 1, 1, "Porto", "", "PT")])
    assert client.get(f"/api/search/places?name={q}", headers=auth).get_json() == []


# 5. trash days are rounded up
def test_trash_days_left_rounded_up(client, auth, lib_on, srv, monkeypatch):
    aid = _ids(client, auth)[0]
    client.post("/ui/assets/delete", json={"ids": [aid], "confirm": True}, headers=auth)
    real = time.time
    monkeypatch.setattr(srv.time, "time", lambda: real() + 29.5 * 86400)
    assert client.get("/ui/trash", headers=auth).get_json()[0]["daysLeft"] == 1
    monkeypatch.setattr(srv.time, "time", lambda: real() + 0.1 * 86400)
    assert client.get("/ui/trash", headers=auth).get_json()[0]["daysLeft"] == 30


# 6. sync lines are sent in chunks
def test_sync_stream_chunked_but_line_per_entity(client, auth, lib_on):
    r = client.post("/api/sync/stream", json={"types": ["AssetsV1", "AssetExifsV1"]}, headers=auth)
    chunks = list(r.response)
    lines = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks).decode().splitlines()
    assert all(json.loads(x)["ack"] for x in lines)
    assert len(chunks) < len(lines)
    assert json.loads(lines[-1])["type"] == "SyncCompleteV1"


# 7. the sync shape was bumped for the ThumbHash
def test_sync_shape_bumped(srv):
    assert srv.SYNC_SHAPE >= 6


# 8. /api/people computes the hidden list once
def test_api_people_hidden_counted_once(people, client, auth, monkeypatch):  # noqa: F811
    calls = []
    real = people.people_rows
    monkeypatch.setattr(people, "people_rows", lambda *a, **k: (calls.append(k.get("hidden", a[0] if a else False)), real(*a, **k))[1])
    client.get("/api/people?withHidden=true", headers=auth)
    assert calls.count(True) == 1


# 9 / 10. hide and favorite take real booleans
@pytest.mark.parametrize("url,key", [("/ui/hide", "hidden"), ("/ui/favorite", "favorite")])
@pytest.mark.parametrize("bad", ["false", "true", 1, None, [], {}])
def test_marks_need_booleans(client, auth, lib_on, url, key, bad):
    if bad is None:
        pytest.skip("null is 'not given'")
    aid = _ids(client, auth)[0]
    assert client.post(url, json={"id": aid, key: bad}, headers=auth).status_code == 400


def test_marks_still_work_with_booleans(client, auth, lib_on):
    aid = _ids(client, auth)[0]
    assert client.post("/ui/favorite", json={"id": aid, "favorite": True}, headers=auth).status_code == 200
    assert client.get("/ui/counts", headers=auth).get_json()["favorites"] == 1


# 11. square placeholders count the gaps
def test_square_placeholder_counts_gaps():
    got = _node("const justified = () => false;\n" + _js("estHeight") + "console.log(JSON.stringify(estHeight({count: 10}, {cols: 4, w: 400, gap: 8})))")
    assert abs(got - 3 * ((400 - 3 * 8) / 4 + 8)) < 1e-6


# 12. a turned photo takes its new shape
def test_rotate_updates_tile_shape():
    src = _js("rotate")
    assert "1 / clampR(m.ratio[pos.k])" in src and "layoutMonth(pos.i)" in src


# 13. trash lookups by map
def test_trash_render_uses_map():
    assert "trashItems.find" not in _js("moreTrash") and "new Map(part.map" in _js("moreTrash")


# 14. Explore asks for 14 people
def test_people_limit(people, client, auth):  # noqa: F811
    assert len(client.get("/ui/people?limit=0", headers=auth).get_json()) == 0
    assert len(client.get("/ui/people?limit=1", headers=auth).get_json()) == 1
    assert len(client.get("/ui/people?limit=x", headers=auth).get_json()) >= 1
    assert "/ui/people?limit=14" in _js("renderExplore")


# 15. the full send computes the fingerprint parts once
def test_full_sync_reads_parts_once(client, auth, lib_on, srv, monkeypatch):
    calls = []
    real = srv._sync_parts
    monkeypatch.setattr(srv, "_sync_parts", lambda c: (calls.append(1), real(c))[1])
    client.post("/api/sync/stream", json={"types": ["AssetsV1"], "reset": True}, headers=auth).get_data()
    assert len(calls) == 1


def test_unchanged_sync_is_complete_only(client, auth, lib_on):
    r = client.post("/api/sync/stream", json={"types": ["AssetsV1"]}, headers=auth).get_data(as_text=True).splitlines()
    done = json.loads(r[-1])["ack"]
    client.post("/api/sync/ack", json={"acks": [done]}, headers=auth)
    again = client.post("/api/sync/stream", json={"types": ["AssetsV1"]}, headers=auth).get_data(as_text=True).splitlines()
    assert [json.loads(x)["type"] for x in again] == ["SyncCompleteV1"]


# 16. files added as the last upload ends are sent
def test_upload_queue_restarts():
    assert "upQ.length && upRun && !upRun.cancelled" in _js("finishUp")


# 17. the backfill reads two columns
def test_backfill_reads_only_what_it_needs(srv):
    import inspect
    assert "SELECT id, mtime FROM assets" in inspect.getsource(srv.thumbhash_pass)


# 18. file name search is literal
def test_file_name_search_is_literal(client, auth, lib, srv):
    d = lib / "names"
    d.mkdir()
    for n in ("IMG_1.jpg", "IMGX1.jpg", "100%.jpg"):
        Image.new("RGB", (20, 20)).save(d / n)
    client.post("/ui/folders", json={"path": str(d)}, headers=auth).get_data()
    s = lambda q: sorted(a["originalFileName"] for a in client.post("/api/search/metadata", json={"originalFileName": q}, headers=auth).get_json()["assets"]["items"])
    assert s("IMG_1") == ["IMG_1.jpg"]
    assert s("100%") == ["100%.jpg"]
    assert s("img") == ["IMGX1.jpg", "IMG_1.jpg"]


# 19. people search means all of them
def test_person_search_needs_everyone(people, client, auth):  # noqa: F811
    with people.app.app_context():
        c = people.connect()
        pids = [r[0] for r in c.execute("SELECT id FROM persons ORDER BY n DESC")]
        assert len(pids) >= 2
        a_assets = {r[0] for r in c.execute("SELECT asset_id FROM faces WHERE person_id=?", (pids[0],))}
        b_assets = {r[0] for r in c.execute("SELECT asset_id FROM faces WHERE person_id=?", (pids[1],))}
        c.close()
    ids = lambda p: {a["id"] for a in client.post("/api/search/metadata", json={"personIds": p}, headers=auth).get_json()["assets"]["items"]}
    assert ids([pids[0]]) == a_assets
    assert ids([pids[0], pids[1]]) == a_assets & b_assets
    assert ids([pids[0], pids[0]]) == a_assets   # the same person twice is once


# 20. the digest is still a fingerprint of the rows
def test_digest_changes_with_rows(srv):
    assert srv._digest([("a", 1)]) != srv._digest([("a", 2)])
    assert srv._digest([("a", 1), ("b", 2)]) != srv._digest([("a", 1, "b", 2)])
    assert srv._digest([]) == srv._digest([])
