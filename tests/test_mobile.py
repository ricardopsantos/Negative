"""The phone app's start-up calls: sync stream, preferences, search; answers match the spec's required fields."""
import json
from pathlib import Path

from conftest import album_assets
from test_favorites_trash import picked
from test_contract import SCHEMAS, required


def lines(r):
    return [json.loads(x) for x in r.get_data(as_text=True).splitlines() if x]


ALL = ["AuthUsersV2", "UsersV1", "AssetsV2", "AssetExifsV1", "AlbumsV2", "AlbumUsersV1", "AlbumToAssetsV1", "MemoriesV2", "MemoryToAssetsV2", "PeopleV1", "AssetFacesV3"]


def test_sync_stream_sends_everything_then_nothing_when_acked(client, auth, lib):
    picked(client, auth, lib)
    got = lines(client.post("/api/sync/stream", json={"types": ALL}, headers=auth))
    assert got[0]["type"] == "SyncResetV1" and got[-1]["type"] == "SyncCompleteV1"
    by = {}
    for g in got:
        by.setdefault(g["type"], []).append(g["data"])
    assert len(by["AssetV2"]) == 2 and len(by["AssetExifV1"]) == 2 and len(by["AlbumToAssetV1"]) == 2 and len(by["AlbumV2"]) == 1
    assert [(u["albumId"], u["role"]) for u in by["AlbumUserV1"]] == [(by["AlbumV2"][0]["id"], "owner")]  # PH54
    assert required("SyncAlbumUserV1") <= set(by["AlbumUserV1"][0])
    for typ, name in (("AssetV2", "SyncAssetV2"), ("AssetExifV1", "SyncAssetExifV1"), ("AlbumV2", "SyncAlbumV2"), ("AuthUserV2", "SyncAuthUserV2"), ("UserV1", "SyncUserV1")):
        assert required(name) <= set(by[typ][0]), (typ, required(name) - set(by[typ][0]))
    assert all(g["ack"].split("|")[0] == g["type"] for g in got)
    ack = got[-1]["ack"]
    assert client.post("/api/sync/ack", json={"acks": [ack]}, headers=auth).status_code == 204
    again = lines(client.post("/api/sync/stream", json={"types": ALL}, headers=auth))
    assert [g["type"] for g in again] == ["SyncCompleteV1"]  # nothing changed since the phone acknowledged
    a = album_assets(client, auth, client.get("/api/albums", headers=auth).get_json()[0]["id"])[0]
    client.post("/ui/favorite", json={"id": a["id"], "favorite": True}, headers=auth)
    assert lines(client.post("/api/sync/stream", json={"types": ALL}, headers=auth))[0]["type"] == "SyncResetV1"  # a change: resync


def test_hidden_photos_never_reach_the_phone(client, auth, lib):
    a = picked(client, auth, lib)[0]
    client.post("/ui/hide", json={"id": a["id"], "hidden": True}, headers=auth)
    got = lines(client.post("/api/sync/stream", json={"types": ALL}, headers=auth))
    assert a["id"] not in json.dumps(got)


def test_startup_endpoints_have_the_spec_fields(client, auth, lib):
    picked(client, auth, lib)
    for url, schema in (("/api/users/me/preferences", "UserPreferencesResponseDto"), ("/api/server/storage", "ServerStorageResponseDto"),
                        ("/api/server/statistics", "ServerStatsResponseDto"), ("/api/users/me/onboarding", "OnboardingResponseDto"),
                        ("/api/memories/statistics", "MemoryStatisticsResponseDto")):
        r = client.get(url, headers=auth)
        assert r.status_code == 200 and required(schema) <= set(r.get_json()), (url, required(schema) - set(r.get_json()))
    for url in ("/api/partners", "/api/notifications", "/api/stacks", "/api/tags", "/api/map/markers", "/api/search/explore"):
        assert client.get(url, headers=auth).get_json() == []
    ss = client.get("/api/sessions", headers=auth).get_json()  # the current login is listed (and can be revoked)
    assert len(ss) == 1 and ss[0]["current"] is True
    r = client.post("/api/search/metadata", json={"originalFileName": "img0"}, headers=auth).get_json()
    assert required("SearchResponseDto") <= set(r) and r["assets"]["total"] == 1 and required("AssetResponseDto") <= set(r["assets"]["items"][0])
    assert client.post("/api/search/smart", json={"query": "dog"}, headers=auth).get_json()["assets"]["items"] == []


def test_same_photo_in_two_folders_reaches_the_phone_once(client, auth, lib):
    """The phone keeps one asset per checksum (INSERT OR REPLACE on a unique index): a second copy would delete the first
    and break the rest of the sync. Both albums still list the photo, under the one id the phone has."""
    import server
    for sub in ("Trip 2019", "Home/Kids"):  # img0.jpg is the same file content in both
        client.post("/ui/folders", json={"path": str(lib / sub)}, headers=auth)
    server.checksum_pass(limit=100)
    by = {}
    for g in lines(client.post("/api/sync/stream", json={"types": ALL}, headers=auth)):
        by.setdefault(g["type"], []).append(g["data"])
    sums = [a["checksum"] for a in by["AssetV2"]]
    assert len(sums) == len(set(sums)) == 2
    ids = {a["id"] for a in by["AssetV2"]}
    assert {e["assetId"] for e in by["AssetExifV1"]} == ids
    assert {x["assetId"] for x in by["AlbumToAssetV1"]} <= ids and len(by["AlbumToAssetV1"]) == 3  # 2 in Trip, 1 in Kids
    assert len(client.get("/api/albums", headers=auth).get_json()) == 2  # the server itself still has every file
