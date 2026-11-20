# Third-party parts

Negative's own code is MIT (see [LICENSE](LICENSE)). It uses the parts below, each under its own license.

## Shipped in this repository

| Part | Files | License |
|---|---|---|
| [Leaflet](https://leafletjs.com) 1.9.4 | `vendor/leaflet.js`, `vendor/leaflet.css` | BSD-2-Clause, `vendor/LICENSE-leaflet` |
| [Leaflet.markercluster](https://github.com/Leaflet/Leaflet.markercluster) 1.5.3 | `vendor/leaflet.markercluster.js`, `vendor/MarkerCluster*.css` | MIT, `vendor/LICENSE-markercluster` |
| [Natural Earth](https://www.naturalearthdata.com) 1:110m admin-0 countries v5.1.2 | `vendor/world.json` (geometry only, rounded) | Public domain |

## Downloaded when the Docker image is built (not in this repository)

Each file is pinned to an upstream commit and checked by sha256 in the `Dockerfile`.

| Part | Used for | License |
|---|---|---|
| [YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) (`face_detection_yunet_2023mar.onnx`), OpenCV Zoo | Face detection | MIT |
| [SFace](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface) (`face_recognition_sface_2021dec.onnx`), OpenCV Zoo | Face grouping | Apache-2.0 |
| [CLIP ViT-B/32](https://github.com/openai/CLIP), quantized ONNX by [Xenova/clip-vit-base-patch32](https://huggingface.co/Xenova/clip-vit-base-patch32) | Smart search by words | MIT |
| [reverse_geocoder](https://github.com/thampiman/reverse-geocoder) 1.5.1 | Its city list (`rg_cities1000.csv`) names Places | LGPL-2.1 (package) |
| [GeoNames](https://www.geonames.org) cities1000, inside reverse_geocoder | Place names | CC BY 4.0: contains data from GeoNames (geonames.org) |

## Python packages and system libraries in the image

The image installs these from PyPI (`requirements.txt`) and Debian (`ffmpeg`, `tzdata`), each under its own license: Flask, Werkzeug, gunicorn, PyYAML, watchdog, segno, NumPy, tokenizers (BSD/MIT style), Pillow (MIT-CMU), OpenCV and ONNX Runtime (Apache-2.0 / MIT).

Two of them bring copyleft code into the built image:

- pillow-heif: the package is BSD-3-Clause, but its binary wheels are GPLv2 because they bundle libheif and libde265 (LGPLv3) and x265 (GPLv2). Negative only decodes HEIC with it.
- ffmpeg from Debian, which includes GPL parts.

Their source is available from the upstream projects and from Debian (`apt-get source ffmpeg`). Negative calls ffmpeg as a separate program and loads pillow-heif as a Pillow plugin, so neither changes the license of Negative's own code.

## Photos in the screenshots

The screenshots in `docs/screenshots/` show CC0 stock photos (from Unsplash, kept on Wikimedia Commons), listed in [docs/PHOTO-CREDITS.md](docs/PHOTO-CREDITS.md). They are only in the documentation; the app and the image do not include them.
