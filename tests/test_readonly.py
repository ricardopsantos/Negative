"""PH2: a scan, thumbnails and downloads leave the library byte-for-byte untouched."""
from conftest import album_assets, tree_state


def test_library_untouched_by_scan_thumbs_and_download(client, auth, lib):
    before = tree_state(lib)
    client.post("/ui/folders", json={"path": str(lib)}, headers=auth)
    ids = client.get("/api/albums", headers=auth).get_json()
    assert ids
    alb = album_assets(client, auth, ids[0]["id"])
    for a in alb:
        assert client.get(f"/api/assets/{a['id']}/thumbnail?size=preview", headers=auth).status_code == 200
        assert client.get(f"/api/assets/{a['id']}/original", headers=auth).status_code == 200
    client.post("/ui/rescan", headers=auth)
    assert tree_state(lib) == before


def test_folder_outside_root_refused(client, auth, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert client.post("/ui/folders", json={"path": str(outside)}, headers=auth).status_code == 400
