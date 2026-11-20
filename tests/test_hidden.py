"""Hidden photos: out of every list, behind a PIN (default 12341234), durable in hidden.json."""
import json

from conftest import album_assets
from test_favorites_trash import picked


def count(client, auth, q=""):
    return sum(x["count"] for x in client.get("/api/timeline/buckets?" + q, headers=auth).get_json())


def test_hidden_leaves_everything_and_needs_the_default_pin(client, auth, lib, tmp_path):
    a = picked(client, auth, lib)[0]
    alb = client.get("/api/albums", headers=auth).get_json()[0]
    client.post("/ui/favorite", json={"id": a["id"], "favorite": True}, headers=auth)
    assert client.post("/ui/hide", json={"id": a["id"], "hidden": True}, headers=auth).status_code == 200
    assert json.loads((tmp_path / "data" / "hidden.json").read_text()).popitem()[1]["name"] == a["originalFileName"]
    assert count(client, auth) == 1 and count(client, auth, "isFavorite=true") == 0
    assert client.get("/api/albums", headers=auth).get_json()[0]["assetCount"] == 1
    assert client.get("/api/timeline/buckets?hidden=true", headers=auth).status_code == 403
    for url in ("", "/thumbnail", "/original"):  # locked: the photo itself is refused
        assert client.get(f"/api/assets/{a['id']}{url}", headers=auth).status_code == 403
    assert client.post("/api/auth/session/unlock", json={"pinCode": "00000000"}, headers=auth).status_code == 400
    assert client.get("/api/auth/status", headers=auth).get_json()["isElevated"] is False
    assert client.post("/api/auth/session/unlock", json={"pinCode": "12341234"}, headers=auth).status_code == 204
    assert count(client, auth, "hidden=true") == 1 and count(client, auth, "visibility=locked") == 1
    assert client.get(f"/api/assets/{a['id']}/thumbnail", headers=auth).status_code == 200
    client.post("/api/auth/session/lock", headers=auth)
    assert client.get("/api/timeline/buckets?hidden=true", headers=auth).status_code == 403
    client.post("/api/auth/session/unlock", json={"pinCode": "12341234"}, headers=auth)
    client.post("/ui/hide", json={"id": a["id"], "hidden": False}, headers=auth)
    assert count(client, auth) == 2 and count(client, auth, "hidden=true") == 0


def test_pin_change_reset_and_lockout(client, auth):
    assert client.get("/api/auth/status", headers=auth).get_json()["pinIsDefault"] is True
    assert client.put("/api/auth/pin-code", json={"pinCode": "12341234", "newPinCode": "12"}, headers=auth).status_code == 400
    assert client.put("/api/auth/pin-code", json={"pinCode": "99999999", "newPinCode": "4321"}, headers=auth).status_code == 400
    assert client.put("/api/auth/pin-code", json={"pinCode": "12341234", "newPinCode": "4321"}, headers=auth).status_code == 204
    s = client.get("/api/auth/status", headers=auth).get_json()
    assert s["pinCode"] is True and s["pinIsDefault"] is False
    assert client.post("/api/auth/session/unlock", json={"pinCode": "12341234"}, headers=auth).status_code == 400  # old default is dead
    assert client.post("/api/auth/session/unlock", json={"pinCode": "4321"}, headers=auth).status_code == 204
    assert client.delete("/api/auth/pin-code", json={"password": "s3cret"}, headers=auth).status_code == 204  # password resets it
    assert client.get("/api/auth/status", headers=auth).get_json()["pinIsDefault"] is True
    codes = [client.post("/api/auth/session/unlock", json={"pinCode": "1"}, headers=auth).status_code for _ in range(7)]
    assert codes[-1] == 429
    assert client.post("/api/auth/session/unlock", json={"pinCode": "12341234"}, headers=auth).status_code == 429


def test_pin_chosen_at_sign_up(client, monkeypatch):
    monkeypatch.delenv("PHOTOS_PASSWORD")
    assert client.post("/api/auth/admin-sign-up", json={"email": "a@b.c", "password": "longenough1", "pinCode": "abc"}).status_code == 400
    assert client.post("/api/auth/admin-sign-up", json={"email": "a@b.c", "password": "longenough1", "pinCode": "5555"}).status_code == 201
    t = client.post("/api/auth/login", json={"email": "a@b.c", "password": "longenough1"}).get_json()["accessToken"]
    h = {"Authorization": "Bearer " + t}
    assert client.post("/api/auth/session/unlock", json={"pinCode": "12341234"}, headers=h).status_code == 400
    assert client.post("/api/auth/session/unlock", json={"pinCode": "5555"}, headers=h).status_code == 204


def test_immich_app_locked_folder_call(client, auth, lib):
    a = picked(client, auth, lib)[0]
    assert client.put("/api/assets", json={"ids": [a["id"]], "visibility": "locked"}, headers=auth).status_code == 204
    assert count(client, auth) == 1
    assert client.get(f"/api/assets/{a['id']}", headers=auth).status_code == 403


def test_hidden_photo_leaves_people_and_memories(client, auth, lib):
    import server
    a = picked(client, auth, lib)[0]
    c = server.connect()
    c.execute("INSERT INTO persons(id,name) VALUES('p1','Ann')")
    c.execute("INSERT INTO faces(asset_id,x,y,w,h,person_id) VALUES(?,0,0,.5,.5,'p1')", (a["id"],))
    c.commit(); c.close()
    assert [p["count"] for p in client.get("/ui/people", headers=auth).get_json()] == [1]
    client.post("/ui/hide", json={"id": a["id"], "hidden": True}, headers=auth)
    assert client.get("/ui/people", headers=auth).get_json() == []


def test_qr_code_signs_a_device_in_once(client, auth):
    r = client.post("/ui/qr", json={"origin": "http://localhost"}, headers=auth)
    assert r.status_code == 200 and r.get_json()["svg"].startswith("<svg")
    code = r.get_json()["url"].rsplit("/", 1)[1]
    client.delete_cookie("photos_session")
    ok = client.post("/api/auth/qr-login", json={"code": code})
    assert ok.status_code == 200 and client.get("/api/users/me", headers={"Authorization": "Bearer " + ok.get_json()["accessToken"]}).status_code == 200
    assert client.post("/api/auth/qr-login", json={"code": code}).status_code == 401  # one use only
    assert client.post("/ui/qr", json={"origin": "https://evil.example"}, headers=auth).status_code == 400
    import server
    server._qr_codes.clear(); server._fails.clear()
    code = client.post("/ui/qr", json={"origin": "http://localhost"}, headers=auth).get_json()["url"].rsplit("/", 1)[1]
    for k in server._qr_codes: server._qr_codes[k] = 0  # expired
    assert client.post("/api/auth/qr-login", json={"code": code}).status_code == 401
    server._fails.clear()


def test_failed_attempts_are_logged_for_fail2ban(client, tmp_path):
    client.post("/api/auth/login", json={"email": "x@y.z", "password": "nope"})
    client.post("/api/auth/qr-login", json={"code": "bad"})
    log = (tmp_path / "data" / "auth.log").read_text()
    assert "photos-auth FAIL ip=" in log and "what=login" in log and "what=qr" in log
    import re
    assert re.search(r"photos-auth FAIL ip=(\S+) what=", log).group(1) == "127.0.0.1"
