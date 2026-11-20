"""PH55 merge suggestions, PH56 face check on a person."""
from test_people import FakeEngine, people  # noqa: F401  (fixture)


def _ids(client, auth):
    return {p["count"]: p["id"] for p in client.get("/ui/people?hidden=0", headers=auth).get_json()}


def test_person_faces_come_least_alike_first(client, auth, people):
    pid = next(p["id"] for p in client.get("/ui/people", headers=auth).get_json() if p["count"] == 4)
    fs = client.get(f"/ui/people/{pid}/faces", headers=auth).get_json()
    assert len(fs) == 4 and [f["score"] for f in fs] == sorted(f["score"] for f in fs)
    assert {"faceId", "assetId", "score"} <= set(fs[0])
    assert client.get("/ui/people/nope/faces", headers=auth).status_code == 404


def test_suggestions_ask_once_and_remember_no(client, auth, people, monkeypatch):
    a = next(p["id"] for p in client.get("/ui/people", headers=auth).get_json() if p["count"] == 4)
    with people.app.app_context():
        b = people.db().execute("SELECT id FROM persons WHERE id != ? LIMIT 1", (a,)).fetchone()["id"]
    client.put(f"/ui/people/{b}", json={"name": "Bia"}, headers=auth)
    monkeypatch.setattr(people, "SUGGEST_MIN", -1.0)
    sg = client.get("/ui/people/suggestions", headers=auth).get_json()
    assert [(s["id"], s["intoId"], s["intoName"]) for s in sg] == [(a, b, "Bia")]
    assert client.post(f"/ui/people/{a}/not-same", json={"as": b}, headers=auth).status_code == 200
    assert client.get("/ui/people/suggestions", headers=auth).get_json() == []


def test_suggestions_need_login(client):
    assert client.get("/ui/people/suggestions").status_code in (401, 403)
