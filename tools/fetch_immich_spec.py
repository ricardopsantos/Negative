"""Fetch the Immich OpenAPI spec this server mimics (v3.3.6) into docs/, pinned by sha256.

The spec is part of the Immich repository (AGPL-3.0), so it is downloaded, never shipped in this repo:
the server builds a few start-up answers for the phone app from it, and tests/test_contract.py checks
every answer against it. The Dockerfile pins the same URL and sha256 (tests/test_spec_pin.py keeps them equal).
Usage: python3 tools/fetch_immich_spec.py   (does nothing when the right file is already there)"""
import hashlib
import sys
import urllib.request
from pathlib import Path

URL = "https://raw.githubusercontent.com/immich-app/immich/v3.3.6/open-api/immich-openapi-specs.json"
SHA256 = "b7f2a9c2543472a9b3a2f32ece82f56cb4b5660d6745d05f2d7903dfb5ba974b"
DEST = Path(__file__).resolve().parent.parent / "docs" / "immich-openapi-v3.3.6.json"


def ok(p: Path) -> bool:
    return p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == SHA256


def fetch(dest: Path = DEST) -> Path:
    if ok(dest):
        return dest
    data = urllib.request.urlopen(URL, timeout=60).read()
    got = hashlib.sha256(data).hexdigest()
    if got != SHA256:
        raise SystemExit(f"immich spec: sha256 {got} is not the pinned {SHA256}; refusing to use it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return dest


if __name__ == "__main__":
    print(fetch(Path(sys.argv[1]) if len(sys.argv) > 1 else DEST))
