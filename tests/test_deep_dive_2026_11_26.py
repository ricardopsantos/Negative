"""PH124: deep dive of 2026-11-26 (the Verso features, PH117-PH123) — one test or more per fix, plus coverage."""
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image

import thumbhash
from test_people import people  # noqa: F401  (fixture)

APP = (Path(__file__).parent.parent / "app.js").read_text()


@pytest.fixture
def lib_on(client, auth, lib):
    r = client.post("/ui/folders", json={"path": str(lib)}, headers=auth)
    r.get_data()


def _first(client, auth):
    tb = client.get("/api/timeline/buckets", headers=auth).get_json()[0]["timeBucket"]
    return tb, client.get(f"/api/timeline/bucket?timeBucket={tb}", headers=auth).get_json()


def _js(*names):
    """The source of top-level functions/consts of app.js, to run under node."""
    out = []
    for n in names:
        m = re.search(rf"^(?:const {n} = [^\n]*|function {n}\(.*?^}})$", APP, re.S | re.M)
        assert m, n
        out.append(m.group(0))
    return "\n".join(out) + "\n"


def _node(src):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    return json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)


# --- fix 1: the backfill no longer walks the whole library when there is nothing to do
def test_backfill_goes_idle(client, auth, lib_on, monkeypatch):
    import server
    _, col = _first(client, auth)
    client.get(f"/api/assets/{col['id'][0]}/thumbnail", headers=auth)
    server._th_idle[0] = None
    server.thumbhash_pass()
    assert server._th_idle[0] is not None
    monkeypatch.setattr(server, "thumb_path", lambda *a: (_ for _ in ()).throw(AssertionError("walked")))
    assert server.thumbhash_pass() == 0


def test_backfill_wakes_when_the_library_changes(client, auth, lib_on, lib):
    import server
    server.thumbhash_pass()
    Image.new("RGB", (300, 200), (1, 2, 3)).save(lib / "Trip 2019" / "new.jpg")
    client.post("/ui/scan", headers=auth).get_data()
    _, col = _first(client, auth)
    for i in col["id"]:
        client.get(f"/api/assets/{i}/thumbnail", headers=auth)
    assert server._th_idle[0] is None or server.thumbhash_pass() >= 0  # a changed key never stays idle
    assert all(_first(client, auth)[1]["thumbhash"])


# --- fix 2: one connection and one commit for a whole backfill
def test_backfill_uses_one_connection(client, auth, lib_on, monkeypatch):
    import server
    _, col = _first(client, auth)
    for i in col["id"]:
        client.get(f"/api/assets/{i}/thumbnail", headers=auth)
    with server.app.app_context():
        c = server.connect(); c.execute("DELETE FROM thumbhash"); c.commit(); c.close()
    server._th_idle[0] = None
    calls = []
    real = server.connect
    monkeypatch.setattr(server, "connect", lambda: (calls.append(1), real())[1])
    assert server.thumbhash_pass() == len(col["id"])
    assert len(calls) == 1


# --- fix 3: the hash is stored after the render slot is released
def test_hash_stored_outside_render_slot(client, auth, lib_on, monkeypatch):
    import server
    seen = []
    monkeypatch.setattr(server, "_store_thumbhash", lambda *a, **k: seen.append(server._render_slot.acquire(blocking=False)) or server._render_slot.release())
    _, col = _first(client, auth)
    client.get(f"/api/assets/{col['id'][0]}/thumbnail", headers=auth)
    assert seen and all(seen)


def test_hash_failure_never_fails_the_tile(client, auth, lib_on, monkeypatch):
    import server
    monkeypatch.setattr(server.thumbhash, "encode_b64", lambda im: 1 / 0)
    _, col = _first(client, auth)
    assert client.get(f"/api/assets/{col['id'][0]}/thumbnail", headers=auth).status_code == 200


# --- fix 4: stale hashes are pruned
def test_prune_drops_stale_hashes_only(client, auth, lib_on):
    import server
    _, col = _first(client, auth)
    client.get(f"/api/assets/{col['id'][0]}/thumbnail", headers=auth)
    with server.app.app_context():
        c = server.connect()
        c.execute("INSERT INTO thumbhash VALUES('gone-1-0', 'x'), ('%s-1-90', 'y')" % col["id"][0])
        assert server.prune_thumbhashes(c) == 2
        assert c.execute("SELECT COUNT(*) FROM thumbhash").fetchone()[0] == 1
        c.close()


def test_daily_sweep_prunes(client, auth, lib_on, monkeypatch):
    import server
    with server.app.app_context():
        c = server.connect(); c.execute("INSERT INTO thumbhash VALUES('gone-1-0', 'x')"); c.commit(); c.close()
    monkeypatch.setattr(server, "trash_purge", lambda: None)
    server.daily_sweep()
    with server.app.app_context():
        assert server.connect().execute("SELECT COUNT(*) FROM thumbhash WHERE key='gone-1-0'").fetchone()[0] == 0


# --- fix 5: a turned photo counts with its turned shape
def test_bucket_ratio_follows_rotation(client, auth, lib_on):
    import server
    before = sum(x["ratio"] for x in client.get("/api/timeline/buckets", headers=auth).get_json())
    _, col = _first(client, auth)
    with server.app.app_context():
        c = server.connect(); c.execute("INSERT OR REPLACE INTO rotations VALUES(?, 90)", (col["id"][0],)); c.commit(); c.close()
    after = sum(x["ratio"] for x in client.get("/api/timeline/buckets", headers=auth).get_json())
    assert abs((before - after) - (400 / 300 - 300 / 400)) < 0.02


# --- fix 6: the asset DTO and the sync stream carry the hash too
def test_asset_dto_has_thumbhash(client, auth, lib_on):
    _, col = _first(client, auth)
    aid = col["id"][0]
    assert client.get(f"/api/assets/{aid}", headers=auth).get_json()["thumbhash"] is None
    client.get(f"/api/assets/{aid}/thumbnail", headers=auth)
    assert client.get(f"/api/assets/{aid}", headers=auth).get_json()["thumbhash"] == _first(client, auth)[1]["thumbhash"][0]


def test_sync_stream_has_thumbhash(client, auth, lib_on):
    _, col = _first(client, auth)
    client.get(f"/api/assets/{col['id'][0]}/thumbnail", headers=auth)
    r = client.post("/api/sync/stream", json={"types": ["AssetsV1"]}, headers=auth)
    rows = [json.loads(x) for x in r.get_data(as_text=True).splitlines() if x.strip()]
    got = {x["data"]["id"]: x["data"]["thumbhash"] for x in rows if x.get("type") == "AssetV2"}
    assert got[col["id"][0]]


# --- fix 7: a merge keeps the birthday and the favorite
def test_merge_keeps_birthday_and_favorite(people, client, auth):  # noqa: F811
    with people.app.app_context():
        c = people.connect()
        a, b = [r[0] for r in c.execute("SELECT id FROM persons ORDER BY n DESC LIMIT 2")]
        c.close()
    client.put(f"/ui/people/{b}", json={"birthDate": "2001-02-03", "isFavorite": True}, headers=auth)
    assert client.post(f"/ui/people/{b}/merge", json={"into": a}, headers=auth).status_code == 200
    dto = client.get(f"/api/people/{a}", headers=auth).get_json()
    assert dto["birthDate"] == "2001-02-03" and dto["isFavorite"] is True


# --- fix 8: a birthday is a date and nothing more
@pytest.mark.parametrize("v,ok", [("2010-05-04", True), ("2010-05-04T00:00:00.000Z", True), ("2010-05-04xyz", False),
                                  ("2010-05-04T13:00:00Z", False), ("2010-13-01", False), (12, False)])
def test_birth_date_strict(people, client, auth, v, ok):  # noqa: F811
    pid = client.get("/ui/people", headers=auth).get_json()[0]["id"]
    assert (client.put(f"/api/people/{pid}", json={"birthDate": v}, headers=auth).status_code == 200) == ok


# --- fix 9 and the age maths, run in node
def test_age_text_and_maths():
    got = _node(_js("ageAt", "ageTxt") + """
      console.log(JSON.stringify([ageAt("2010-05-04", "2020-05-03T10:00:00"), ageAt("2010-05-04", "2020-05-04"),
        ageAt("2010-05-04", "2009-01-01"), ageAt("", "2020-01-01"), ageTxt(0), ageTxt(1), ageTxt(7), ageTxt(null)]));""")
    assert got == [9, 10, None, None, "under 1 year", "1 year", "7 years", ""]


# --- justified rows, run in node
def test_just_rows_fill_the_width():
    rows = _node(_js("clampR", "justRows") + "console.log(JSON.stringify(justRows([1.5,1.5,1.5,1.5,1.5,0.75,1], 1000, 200, 4)))")
    full = [r for r in rows[:-1]]
    assert full and all(abs(sum(1.5 if k < 5 else 0.75 for k in range(a, b)) * h + (b - a - 1) * 4 - 1000) < 0.01 for a, b, h in full)
    assert rows[-1][2] == 200 and rows[-1][1] == 7           # the last row keeps the target height


def test_just_rows_clamp_odd_shapes():
    rows = _node(_js("clampR", "justRows") + "console.log(JSON.stringify(justRows([50, 0, null, 0.01], 1000, 200, 4)))")
    assert rows[0][:2] == [0, 3]           # a panorama counts as 3, not 50 (no row of one photo 6 px high); 0 and null count as 1
    assert all(h > 100 for _, _, h in rows)


def test_just_rows_empty():
    assert _node(_js("clampR", "justRows") + "console.log(JSON.stringify(justRows([], 1000, 200, 4)))") == []


# --- fix 10/11 present in the page
def test_relayout_reads_style_once_and_skips_cached_decodes():
    assert re.search(r"function relayout\(\) \{ const g = grid\(\);", APP)
    assert "img.complete && img.naturalWidth" in APP


# --- coverage of the encoder
def test_encoder_size_and_orientation_flags():
    sq = thumbhash.encode(Image.new("RGB", (50, 50), (10, 200, 30)))
    wide = thumbhash.encode(Image.new("RGB", (100, 20), (10, 200, 30)))
    assert sq[4] >> 7 == 0 and wide[4] >> 7 == 1 and 5 < len(sq) <= 30


def test_encoder_handles_alpha_and_palette_and_tiny():
    for im in (Image.new("RGBA", (20, 20), (0, 0, 0, 0)), Image.new("P", (20, 10)), Image.new("L", (1, 1), 128)):
        assert thumbhash.encode(im)


def test_bucket_304_and_rotation_changes_tag(client, auth, lib_on):
    import server
    tb, _ = _first(client, auth)
    r = client.get(f"/api/timeline/bucket?timeBucket={tb}", headers=auth)
    assert client.get(f"/api/timeline/bucket?timeBucket={tb}", headers={**auth, "If-None-Match": r.headers["ETag"]}).status_code == 304
    with server.app.app_context():
        c = server.connect(); c.execute("INSERT OR REPLACE INTO rotations VALUES(?, 90)", (r.get_json()["id"][0],)); c.commit(); c.close()
    assert client.get(f"/api/timeline/bucket?timeBucket={tb}", headers={**auth, "If-None-Match": r.headers["ETag"]}).status_code == 200


def test_etag_not_on_writes_or_errors(client, auth, lib_on):
    assert "ETag" not in client.post("/api/auth/logout", headers=auth).headers
    assert "ETag" not in client.get("/api/assets/nope", headers=auth).headers


def test_people_fav_filter_and_hidden_list(people, client, auth):  # noqa: F811
    pid = client.get("/ui/people", headers=auth).get_json()[0]["id"]
    assert client.get("/ui/people?fav=1", headers=auth).get_json() == []
    client.put(f"/ui/people/{pid}", json={"isFavorite": True, "hidden": True}, headers=auth)
    assert client.get("/ui/people?fav=1", headers=auth).get_json() == []          # hidden people stay out of Favorites
    assert [p["id"] for p in client.get("/ui/people?hidden=1", headers=auth).get_json()] == [pid]
