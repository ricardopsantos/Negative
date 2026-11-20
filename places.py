"""GPS from photos/videos and an offline "nearest city" lookup (GeoNames cities of 1000+ people, shipped inside the
reverse_geocoder package; nothing is fetched at run time)."""
import csv
import json
import os
import re
import subprocess

import numpy as np

_cities = None


def _load():
    global _cities
    if _cities is None:
        import importlib.util
        spec = importlib.util.find_spec("reverse_geocoder")  # locate the data file WITHOUT importing the package (it needs scipy)
        p = os.path.join(list(spec.submodule_search_locations)[0], "rg_cities1000.csv")
        with open(p, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        lat = np.radians(np.array([float(r["lat"]) for r in rows]))
        lon = np.radians(np.array([float(r["lon"]) for r in rows]))
        _cities = (lat, lon, [(r["name"], r["admin1"], r["cc"]) for r in rows])
    return _cities


def nearest(lat, lon):
    """(city, region, country code) of the closest known city to a point."""
    la, lo, names = _load()
    p, q = np.radians(lat), np.radians(lon)
    a = np.sin((la - p) / 2) ** 2 + np.cos(p) * np.cos(la) * np.sin((lo - q) / 2) ** 2
    return names[int(np.argmin(a))]


def _deg(v, ref):
    d = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
    return -d if ref in ("S", "W") else d


def gps_of(path, kind):
    """(lat, lon) or None. Images: EXIF GPS block. Videos: the QuickTime/ISO 6709 location tag. Never raises."""
    try:
        if kind == "VIDEO":
            out = subprocess.run(["ffprobe", "-protocol_whitelist", "file,pipe", "-v", "error", "-print_format", "json", "-show_entries", "format_tags", str(path)],
                                 capture_output=True, timeout=15, check=True).stdout
            tags = json.loads(out).get("format", {}).get("tags", {})
            loc = next((v for k, v in tags.items() if "location" in k.lower()), None)
            m = loc and re.match(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", loc)
            return (float(m.group(1)), float(m.group(2))) if m else None
        from PIL import Image
        with Image.open(path) as im:
            g = im.getexif().get_ifd(0x8825)
        if g and 2 in g and 4 in g:
            lat, lon = _deg(g[2], g.get(1)), _deg(g[4], g.get(3))
            if (lat, lon) != (0.0, 0.0) and abs(lat) <= 90 and abs(lon) <= 180:
                return lat, lon
    except Exception:
        pass
    return None
