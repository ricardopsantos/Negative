"""PH58 upload into the upload folder, PH59 albums.yml as the source of truth for albums."""
import io
import os

import yaml
from PIL import Image


def jpeg(color=(10, 200, 30)):
    b = io.BytesIO()
    im = Image.new("RGB", (64, 48), color)
    ex = im.getexif()
    ex[0x0132] = "2021:05:04 10:11:12"
    im.save(b, "JPEG", exif=ex)
    return b.getvalue()


def up(client, auth, name, data, mtime=""):
    return client.post(f"/ui/upload?name={name}&mtime={mtime}", data=data, headers={**auth, "Content-Type": "application/octet-stream"})


def ymlfile(tmp_path):
    return tmp_path / "data" / "albums.yml"


def names(client, auth):
    return sorted(a["albumName"] for a in client.get("/api/albums", headers=auth).get_json())


def test_upload_goes_to_organize_byte_for_byte(client, auth, lib):
    data = jpeg()
    r = up(client, auth, "IMG_1.jpg", data, mtime="1600000000000")
    assert r.status_code == 201 and r.get_json()["folder"] == "_Organize"
    f = lib / "_Organize" / "IMG_1.jpg"
    assert f.read_bytes() == data and int(os.stat(f).st_mtime) == 1600000000  # nothing re-encoded, phone date kept
    assert not [p for p in f.parent.iterdir() if p.name.startswith(".")]  # no .part left behind
    assert "_Organize" in names(client, auth)
    alb = next(a for a in client.get("/api/albums", headers=auth).get_json() if a["albumName"] == "_Organize")
    assert alb["assetCount"] == 1
    # same content again: skipped; other content, same name: kept beside it, nothing replaced
    assert up(client, auth, "copy.jpg", data).get_json()["status"] == "duplicate"
    assert up(client, auth, "IMG_1.jpg", jpeg((200, 0, 0))).get_json()["name"] == "IMG_1 (1).jpg"
    assert f.read_bytes() == data


def test_upload_refuses_bad_names_and_types(client, auth, lib):
    assert up(client, auth, "notes.txt", b"hi").status_code == 400
    assert up(client, auth, "x.jpg", b"").status_code == 400
    r = up(client, auth, "..%2F..%2Fevil.jpg", jpeg())
    assert r.status_code == 201 and (lib / "_Organize" / "evil.jpg").exists()  # folders in the name are dropped
    import server
    assert server.app.test_client().post("/ui/upload?name=a.jpg", data=jpeg()).status_code == 401  # needs a login


def test_upload_folder_comes_from_the_file(client, auth, lib, tmp_path):
    client.get("/api/albums", headers=auth)  # first request writes albums.yml
    d = yaml.safe_load(ymlfile(tmp_path).read_text())
    assert d == {"upload_folder": "_Organize", "albums": []}
    d["upload_folder"] = "Inbox/Phone"
    ymlfile(tmp_path).write_text(yaml.safe_dump(d))
    assert up(client, auth, "a.jpg", jpeg()).get_json()["folder"] == "Inbox/Phone"
    assert (lib / "Inbox" / "Phone" / "a.jpg").exists() and "Phone" in names(client, auth)
    assert not (lib / "_Organize").exists()
    d["upload_folder"] = "../outside"
    ymlfile(tmp_path).write_text(yaml.safe_dump(d))
    assert up(client, auth, "b.jpg", jpeg()).status_code == 409


def test_albums_yml_is_the_source_of_truth(client, auth, lib, tmp_path):
    client.post("/ui/folders", json={"path": str(lib / "Trip 2019")}, headers=auth)
    d = yaml.safe_load(ymlfile(tmp_path).read_text())
    assert d["albums"] == ["Trip 2019"]  # Add folder writes the file
    d["albums"] = [{"path": "Home/Kids", "name": "The kids"}, "Trip 2019", "Missing"]
    ymlfile(tmp_path).write_text(yaml.safe_dump(d))
    assert names(client, auth) == ["The kids", "Trip 2019"]  # a missing folder is skipped, a name overrides
    kids = next(a for a in client.get("/api/albums", headers=auth).get_json() if a["albumName"] == "The kids")
    assert kids["assetCount"] == 1
    assert client.delete("/ui/folders/" + kids["id"], headers=auth).status_code == 200
    assert yaml.safe_load(ymlfile(tmp_path).read_text())["albums"] == ["Trip 2019", "Missing"]  # removing writes the file
    ymlfile(tmp_path).write_text("albums: [unclosed")  # malformed: nothing changes
    assert names(client, auth) == ["Trip 2019"]
    ymlfile(tmp_path).write_text(yaml.safe_dump({"albums": []}))
    assert names(client, auth) == []  # no longer listed: forgotten (files untouched)
    assert (lib / "Trip 2019" / "img0.jpg").exists()


def test_a_listed_folder_that_vanishes_is_kept(client, auth, lib, tmp_path):
    client.post("/ui/folders", json={"path": str(lib / "Trip 2019")}, headers=auth)
    (lib / "Trip 2019").rename(lib / "gone")  # e.g. the disk is not mounted
    client.get("/api/albums", headers=auth)
    os.utime(ymlfile(tmp_path), None)
    assert names(client, auth) == ["Trip 2019"]


def test_upload_album_is_flagged_and_opens_only_with_the_pin(client, auth, lib):
    """PH60: only the upload album is marked for drops, and its photos list only after the PIN, like Hidden."""
    up(client, auth, "IMG_9.jpg", jpeg((1, 2, 3)))
    albs = client.get("/api/albums", headers=auth).get_json()
    flagged = [a["albumName"] for a in albs if a["isUploadFolder"]]
    assert flagged == ["_Organize"]
    aid = next(a["id"] for a in albs if a["isUploadFolder"])
    assert client.get(f"/api/timeline/buckets?albumId={aid}", headers=auth).status_code == 403
    assert client.post("/api/auth/session/unlock", headers=auth, json={"pinCode": "12341234"}).status_code == 204
    assert client.get(f"/api/timeline/buckets?albumId={aid}", headers=auth).status_code == 200


def test_album_manager_new_rename_remove_and_choose_upload(client, auth, lib):
    """PH61: new album = new folder, rename, remove (list only), and moving the upload album needs the PIN."""
    r = client.post("/ui/albums", headers=auth, json={"name": "Holidays"})
    assert r.status_code == 201 and (lib / "Holidays").is_dir()
    assert client.post("/ui/albums", headers=auth, json={"name": "../evil"}).status_code == 400
    assert client.post("/ui/albums", headers=auth, json={"name": "Holidays"}).status_code == 409
    hid = r.get_json()["id"]
    assert client.put(f"/ui/folders/{hid}", headers=auth, json={"name": "Summer"}).status_code == 200
    assert "Summer" in names(client, auth)
    assert client.post(f"/ui/folders/{hid}/upload-folder", headers=auth).status_code == 403  # locked
    client.post("/api/auth/session/unlock", headers=auth, json={"pinCode": "12341234"})
    up(client, auth, "A.jpg", jpeg((9, 9, 9)))
    assert client.post(f"/ui/folders/{hid}/upload-folder", headers=auth).status_code == 200
    assert yaml.safe_load(ymlfile(lib.parent).read_text())["upload_folder"] == "Holidays"
    assert up(client, auth, "B.jpg", jpeg((8, 8, 8))).get_json()["folder"] == "Holidays"
    assert (lib / "_Organize" / "A.jpg").exists() and "_Organize" in names(client, auth)  # the old one stays an album
    assert client.delete(f"/ui/folders/{hid}", headers=auth).status_code == 400  # the upload album cannot be removed


def test_upload_album_photos_show_only_inside_the_album(client, auth, lib):
    """PH62: uploaded photos are not in the timeline or search, and the file itself needs the PIN."""
    total = lambda: sum(b["count"] for b in client.get("/api/timeline/buckets", headers=auth).get_json())
    before = total()
    r = up(client, auth, "IMG_7.jpg", jpeg((7, 7, 7)))
    assert r.status_code == 201
    assert total() == before  # the upload is not in the timeline
    client.post("/api/auth/session/unlock", headers=auth, json={"pinCode": "12341234"})
    aid_ = next(a["id"] for a in client.get("/api/albums", headers=auth).get_json() if a["isUploadFolder"])
    inside = client.get(f"/api/timeline/buckets?albumId={aid_}", headers=auth).get_json()
    assert sum(b["count"] for b in inside) == 1 and total() == before
    assert next(a for a in client.get("/api/albums", headers=auth).get_json() if a["isUploadFolder"])["assetCount"] == 1
