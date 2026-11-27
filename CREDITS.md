# Credits

## Inspiration

Negative exists because of [Immich](https://github.com/immich-app/immich) and its iPhone app: the app is what makes it
worth keeping your photos in plain folders and still having a good phone gallery. Negative is the small server behind
that app for people who do not want to run, or move their library into, a full Immich install. Thank you to the Immich
team for the app and for publishing its API.

## Code

- **[Leaflet](https://leafletjs.com)** by Vladimir Agafonkin and contributors (BSD-2-Clause) and
  **[Leaflet.markercluster](https://github.com/Leaflet/Leaflet.markercluster)** (MIT): the map in the web page,
  served from `vendor/`.
- **[Natural Earth](https://www.naturalearthdata.com)** (public domain): the world outline behind the map, in
  `vendor/world.json`.
- **[YuNet and SFace](https://github.com/opencv/opencv_zoo)** from the OpenCV Zoo (MIT and Apache-2.0): face detection
  and grouping. Downloaded when the image is built.
- **[CLIP ViT-B/32](https://github.com/openai/CLIP)** by OpenAI (MIT), as a quantized ONNX export by
  [Xenova](https://huggingface.co/Xenova/clip-vit-base-patch32): smart search by words.
- **[reverse_geocoder](https://github.com/thampiman/reverse-geocoder)** by Ajay Thampi (LGPL-2.1) and
  **[GeoNames](https://www.geonames.org)** (CC BY 4.0): the names of Places. Contains data from GeoNames.
- **Flask, Werkzeug, gunicorn, Pillow, pillow-heif, OpenCV, ONNX Runtime, NumPy, watchdog, segno, PyYAML,
  tokenizers and FFmpeg**: the rest of the stack, each under its own license. The full list, with the copyleft
  notes for pillow-heif and FFmpeg, is in [THIRD-PARTY.md](THIRD-PARTY.md).

## Immich

[Immich](https://github.com/immich-app/immich) is a self-hosted photo and video management solution, licensed
AGPL-3.0. Negative is an unofficial, independent server for its phone app: it answers the Immich REST API shapes and
contains no Immich code. Those shapes come from Immich's public OpenAPI specification (clean-room); nothing is copied.

> Negative is an independent, unofficial project. It is not affiliated with, endorsed by, or sponsored by the Immich
> project or FUTO. "Immich" is used only to describe the app this software works with, and remains the mark of its
> owner.

## Test photos

The screenshots in `docs/screenshots/` show CC0 stock photos (from Unsplash, kept on Wikimedia Commons), credited in
[docs/PHOTO-CREDITS.md](docs/PHOTO-CREDITS.md). They are only in the documentation; the app and the image do not
include them. Thanks to the photographers who share their work.
