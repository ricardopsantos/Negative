"""PH17 favorites (JSON, no copies), PH18 delete -> trash -> restore, CSRF."""
import json
from pathlib import Path

from conftest import album_assets, tree_state


def picked(client, auth, lib, sub="Trip 2019"):
    client.post("/ui/folders", json={"path": str(lib / sub)}, headers=auth)
    alb = client.get("/api/albums", headers=auth).get_json()[0]
    return album_assets(client, auth, alb["id"])


def test_favorite_is_a_json_entry_and_no_copy(client, auth, lib, tmp_path):
    a = picked(client, auth, lib)[0]
    files_before = sorted(p.name for p in lib.rglob("*"))
    assert client.put("/api/assets", json={"ids": [a["id"]], "isFavorite": True}, headers=auth).status_code == 204
    fav = json.loads((tmp_path / "data" / "favorites.json").read_text())
    assert [v["name"] for v in fav.values()] == [a["originalFileName"]]
    assert sorted(p.name for p in lib.rglob("*")) == files_before
    assert not list((tmp_path / "data").rglob("*.jpg"))  # no image copy anywhere
    b = client.get("/api/timeline/buckets?isFavorite=true", headers=auth).get_json()
    assert sum(x["count"] for x in b) == 1
    client.post("/ui/favorite", json={"id": a["id"], "favorite": False}, headers=auth)
    assert client.get("/api/timeline/buckets?isFavorite=true", headers=auth).get_json() == []


def test_favorite_survives_a_rename_inside_the_library(client, auth, lib):
    a = picked(client, auth, lib)[0]
    client.post("/ui/favorite", json={"id": a["id"], "favorite": True}, headers=auth)
    src = Path(a["originalPath"])
    src.rename(src.with_name("renamed.jpg"))
    client.post("/ui/rescan", headers=auth)
    alb = client.get("/api/albums", headers=auth).get_json()[0]
    got = {x["originalFileName"]: x["isFavorite"] for x in album_assets(client, auth, alb["id"])}
    assert got["renamed.jpg"] is True


def test_delete_needs_confirm_then_trash_then_restore(client, auth, lib, tmp_path):
    a = picked(client, auth, lib)[0]
    p = Path(a["originalPath"])
    assert client.delete(f"/ui/assets/{a['id']}", json={}, headers=auth).status_code == 400 and p.exists()
    assert client.delete(f"/ui/assets/{a['id']}", json={"confirm": True}, headers=auth).status_code == 200
    assert not p.exists()
    assert (tmp_path / "data" / "trash" / a["id"] / p.name).exists()  # outside the library tree
    t = client.get("/ui/trash", headers=auth).get_json()
    assert t[0]["id"] == a["id"] and t[0]["daysLeft"] >= 29
    assert client.get("/api/assets/" + a["id"], headers=auth).status_code == 404
    assert client.post(f"/ui/trash/{a['id']}/restore", headers=auth).status_code == 200
    assert p.exists() and client.get("/api/assets/" + a["id"], headers=auth).status_code == 200


def test_trash_expires_after_30_days(client, auth, lib, tmp_path):
    import server
    a = picked(client, auth, lib)[0]
    client.delete(f"/ui/assets/{a['id']}", json={"confirm": True}, headers=auth)
    m = tmp_path / "data" / "trash" / a["id"] / "meta.json"
    d = json.loads(m.read_text())
    d["deleted"] -= 31 * 86400
    m.write_text(json.dumps(d))
    assert len(client.get("/ui/trash", headers=auth).get_json()) == 1 and m.parent.exists()  # PH73: a read never deletes
    server.daily_sweep()  # the background worker purges, once a day
    assert client.get("/ui/trash", headers=auth).get_json() == []
    assert not m.parent.exists()


def test_no_batch_or_folder_delete(client, auth, lib):
    picked(client, auth, lib)
    assert client.delete("/api/assets", json={"ids": ["x"]}, headers=auth).status_code == 403
    assert client.delete("/ui/assets/", headers=auth).status_code in (404, 405)


def test_unpick_forgets_index_only(client, auth, lib):
    picked(client, auth, lib)
    before = tree_state(lib)
    fid = client.get("/api/albums", headers=auth).get_json()[0]["id"]
    assert client.delete("/ui/folders/" + fid, headers=auth).status_code == 200
    assert client.get("/api/albums", headers=auth).get_json() == [] and tree_state(lib) == before


def test_tree_lists_subfolders_inside_root_only(client, auth, lib):
    r = client.get("/ui/tree", headers=auth).get_json()
    assert {d["name"] for d in r["dirs"]} == {"Trip 2019", "Home"} and r["parent"] is None
    assert client.get("/ui/tree?path=/etc", headers=auth).status_code == 400
    assert client.get(f"/ui/tree?path={lib}/../..", headers=auth).status_code == 400


def test_cross_site_writes_refused(client, auth, lib):
    h = {**auth, "Sec-Fetch-Site": "cross-site"}
    assert client.post("/ui/folders", json={"path": str(lib)}, headers=h).status_code == 403
    h = {**auth, "Origin": "https://evil.example"}
    assert client.post("/ui/folders", json={"path": str(lib)}, headers=h).status_code == 403


def test_multi_select_delete_needs_confirm_and_trashes_each(client, auth, lib):
    assets = picked(client, auth, lib)
    ids = [a["id"] for a in assets]
    paths = [Path(a["originalPath"]) for a in assets]
    assert client.post("/ui/assets/delete", json={"ids": ids}, headers=auth).status_code == 400
    assert client.post("/ui/assets/delete", json={"ids": [], "confirm": True}, headers=auth).status_code == 400
    assert all(p.exists() for p in paths)
    r = client.post("/ui/assets/delete", json={"ids": ids + ["nope"], "confirm": True}, headers=auth)
    assert r.get_json() == {"deleted": len(ids), "failed": 1}
    assert not any(p.exists() for p in paths) and (lib / "Home" / "Kids" / "img0.jpg").exists()
    assert len(client.get("/ui/trash", headers=auth).get_json()) == len(ids)
