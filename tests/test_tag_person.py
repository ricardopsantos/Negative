"""PH49: tag a person who already has a name by clicking on the photo."""
import pytest

from test_people import FakeEngine, people  # noqa: F401  (fixture)
from conftest import album_assets


def _asset(client, auth, name):
    return next(a for a in album_assets(client, auth) if a["originalFileName"] == name)


def _named(people, client, auth):
    pid = next(p["id"] for p in client.get("/ui/people", headers=auth).get_json() if p["count"] >= 3)
    client.put(f"/ui/people/{pid}", json={"name": "Ana"}, headers=auth)
    return pid


def test_click_on_a_missed_face_adds_a_hand_placed_tag(client, auth, people):
    pid = _named(people, client, auth)
    a = _asset(client, auth, "none-1.jpg")
    r = client.post(f"/ui/assets/{a['id']}/faces", json={"personId": pid, "x": 0.5, "y": 0.5}, headers=auth)
    assert r.status_code == 200 and r.get_json()["moved"] is False
    fc = client.get(f"/ui/assets/{a['id']}/faces", headers=auth).get_json()
    assert [(f["personId"], f["name"]) for f in fc] == [(pid, "Ana")]
    with people.app.app_context():
        assert people.db().execute("SELECT COUNT(*) FROM face_manual").fetchone()[0] == 1
    # "not them" on a hand-placed face removes it
    client.post(f"/ui/faces/{fc[0]['faceId']}/not-this-person", headers=auth)
    assert client.get(f"/ui/assets/{a['id']}/faces", headers=auth).get_json() == []


def test_click_on_a_detected_face_gives_it_to_the_person(client, auth, people):
    pid = _named(people, client, auth)
    b = _asset(client, auth, "b-1.jpg")
    before = client.get(f"/ui/assets/{b['id']}/faces", headers=auth).get_json()
    assert len(before) == 1 and before[0]["personId"] != pid
    r = client.post(f"/ui/assets/{b['id']}/faces", json={"personId": pid, "x": 0.45, "y": 0.45}, headers=auth)
    assert r.status_code == 200 and r.get_json()["moved"] is True
    after = client.get(f"/ui/assets/{b['id']}/faces", headers=auth).get_json()
    assert len(after) == 1 and after[0]["personId"] == pid and after[0]["faceId"] == before[0]["faceId"]


def test_only_named_people_and_photos_can_be_tagged(client, auth, people):
    pid = _named(people, client, auth)
    a = _asset(client, auth, "none-1.jpg")
    url = f"/ui/assets/{a['id']}/faces"
    with people.app.app_context():
        unnamed = people.db().execute("SELECT id FROM persons WHERE name='' LIMIT 1").fetchone()["id"]
    assert client.post(url, json={"personId": unnamed, "x": 0.5, "y": 0.5}, headers=auth).status_code == 400
    assert client.post(url, json={"personId": "nope", "x": 0.5, "y": 0.5}, headers=auth).status_code == 400
    assert client.post(url, json={"personId": pid, "x": 2, "y": 0.5}, headers=auth).status_code == 400
    assert client.post(url, json={"personId": pid}, headers=auth).status_code == 400


def test_rotation_is_undone_and_a_rescan_keeps_the_tag(client, auth, people):
    pid = _named(people, client, auth)
    a = _asset(client, auth, "none-1.jpg")
    client.post("/ui/rotate", json={"id": a["id"], "direction": "right"}, headers=auth)   # shown turned 90 degrees clockwise
    client.post(f"/ui/assets/{a['id']}/faces", json={"personId": pid, "x": 0.9, "y": 0.2}, headers=auth)
    with people.app.app_context():
        f = people.db().execute("SELECT x, y, w FROM faces WHERE asset_id=?", (a["id"],)).fetchone()
        assert abs((f["x"] + f["w"] / 2) - 0.2) < 0.07 and abs((f["y"] + f["w"] / 2) - 0.1) < 0.07   # displayed (0.9, 0.2) -> file (0.2, 0.1)
        people.db().execute("DELETE FROM face_done"); people.db().commit()
    people.faces_pass()
    assert [f["personId"] for f in client.get(f"/ui/assets/{a['id']}/faces", headers=auth).get_json()] == [pid]


def test_untag_keeps_the_name(client, auth, people):
    pid = _named(people, client, auth)
    a = _asset(client, auth, "a-1.jpg")
    (f,) = client.get(f"/ui/assets/{a['id']}/faces", headers=auth).get_json()
    assert f["personId"] == pid
    assert client.post(f"/ui/faces/{f['faceId']}/not-this-person", headers=auth).status_code == 200
    (g,) = client.get(f"/ui/assets/{a['id']}/faces", headers=auth).get_json()
    assert g["personId"] != pid and g["name"] == ""
    names = {p["name"] for p in client.get("/ui/people", headers=auth).get_json()}
    assert "Ana" in names                                       # the person and their other photos stay


def test_untag_refuses_to_lose_a_name_with_its_last_face(client, auth, people):
    b = _asset(client, auth, "b-1.jpg")
    (f,) = client.get(f"/ui/assets/{b['id']}/faces", headers=auth).get_json()
    client.put(f"/ui/people/{f['personId']}", json={"name": "Solo"}, headers=auth)
    assert client.post(f"/ui/faces/{f['faceId']}/not-this-person", headers=auth).status_code == 409
    assert client.get(f"/ui/assets/{b['id']}/faces", headers=auth).get_json()[0]["name"] == "Solo"


def test_change_the_person(client, auth, people):
    pid = _named(people, client, auth)
    b = _asset(client, auth, "b-1.jpg")
    (f,) = client.get(f"/ui/assets/{b['id']}/faces", headers=auth).get_json()
    url = f"/ui/faces/{f['faceId']}/person"
    unnamed = f["personId"]
    assert client.put(url, json={"personId": unnamed}, headers=auth).status_code == 400    # not a named person
    assert client.put(url, json={"personId": "nope"}, headers=auth).status_code == 400
    assert client.put("/ui/faces/999999/person", json={"personId": pid}, headers=auth).status_code == 404
    assert client.put(url, json={"personId": pid}, headers=auth).status_code == 200
    (g,) = client.get(f"/ui/assets/{b['id']}/faces", headers=auth).get_json()
    assert g["personId"] == pid and g["name"] == "Ana"

