# Changelog

## 0.2.0

Faster to browse, with a few things borrowed from other photo apps, and many fixes from four reviews.

### Added
- Tiles show a blurred preview (ThumbHash) until their thumbnail arrives, and the timeline lays photos out in justified rows at their own shape. Months keep their real height before they load, so the scroll bar no longer jumps.
- The map can show a date range: pick From and To, or All dates.
- People can have a birthday (their age shows in the photo info) and a favorite mark.
- Press "." to favorite the focused tile or the open photo.
- Slideshow controls and the cursor fade after 2.5 seconds without input.
- A folder named with a day and a month, such as "2011 - 17 Set - Trip" (Portuguese or English month names), dates its photos to that day.

### Changed
- JSON answers carry an ETag, so the app and the browser get a quick "not changed" instead of the whole answer again.
- Favoriting or hiding many photos from the phone is one write and cannot time out.
- Memories, merge suggestions and the upload-album filter read much less from the database.
- Authenticated answers are never marked for shared caches.

### Fixed
- Favorites and hidden marks work while the original file is missing, survive byte-identical copies, and come back when the file does.
- A new PIN locks every other unlocked session.
- An edited photo gets its new thumbnail at once instead of the next day.
- A damaged video can no longer hang conversions, and phone playback of a video that needs converting answers "try again shortly" instead of waiting for the whole encode.
- Deleting a second file from the same path keeps the first one in the trash.
- A video hidden while it converts gives a hidden copy.
- Smart search never returns a photo hidden since the last search; the Favorites count matches the Favorites view.
- Odd request bodies and fields are a clear 400, never a server error.
- Phone map, cities and places, sync, rotation and search fixes from the reviews.

## 0.1.0

First public release, beta: tested with the Immich iPhone app 1.140.0.

### Added
- Photo server for your existing folders: each picked folder is an album; files are only read (Delete moves to the app's own trash, Upload writes into one upload folder).
- Works with the Immich phone app: sign-in, timeline, albums, people, memories, places, map and smart search, built from Immich's public OpenAPI spec (API 3.3.6). Phone backup is refused on purpose.
- Web app: timeline, albums, Explore (memories, people, places), map, smart search, slideshow, favorites, hidden photos behind a PIN, trash, conversions, library dashboard.
- Offline machine learning on the CPU: face grouping (OpenCV YuNet + SFace) and search by words (CLIP ViT-B/32); `PHOTOS_FACES` / `PHOTOS_CLIP` turn them off, and the apps then hide those features.
- Live Photos, HEIC/AVIF/WebP/TIFF, videos the browser cannot play (cached MP4 copy).
- One Docker container, SQLite, settings in `.env`; a ready-built image on `ghcr.io/ricardopsantos/negative` (amd64 and arm64).
