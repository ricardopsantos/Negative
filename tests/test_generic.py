# summary: The release defaults (step 3 of the open-source card): no personal default email or name; the name follows the account.


def test_name_follows_the_email(client, auth):
    me = client.get("/api/users/me", headers=auth).get_json()
    assert me["email"] == "owner@example.com" and me["name"] == "Owner"


def test_photos_name_wins(client, auth, monkeypatch):
    monkeypatch.setenv("PHOTOS_NAME", "Alice")
    assert client.get("/api/users/me", headers=auth).get_json()["name"] == "Alice"


def test_default_email_is_neutral(client, monkeypatch):
    monkeypatch.delenv("PHOTOS_EMAIL")
    import server
    assert server.DEFAULT_EMAIL == "admin@photos.local"
    r = client.post("/api/auth/login", json={"email": "admin@photos.local", "password": "s3cret"})
    assert r.status_code in (200, 201) and r.get_json()["name"] == "Admin"
