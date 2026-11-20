"""Every /api route the Immich phone app can call, checked deeply against the Immich OpenAPI spec of the version we
report (required fields, types, nullability, enums, uuid/date-time formats), plus every sync stream line."""
import json
import re

import pytest

from test_contract import SPEC, SCHEMAS
from test_people import people  # noqa: F401  (fixture: a folder with 2 grouped people)

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)$")
TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}


def resolve(s):
    while "$ref" in s:
        s = SCHEMAS[s["$ref"].split("/")[-1]]
    if "allOf" in s and len(s["allOf"]) == 1:
        return resolve({**{k: v for k, v in s.items() if k != "allOf"}, **resolve(s["allOf"][0])})
    return s


def check(v, s, where, errs):
    s0 = s
    s = resolve(s)
    if v is None:
        if not (s.get("nullable") or s0.get("nullable")):
            errs.append(f"{where}: null, not nullable")
        return
    for k in ("oneOf", "anyOf"):
        if k in s:
            sub = []
            if not any(not (sub := [], check(v, o, where, sub))[0] and not sub for o in s[k]):
                errs.append(f"{where}: matches no {k}")
            return
    if "enum" in s and v not in s["enum"]:
        errs.append(f"{where}: {v!r} not in enum {s['enum'][:8]}")
    t = s.get("type") or ("object" if "properties" in s else None)
    if t in ("integer", "number"):
        if isinstance(v, bool) or not isinstance(v, int if t == "integer" else (int, float)):
            errs.append(f"{where}: {v!r} is not {t}")
    elif t in TYPES and not isinstance(v, TYPES[t]):
        errs.append(f"{where}: {type(v).__name__} is not {t}")
    elif t == "string":
        if s.get("format") == "uuid" and not UUID.match(v):
            errs.append(f"{where}: {v!r} is not a uuid")
        if s.get("format") == "date-time" and not DT.match(v):
            errs.append(f"{where}: {v!r} is not a date-time")
    elif t == "array":
        for i, x in enumerate(v[:50]):
            check(x, s.get("items", {}), f"{where}[{i}]", errs)
    elif t == "object":
        for k in s.get("required", []):
            if k not in v:
                errs.append(f"{where}: missing {k}")
        for k, x in v.items():
            if k in s.get("properties", {}):
                check(x, s["properties"][k], f"{where}.{k}", errs)


def schema_of(method, path):
    for p, ops in SPEC["paths"].items():
        if re.fullmatch(re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape(p)), path.split("?")[0][4:]) and method in ops:
            for code, r in ops[method]["responses"].items():
                if code.startswith("2"):
                    c = r.get("content", {}).get("application/json")
                    return int(code), c["schema"] if c else None
    raise AssertionError(f"{method} {path} is not in the spec")


@pytest.fixture
def full(people, client, auth, lib):  # noqa: F811
    client.post("/ui/folders", json={"path": str(lib / "Trip 2019")}, headers=auth)
    people.checksum_pass(limit=100)
    c = people.connect()
    aid = c.execute("SELECT id FROM assets ORDER BY id LIMIT 1").fetchone()[0]
    c.execute("INSERT OR REPLACE INTO geo VALUES (?, 0, 38.72, -9.14, 'Lisbon', 'Lisboa', 'PT')", (aid,))
    c.commit()
    c.close()
    return people


def calls(client, auth):
    albums = client.get("/api/albums", headers=auth).get_json()
    person = client.get("/api/people", headers=auth).get_json()["people"][0]["id"]
    bucket = client.get("/api/timeline/buckets", headers=auth).get_json()[0]["timeBucket"]
    asset = client.get(f"/api/timeline/bucket?timeBucket={bucket}", headers=auth).get_json()["id"][0]
    g = "get"
    return [
        (g, "/api/server/ping", None), (g, "/api/server/version", None), (g, "/api/server/about", None),
        (g, "/api/server/features", None), (g, "/api/server/config", None), (g, "/api/server/media-types", None),
        (g, "/api/server/version-check", None), (g, "/api/public/config", None), (g, "/api/server/storage", None),
        (g, "/api/server/statistics", None), (g, "/api/auth/status", None),
        ("post", "/api/auth/validateToken", None), (g, "/api/users/me", None), (g, "/api/users", None),
        (g, "/api/users/me/preferences", None), (g, "/api/users/me/onboarding", None),
        (g, "/api/albums", None), (g, f"/api/albums/{albums[0]['id']}", None), (g, "/api/albums/statistics", None),
        (g, "/api/timeline/buckets", None), (g, f"/api/timeline/bucket?timeBucket={bucket}", None),
        (g, f"/api/assets/{asset}", None), (g, "/api/assets/statistics", None),
        ("post", "/api/assets/bulk-upload-check", {"assets": [{"id": "x", "checksum": "abc"}]}),
        (g, "/api/people", None), (g, f"/api/people/{person}", None), (g, f"/api/people/{person}/statistics", None),
        (g, "/api/memories", None), (g, "/api/memories/statistics", None), (g, "/api/sync/ack", None),
        (g, "/api/partners", None), (g, "/api/notifications", None), (g, "/api/stacks", None), (g, "/api/tags", None),
        (g, "/api/shared-links", None), (g, "/api/map/markers", None), (g, "/api/search/explore", None),
        (g, "/api/search/suggestions?type=country", None), (g, "/api/search/cities", None),
        (g, "/api/search/places?name=Lis", None), (g, "/api/search/person?name=a", None),
        ("post", "/api/search/random", {}), (g, "/api/sessions", None),
        ("post", "/api/search/metadata", {}), ("post", "/api/search/smart", {"query": "cat"}),
    ]


def test_every_api_answer_matches_the_spec(full, client, auth):
    errs = []
    for method, path, body in calls(client, auth):
        code, schema = schema_of(method, path)
        r = getattr(client, method)(path, json=body, headers=auth)
        if r.status_code >= 400:
            errs.append(f"{method.upper()} {path}: HTTP {r.status_code} {r.get_data(as_text=True)[:120]}")
            continue
        if schema:
            check(r.get_json(), schema, f"{method.upper()} {path.split('?')[0]}", errs)
    assert not errs, "\n".join(errs)


def test_no_license_answers_404_like_immich(client, auth):
    for p in ("/api/server/license", "/api/users/me/license"):
        assert client.get(p, headers=auth).status_code == 404  # what real Immich answers when unlicensed


def test_every_sync_line_matches_the_spec(full, client, auth):
    types = SCHEMAS["SyncRequestType"]["enum"]
    errs = []
    for line in client.post("/api/sync/stream", json={"types": types}, headers=auth).get_data(as_text=True).splitlines():
        ev = json.loads(line)
        assert ev["type"] in SCHEMAS["SyncEntityType"]["enum"], ev["type"]
        name = "Sync" + ev["type"]
        if name in SCHEMAS:
            check(ev["data"], {"$ref": "#/components/schemas/" + name}, name, errs)
    assert not errs, "\n".join(dict.fromkeys(errs))
