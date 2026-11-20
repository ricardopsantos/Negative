"""Bugs found in the two deep-dive passes, each pinned by a test."""
import os
from pathlib import Path

from conftest import album_assets


def pick(client, auth, p):
    client.post("/ui/folders", json={"path": str(p)}, headers=auth)


def test_overlapping_picks_keep_every_album_complete(client, auth, lib):
    """Picking Home and Home/Kids: the parent keeps the child's photo (was: the child stole it)."""
    pick(client, auth, lib / "Home")
    pick(client, auth, lib / "Home" / "Kids")
    n = {a["albumName"]: a["assetCount"] for a in client.get("/api/albums", headers=auth).get_json()}
    assert n == {"Home": 1, "Kids": 1}
    pick(client, auth, lib / "Home")  # re-pick / rescan changes nothing
    assert {a["albumName"]: a["assetCount"] for a in client.get("/api/albums", headers=auth).get_json()} == n


def test_unpicking_the_child_keeps_the_parents_photos(client, auth, lib):
    pick(client, auth, lib / "Home")
    pick(client, auth, lib / "Home" / "Kids")
    kid = [a for a in client.get("/api/albums", headers=auth).get_json() if a["albumName"] == "Kids"][0]
    client.delete("/ui/folders/" + kid["id"], headers=auth)
    assert client.get("/api/albums", headers=auth).get_json()[0]["assetCount"] == 1


def test_sibling_folder_with_same_prefix_is_not_included(client, auth, lib):
    (lib / "Trip 2019 extra").mkdir()
    from PIL import Image
    Image.new("RGB", (30, 30)).save(lib / "Trip 2019 extra" / "x.jpg")
    pick(client, auth, lib / "Trip 2019")
    assert client.get("/api/albums", headers=auth).get_json()[0]["assetCount"] == 2


def test_album_detail_has_no_asset_list(client, auth, lib):
    """AlbumResponseDto has no `assets`; sending thousands of them made a 4.7 MB reply for a 6,000-photo album."""
    pick(client, auth, lib)
    a = client.get("/api/albums", headers=auth).get_json()[0]
    assert "assets" not in client.get("/api/albums/" + a["id"], headers=auth).get_json()
    assert len(album_assets(client, auth, a["id"])) == 3


def test_file_removed_behind_our_back_is_404_not_500(client, auth, lib):
    pick(client, auth, lib / "Trip 2019")
    a = album_assets(client, auth)[0]
    os.remove(a["originalPath"])  # e.g. Syncthing deleted it
    for u in ("thumbnail", "original"):
        assert client.get(f"/api/assets/{a['id']}/{u}", headers=auth).status_code == 404
    assert client.get(f"/api/assets/{a['id']}", headers=auth).status_code == 200  # PH64: a GET never deletes the row
    client.post("/ui/rescan", headers=auth)  # only a scan forgets it
    assert client.get(f"/api/assets/{a['id']}", headers=auth).status_code == 404


def test_symlinks_are_not_followed(client, auth, lib, tmp_path):
    secret = tmp_path / "outside.jpg"
    from PIL import Image
    Image.new("RGB", (30, 30)).save(secret)
    (lib / "Trip 2019" / "link.jpg").symlink_to(secret)
    pick(client, auth, lib / "Trip 2019")
    assert client.get("/api/albums", headers=auth).get_json()[0]["assetCount"] == 2


def test_token_in_url_is_not_accepted(client, auth):
    tok = auth["Authorization"].split()[1]
    client.delete_cookie("photos_session")  # the login also set the browser cookie
    assert client.get("/api/users/me?key=" + tok).status_code == 401


def test_host_allowlist(client, auth, monkeypatch):
    monkeypatch.setenv("PHOTOS_ALLOWED_HOSTS", "photos.example.ts.net,localhost")
    assert client.get("/api/server/ping", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/server/ping", headers={"Host": "photos.example.ts.net:9557"}).status_code == 200


def test_no_library_configured_refuses_instead_of_serving_the_whole_disk(client, auth, monkeypatch):
    monkeypatch.delenv("PHOTOS_BROWSE_ROOT")
    assert client.get("/ui/tree", headers=auth).status_code == 503
    assert client.post("/ui/folders", json={"path": "/etc"}, headers=auth).status_code == 503


def test_restore_indexes_the_single_file(client, auth, lib):
    pick(client, auth, lib / "Trip 2019")
    a = album_assets(client, auth)[0]
    client.delete(f"/ui/assets/{a['id']}", json={"confirm": True}, headers=auth)
    assert client.get("/api/albums", headers=auth).get_json()[0]["assetCount"] == 1
    assert client.post(f"/ui/trash/{a['id']}/restore", headers=auth).status_code == 200
    assert client.get("/api/albums", headers=auth).get_json()[0]["assetCount"] == 2


def test_default_albums_are_picked_once_and_stay_unpicked(client, auth, lib, monkeypatch):
    import server
    monkeypatch.setenv("PHOTOS_DEFAULT_ALBUMS", "Trip 2019,Home,Missing")
    server._seeded = False
    assert client.get("/api/server/ping").status_code == 200
    names = sorted(a["albumName"] for a in client.get("/api/albums", headers=auth).get_json())
    assert names == ["Home", "Trip 2019"]  # a folder that does not exist is skipped
    for a in client.get("/api/albums", headers=auth).get_json():
        client.delete("/ui/folders/" + a["id"], headers=auth)
    server._seeded = False
    client.get("/api/server/ping")
    assert client.get("/api/albums", headers=auth).get_json() == []  # not re-added after a restart
