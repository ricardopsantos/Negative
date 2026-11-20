# summary: The Immich OpenAPI spec is downloaded, never shipped; the Dockerfile and tools/fetch_immich_spec.py pin the same URL and sha256.
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _fetcher():
    spec = importlib.util.spec_from_file_location("fetch_immich_spec", ROOT / "tools" / "fetch_immich_spec.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dockerfile_pins_the_same_spec():
    f, docker = _fetcher(), (ROOT / "Dockerfile").read_text()
    assert f.URL in docker and f.SHA256 in docker
    assert "COPY docs/immich-openapi" not in docker  # downloaded at build time, not copied from the repo


def test_spec_in_docs_is_the_pinned_one():
    f = _fetcher()
    assert f.ok(f.DEST)  # conftest fetched it if it was missing
