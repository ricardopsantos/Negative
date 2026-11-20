# Endpoints to mimic (from docs/immich-openapi-v3.3.6.json)

Clean-room: shapes come from the public OpenAPI spec only; no Immich code is copied (AGPL).
Order = build order. "stub" = answers with a valid empty/neutral shape. Real app traffic must be logged to confirm this list (step 2).

1. **Handshake:** GET /server/ping, /server/version, /server/about, /server/features, /server/config, /server/media-types, /server/version-check, /server/storage (stub), /public/config, /config.
2. **Auth:** POST /auth/login, /auth/logout, /auth/validateToken, GET /auth/status; GET /users/me, /users/me/preferences, /users/{id}; /users/me/onboarding, /users/me/license (stub).
3. **Browse:** GET /timeline/buckets, /timeline/bucket; GET /assets/{id}, /assets/{id}/thumbnail, /assets/{id}/original, /assets/{id}/video/playback; POST /assets/bulk-upload-check; GET /assets/statistics; POST /download/info.
4. **Sync (new app uses it):** POST /sync/stream, POST/GET/DELETE /sync/ack.
5. **Albums = folders:** GET/POST /albums, GET /albums/{id}, GET /albums/statistics, PATCH (name only), /albums/{id}/map-markers (stub).
6. **People (PH4, PH15 Explore):** GET /people, /people/{id}, /people/{id}/thumbnail, /people/{id}/statistics, PUT /people/{id} (name), POST /people/merge, GET/POST/PUT/DELETE /faces.
7. **Search:** POST /search/smart, /search/metadata, /search/random, GET /search/explore, /search/suggestions, /search/cities, /search/places.
8. **Memories + Places (PH14, PH16, real, not stubs):** GET/POST /memories, GET /memories/{id}, /memories/statistics, /memories/{id}/assets; GET /search/explore, /search/cities, /search/places, /map/markers, /map/reverse-geocode.
9. **Rest (stubs):**  /activities, /stacks, /partners, /shared-links, /notifications, /view/folder.
10. **Refuse (PH6):** POST /assets (upload), DELETE /assets, PUT /assets.
