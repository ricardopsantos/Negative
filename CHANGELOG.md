# Changelog

## 0.1.0

First public release, beta: tested with the Immich iPhone app 1.140.0.

### Added
- Photo server for your existing folders: each picked folder is an album; files are only read (Delete moves to the app's own trash, Upload writes into one upload folder).
- Works with the Immich phone app: sign-in, timeline, albums, people, memories, places, map and smart search, built from Immich's public OpenAPI spec (API 3.3.6). Phone backup is refused on purpose.
- Web app: timeline, albums, Explore (memories, people, places), map, smart search, slideshow, favorites, hidden photos behind a PIN, trash, conversions, library dashboard.
- Offline machine learning on the CPU: face grouping (OpenCV YuNet + SFace) and search by words (CLIP ViT-B/32); `PHOTOS_FACES` / `PHOTOS_CLIP` turn them off, and the apps then hide those features.
- Live Photos, HEIC/AVIF/WebP/TIFF, videos the browser cannot play (cached MP4 copy).
- One Docker container, SQLite, settings in `.env`; a ready-built image on `ghcr.io/ricardopsantos/negative` (amd64 and arm64).
