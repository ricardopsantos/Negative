"""PH1 / PH6: the endpoints the Immich app calls answer with the spec's shapes; writes are refused."""
import json
from pathlib import Path

SPEC = json.loads((Path(__file__).resolve().parent.parent / "docs/immich-openapi-v3.3.6.json").read_text())
SCHEMAS = SPEC["components"]["schemas"]


def required(name):
    return set(SCHEMAS[name].get("required", []))


def test_handshake_is_public_and_shaped(client):
    assert client.get("/api/server/ping").get_json() == {"res": "pong"}
    assert client.get("/api/server/version").get_json() == {"major": 3, "minor": 3, "patch": 6, "prerelease": 0}
    for path, schema in (("/api/server/about", "ServerAboutResponseDto"), ("/api/server/features", "ServerFeaturesDto"),
                         ("/api/server/config", "ServerConfigDto"), ("/api/server/media-types", "ServerMediaTypesResponseDto"),
                         ("/api/public/config", "PublicConfigDto")):
        body = client.get(path).get_json()
        assert required(schema) <= set(body), (path, required(schema) - set(body))


def test_login_and_user_shapes(client, auth):
    r = client.post("/api/auth/login", json={"email": "owner@example.com", "password": "s3cret"}).get_json()
    assert required("LoginResponseDto") <= set(r)
    me = client.get("/api/users/me", headers=auth).get_json()
    assert required("UserAdminResponseDto") <= set(me)
    assert client.post("/api/auth/validateToken", headers=auth).get_json() == {"authStatus": True}
    assert required("AuthStatusResponseDto") <= set(client.get("/api/auth/status", headers=auth).get_json())


def test_timeline_and_album_shapes(client, auth, lib):
    client.post("/ui/folders", json={"path": str(lib / "Trip 2019")}, headers=auth)
    b = client.get("/api/timeline/buckets", headers=auth).get_json()
    assert b and required("TimeBucketsResponseDto") <= set(b[0])
    col = client.get("/api/timeline/bucket?timeBucket=" + b[0]["timeBucket"], headers=auth).get_json()
    assert required("TimeBucketAssetResponseDto") <= set(col)
    assert len({len(v) for v in col.values()}) == 1  # columns are parallel arrays
    alb = client.get("/api/albums", headers=auth).get_json()
    assert required("AlbumResponseDto") <= set(alb[0])


def test_uploads_and_deletes_are_refused(client, auth):
    for m in (client.post, client.put, client.delete):
        r = m("/api/assets", headers=auth)
        assert r.status_code == 403 and "view-only" in r.get_json()["message"]


def test_web_page_and_favicon_are_served_without_login(client):
    assert b"<title>Negative" in client.get("/photos").data and client.get("/").status_code == 200
    r = client.get("/favicon.png")
    assert r.status_code == 200 and r.mimetype == "image/png" and r.data[:4] == b"\x89PNG"   # PH52


def test_ph53_well_known_points_the_app_at_api(client):
    r = client.get("/.well-known/immich")
    assert r.status_code == 200 and r.get_json() == {"api": {"endpoint": "/api"}}
