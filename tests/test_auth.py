"""PH7: Immich-style login, fail closed, rate limit by last-hop client IP."""


def test_everything_else_needs_a_token(client):
    assert client.get("/api/users/me").status_code == 401
    assert client.get("/api/albums", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_login_accepts_bearer_and_immich_header(client):
    tok = client.post("/api/auth/login", json={"email": "owner@example.com", "password": "s3cret"}).get_json()["accessToken"]
    assert client.get("/api/users/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert client.get("/api/users/me", headers={"x-immich-user-token": tok}).status_code == 200
    client.post("/api/auth/logout", headers={"Authorization": f"Bearer {tok}"})
    assert client.get("/api/users/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_unset_password_fails_closed(client, monkeypatch):
    monkeypatch.delenv("PHOTOS_PASSWORD")
    assert client.post("/api/auth/login", json={"email": "owner@example.com", "password": ""}).status_code == 503


def test_lockout_after_five_failures_per_client_ip(client):
    bad = {"email": "owner@example.com", "password": "x"}
    for _ in range(5):
        assert client.post("/api/auth/login", json=bad).status_code == 401
    assert client.post("/api/auth/login", json=bad).status_code == 429
    # a different real client (last hop of X-Forwarded-For from a private peer) is not locked out
    other = client.post("/api/auth/login", json={"email": "owner@example.com", "password": "s3cret"},
                        headers={"X-Forwarded-For": "8.8.8.8, 100.64.0.9"}, environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
    assert other.status_code == 200


def test_folder_admin_endpoints_need_a_token(client, lib):
    assert client.post("/ui/folders", json={"path": str(lib)}).status_code == 401
    assert client.post("/ui/rescan").status_code == 401
