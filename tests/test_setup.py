"""First run: with no user, the login screen creates one."""
import pytest


@pytest.fixture
def fresh(client, monkeypatch):
    monkeypatch.delenv("PHOTOS_PASSWORD")
    return client


def test_uninitialized_server_says_so_and_refuses_login(fresh):
    assert fresh.get("/api/server/config").get_json()["isInitialized"] is False
    assert fresh.post("/api/auth/login", json={"email": "a@b.c", "password": "whatever1"}).status_code == 503
    assert fresh.get("/api/albums").status_code == 401


def test_sign_up_then_login_and_only_once(fresh):
    r = fresh.post("/api/auth/admin-sign-up", json={"email": "me@home.lan", "password": "longenough1"})
    assert r.status_code == 201
    assert fresh.get("/api/server/config").get_json()["isInitialized"] is True
    assert fresh.post("/api/auth/login", json={"email": "me@home.lan", "password": "wrongwrong"}).status_code == 401
    ok = fresh.post("/api/auth/login", json={"email": "ME@home.lan", "password": "longenough1"})
    assert ok.status_code == 200 and ok.get_json()["userEmail"] == "me@home.lan"
    again = fresh.post("/api/auth/admin-sign-up", json={"email": "evil@x.io", "password": "takeover123"})
    assert again.status_code == 400  # the account cannot be taken over


def test_sign_up_validates_and_env_password_wins(fresh, monkeypatch):
    assert fresh.post("/api/auth/admin-sign-up", json={"email": "nope", "password": "longenough1"}).status_code == 400
    assert fresh.post("/api/auth/admin-sign-up", json={"email": "a@b.c", "password": "short"}).status_code == 400
    monkeypatch.setenv("PHOTOS_PASSWORD", "s3cret")
    assert fresh.post("/api/auth/admin-sign-up", json={"email": "a@b.c", "password": "longenough1"}).status_code == 400
