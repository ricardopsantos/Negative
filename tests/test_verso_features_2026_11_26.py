"""PH117-PH123: features taken from the Verso client review (2026-11-26)."""
import base64
import re
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import thumbhash
from test_people import people  # noqa: F401  (fixture)

APP = (Path(__file__).parent.parent / "app.js").read_text()


@pytest.fixture
def lib_on(client, auth, lib):
    r = client.post("/ui/folders", json={"path": str(lib)}, headers=auth)
    assert r.status_code in (200, 201, 202)
    r.get_data()  # the scan streams its progress; reading it to the end waits for it


def _decode_avg(b64):
    """Average colour from a hash (the reference decoder's DC terms)."""
    h = base64.b64decode(b64)
    h24 = h[0] | (h[1] << 8) | (h[2] << 16)
    l, p, q = (h24 & 63) / 63, ((h24 >> 6) & 63) / 31.5 - 1, ((h24 >> 12) & 63) / 31.5 - 1
    b = l - 2 / 3 * p
    r = (3 * l - b + q) / 2
    return [round(255 * min(1, max(0, v))) for v in (r, r - q, b)]


def test_ph117_thumbhash_encodes_like_the_reference():
    # checked byte for byte against the reference (pip thumbhash 0.1.2) on 30 random images when written
    im = Image.new("RGB", (4, 3), (200, 100, 50))
    h = thumbhash.encode(im)
    assert len(h) >= 5 and h[4] >> 7 == 1          # landscape flag
    assert all(abs(a - b) <= 6 for a, b in zip(_decode_avg(thumbhash.encode_b64(im)), (200, 100, 50)))
    big = Image.fromarray((np.add.outer(np.arange(300), np.arange(400)) % 256).astype(np.uint8)).convert("RGB")
    assert len(thumbhash.encode(big)) == len(thumbhash.encode(big.resize((100, 75))))  # bigger images are shrunk, not refused
    tall = thumbhash.encode(Image.new("RGB", (30, 90), (0, 0, 0)))
    assert tall[4] >> 7 == 0


def test_ph117_bucket_carries_thumbhash_after_the_tile_is_made(client, auth, lib_on):
    b = client.get("/api/timeline/buckets", headers=auth).get_json()
    col = client.get(f"/api/timeline/bucket?timeBucket={b[0]['timeBucket']}", headers=auth).get_json()
    assert all(h is None for h in col["thumbhash"])
    assert client.get(f"/api/assets/{col['id'][0]}/thumbnail", headers=auth).status_code == 200
    col = client.get(f"/api/timeline/bucket?timeBucket={b[0]['timeBucket']}", headers=auth).get_json()
    h = col["thumbhash"][0]
    assert h and abs(_decode_avg(h)[2] - 160) <= 12   # the fixture photos are blue-ish (b=160)


def test_ph117_backfill_pass_hashes_existing_tiles(client, auth, lib_on):
    import server
    b = client.get("/api/timeline/buckets", headers=auth).get_json()
    col = client.get(f"/api/timeline/bucket?timeBucket={b[0]['timeBucket']}", headers=auth).get_json()
    client.get(f"/api/assets/{col['id'][0]}/thumbnail", headers=auth)
    with server.app.app_context():
        server.connect().execute("DELETE FROM thumbhash").connection.commit()
    assert server.thumbhash_pass() >= 1
    assert server.thumbhash_pass() == 0          # nothing left to do
    col = client.get(f"/api/timeline/bucket?timeBucket={b[0]['timeBucket']}", headers=auth).get_json()
    assert col["thumbhash"][0]


def test_ph118_month_buckets_carry_the_sum_of_shapes(client, auth, lib_on):
    b = client.get("/api/timeline/buckets", headers=auth).get_json()
    assert sum(x["count"] for x in b) == 3
    assert abs(sum(x["ratio"] for x in b) - 3 * 400 / 300) < 0.05   # three 400x300 photos


def test_ph118_web_app_lays_out_justified_rows_and_reads_ahead():
    assert "function justRows(" in APP and 'localStorage.photos_layout !== "square"' in APP
    assert 'rootMargin: "300% 0px"' in APP
    assert re.search(r'addEventListener\("resize".*120\)', APP)


def test_ph119_json_answers_304_when_unchanged(client, auth, lib_on):
    r = client.get("/api/timeline/buckets", headers=auth)
    tag = r.headers["ETag"]
    assert "no-cache" in r.headers["Cache-Control"]
    r2 = client.get("/api/timeline/buckets", headers={**auth, "If-None-Match": tag})
    assert r2.status_code == 304 and not r2.data
    # a favorite changes the bucket, so the old tag no longer matches
    tb = r.get_json()[0]["timeBucket"]
    col = client.get(f"/api/timeline/bucket?timeBucket={tb}", headers=auth)
    client.put("/api/assets", json={"ids": [col.get_json()["id"][0]], "isFavorite": True}, headers=auth)
    r3 = client.get(f"/api/timeline/bucket?timeBucket={tb}", headers={**auth, "If-None-Match": col.headers["ETag"]})
    assert r3.status_code == 200


def test_ph120_map_points_carry_their_day(client, auth, lib_on, monkeypatch):
    import server
    with server.app.app_context():
        c = server.connect()
        aid = c.execute("SELECT id FROM assets LIMIT 1").fetchone()[0]
        c.execute("INSERT OR REPLACE INTO geo(asset_id, mtime, lat, lon) VALUES(?, 0, 38.7, -9.1)", (aid,))
        c.commit()
    m = client.get("/ui/map", headers=auth).get_json()
    assert len(m["day"]) == len(m["id"]) == 1 and re.fullmatch(r"\d{4}-\d\d-\d\d", m["day"][0])
    assert 'id="mapfrom"' in APP and 'id="mapto"' in APP


def test_ph121_birthday_and_favorite_person(people, client, auth):  # noqa: F811
    pid = client.get("/ui/people", headers=auth).get_json()[0]["id"]
    assert client.put(f"/ui/people/{pid}", json={"birthDate": "2010-05-04", "isFavorite": True}, headers=auth).status_code == 200
    p = next(x for x in client.get("/ui/people", headers=auth).get_json() if x["id"] == pid)
    assert p["birth"] == "2010-05-04" and p["fav"] is True
    assert [x["id"] for x in client.get("/ui/people?fav=1", headers=auth).get_json()] == [pid]
    dto = client.get(f"/api/people/{pid}", headers=auth).get_json()
    assert dto["birthDate"] == "2010-05-04" and dto["isFavorite"] is True
    for bad in ("2999-01-01", "05/04/2010", "1700-01-01"):
        assert client.put(f"/api/people/{pid}", json={"birthDate": bad}, headers=auth).status_code == 400
    assert client.put(f"/ui/people/{pid}", json={"name": "Ann"}, headers=auth).get_json()["birthDate"] == "2010-05-04"  # kept
    assert client.put(f"/ui/people/{pid}", json={"birthDate": None}, headers=auth).get_json()["birthDate"] is None
    assert "function ageAt(" in APP and 'id="pfavs"' in APP


def test_ph122_dot_toggles_favorite():
    assert 'e.key === "."' in APP and "async function favTile(" in APP


def test_ph123_slideshow_controls_fade_after_2_5_s():
    assert "function slideIdle(" in APP and "2500" in APP
