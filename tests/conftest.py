import hashlib
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "lib"
    for sub, n in (("Trip 2019", 2), ("Home/Kids", 1)):
        d = root / sub
        d.mkdir(parents=True)
        for i in range(n):
            Image.new("RGB", (400, 300), (i * 40, 90, 160)).save(d / f"img{i}.jpg")
    (root / "Trip 2019" / "notes.txt").write_text("not a photo")
    return root


def tree_state(root: Path):
    """Names, sizes, mtimes and content hashes of every entry: any write shows up as a difference."""
    out = {}
    for dp, dn, fn in os.walk(root):
        for n in dn + fn:
            p = Path(dp) / n
            st = p.stat()
            h = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""
            out[str(p)] = (st.st_size, st.st_mtime_ns, h)
    return out


@pytest.fixture
def client(tmp_path, lib, monkeypatch):
    monkeypatch.setenv("PHOTOS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PHOTOS_PASSWORD", "s3cret")
    monkeypatch.setenv("PHOTOS_EMAIL", "owner@example.com")
    monkeypatch.setenv("PHOTOS_BROWSE_ROOT", str(lib))
    import server
    server._fails.clear(); server._pin_fails.clear(); server._unlocked.clear()
    server.app.config["TESTING"] = True
    return server.app.test_client()


@pytest.fixture
def auth(client):
    r = client.post("/api/auth/login", json={"email": "owner@example.com", "password": "s3cret"})
    return {"Authorization": "Bearer " + r.get_json()["accessToken"]}


def album_assets(client, auth, album_id=None):
    """Asset DTOs of an album (or of everything): what the app does, via the timeline, since AlbumResponseDto has no assets."""
    q = f"albumId={album_id}&" if album_id else ""
    out = []
    for b in client.get(f"/api/timeline/buckets?{q}".rstrip("&?"), headers=auth).get_json():
        col = client.get(f"/api/timeline/bucket?{q}timeBucket={b['timeBucket']}", headers=auth).get_json()
        out += [client.get(f"/api/assets/{i}", headers=auth).get_json() for i in col["id"]]
    return out


def pytest_configure(config):
    """The Immich OpenAPI spec is downloaded, not shipped (tools/fetch_immich_spec.py); fetch it once before collection."""
    import importlib.util
    from pathlib import Path
    f = Path(__file__).resolve().parent.parent / "tools" / "fetch_immich_spec.py"
    spec = importlib.util.spec_from_file_location("fetch_immich_spec", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.fetch()
