from conftest import album_assets
"""PH3: a picked folder is an album and follows the folder."""
from PIL import Image


def test_picked_folder_is_an_album_with_its_photos_only(client, auth, lib):
    client.post("/ui/folders", json={"path": str(lib / "Trip 2019")}, headers=auth)
    alb = client.get("/api/albums", headers=auth).get_json()
    assert [a["albumName"] for a in alb] == ["Trip 2019"] and alb[0]["assetCount"] == 2  # notes.txt ignored


def test_album_follows_added_and_removed_files(client, auth, lib):
    d = lib / "Trip 2019"
    client.post("/ui/folders", json={"path": str(d)}, headers=auth)
    Image.new("RGB", (50, 50)).save(d / "new.png")
    (d / "img0.jpg").unlink()
    client.post("/ui/rescan", headers=auth)
    assert client.get("/api/albums", headers=auth).get_json()[0]["assetCount"] == 2
    names = {a["originalFileName"] for a in album_assets(client, auth, client.get("/api/albums", headers=auth).get_json()[0]["id"])}
    assert names == {"img1.jpg", "new.png"}


def test_subfolders_are_included(client, auth, lib):
    client.post("/ui/folders", json={"path": str(lib / "Home")}, headers=auth)
    assert client.get("/api/albums", headers=auth).get_json()[0]["assetCount"] == 1


def test_real_mode_scans_in_background_and_reports_progress(client, auth, lib):
    import time
    import server
    server.app.config["TESTING"] = False  # the real, threaded path
    try:
        r = client.post("/ui/folders", json={"path": str(lib)}, headers=auth)
        assert r.status_code == 202 and r.get_json()["scanning"] is True
        for _ in range(100):
            if not client.get("/ui/scan", headers=auth).get_json():
                break
            time.sleep(0.05)
        assert client.get("/ui/scan", headers=auth).get_json() == []
        assert client.get("/api/albums", headers=auth).get_json()[0]["assetCount"] == 3
    finally:
        server.app.config["TESTING"] = True
