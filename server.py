"""Negative: an Immich-API-compatible, read-only photo server (clean-room, from the public OpenAPI spec).

Folders are never written to. A picked folder is an album; metadata and thumbnails live in DATA_DIR.
"""
import hashlib
import hmac
import io
import ipaddress
import json
import mimetypes
import shutil
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.exceptions import HTTPException
from flask import Flask, Response, abort, g, has_request_context, jsonify, request, send_file, stream_with_context

try:  # HEIC support is optional at import time so tests run without the wheel
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass
from PIL import ExifTags, Image, ImageOps

import numpy as np

import convert
import clip as clipmod
import faces
import yaml
import places

VERSION = "0.1.0"  # Negative's own version (CHANGELOG.md); hand-maintained, bumped for each public release
IMMICH_VERSION = (3, 3, 6)  # the API we mimic (docs/immich-openapi-v3.3.6.json)
NS = uuid.UUID("6f1d7a52-0c7e-4b8e-9f1a-5e0b1c2d3e4f")
IMAGE_EXT = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".heic", ".heif", ".avif", ".webp", ".gif", ".tif", ".tiff", ".bmp"}
VIDEO_EXT = {".flv", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp", ".mts", ".m2ts", ".mpg", ".mpeg", ".wmv"}
FAIL_LIMIT, FAIL_WINDOW = 5, 900
TOKEN_DAYS = int(os.environ.get("PHOTOS_TOKEN_DAYS", "90"))  # a login unused for this long expires (sliding)
COOKIE = "photos_session"  # own name: Immich runs on the same host and uses immich_access_token (cookies ignore ports)
BAD_IMAGE = (OSError, ValueError, SyntaxError, Image.DecompressionBombError)  # what decoding an untrusted file can raise
# The slim image has no /etc/mime.types, so name the video types ourselves
for _t, _e in (("video/x-matroska", ".mkv"), ("video/x-ms-wmv", ".wmv"), ("video/x-flv", ".flv"), ("video/mp2t", ".m2ts"),
               ("video/mp2t", ".mts"), ("video/3gpp", ".3gp"), ("video/quicktime", ".mov"), ("video/mp4", ".m4v"),
               ("video/webm", ".webm"), ("video/x-msvideo", ".avi"), ("video/mpeg", ".mpg"), ("video/mpeg", ".mpeg"),
               ("image/heic", ".heic"), ("image/heif", ".heif"), ("image/avif", ".avif"), ("image/webp", ".webp")):
    mimetypes.add_type(_t, _e)
# ffmpeg/ffprobe read files that arrive from phones: never let one open a URL or another local file (HLS/concat tricks)
FF_SAFE = ["-protocol_whitelist", "file,pipe"]

class _Request(Flask.request_class):
    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        """Multipart files (the share target) spill to DATA_DIR/tmp, not to the container's 64 MB /tmp."""
        import tempfile
        d = data_dir() / "tmp"
        d.mkdir(exist_ok=True)
        return tempfile.TemporaryFile(dir=d)


app = Flask(__name__)
app.request_class = _Request
_lock = threading.Lock()
_fails: dict = {}


def cfg(name, default=""):
    return os.environ.get(name, default)


def data_dir() -> Path:
    p = Path(cfg("PHOTOS_DATA_DIR", "./data"))
    (p / "thumbs").mkdir(parents=True, exist_ok=True)
    return p


SCHEMA = """CREATE TABLE IF NOT EXISTS folders(id TEXT PRIMARY KEY, path TEXT UNIQUE, name TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, folder_id TEXT, path TEXT UNIQUE, name TEXT,
  kind TEXT, taken TEXT, mtime REAL, size INTEGER, w INTEGER, h INTEGER);
CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY CHECK(id=1), email TEXT, salt BLOB, hash BLOB);
CREATE TABLE IF NOT EXISTS rotations(id TEXT PRIMARY KEY, deg INTEGER);
CREATE TABLE IF NOT EXISTS faces(id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id TEXT, x REAL, y REAL, w REAL, h REAL,
  score REAL, emb BLOB, person_id TEXT);
CREATE INDEX IF NOT EXISTS faces_asset ON faces(asset_id);
CREATE INDEX IF NOT EXISTS faces_person ON faces(person_id);
CREATE TABLE IF NOT EXISTS persons(id TEXT PRIMARY KEY, name TEXT DEFAULT '', hidden INTEGER DEFAULT 0, centroid BLOB,
  n INTEGER DEFAULT 0, cover INTEGER, cover_q REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS face_done(asset_id TEXT PRIMARY KEY, mtime REAL);
CREATE TABLE IF NOT EXISTS geo(asset_id TEXT PRIMARY KEY, mtime REAL, lat REAL, lon REAL, city TEXT, region TEXT, cc TEXT);
CREATE INDEX IF NOT EXISTS geo_city ON geo(city, cc);
CREATE TABLE IF NOT EXISTS live(still_id TEXT PRIMARY KEY, video_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS hidden(path TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v BLOB);
CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, created REAL);
CREATE TABLE IF NOT EXISTS face_not(face_id INTEGER, person_id TEXT, PRIMARY KEY(face_id, person_id));
CREATE TABLE IF NOT EXISTS clip(asset_id TEXT PRIMARY KEY, mtime REAL, emb BLOB);
CREATE TABLE IF NOT EXISTS merge_no(src TEXT, dst TEXT, PRIMARY KEY(src, dst));
CREATE TABLE IF NOT EXISTS face_manual(face_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY, id TEXT UNIQUE, first REAL, used REAL, agent TEXT);
CREATE INDEX IF NOT EXISTS assets_taken ON assets(taken);
CREATE INDEX IF NOT EXISTS assets_folder_taken ON assets(folder_id, taken);
CREATE INDEX IF NOT EXISTS assets_size ON assets(size);"""
_schema_done: set = set()


_upl_cache = (0.0, "", None)


def _upload_prefix():
    """The upload folder as a path prefix ("" if none), re-read from albums.yml at most every 3 s."""
    global _upl_cache
    now = time.time()
    # Called once per row of big queries: the albums.yml path is compared at most once per request (it changes only in tests).
    stale = now > _upl_cache[0]
    if not stale and has_request_context() and not g.get("_upl_ok"):
        g._upl_ok = True
        stale = _upl_cache[2] != str(albums_file())
    if stale:
        key = str(albums_file())
        try:
            d = upload_dir()
        except Exception:  # never break a query because albums.yml is being edited
            d = None
        _upl_cache = (now + 3, str(d).rstrip("/") + "/" if d else "", key)
        if has_request_context():
            g._upl_ok = True
    return _upl_cache[1]


def _in_upload(p):
    pre = _upload_prefix()
    return 1 if pre and isinstance(p, str) and p.startswith(pre) else 0


def connect():
    path = data_dir() / "photos.db"
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.create_function("in_upload", 1, _in_upload, deterministic=True)
    if str(path) not in _schema_done:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        for col in ("ver INTEGER DEFAULT 0", "duration REAL", "sha1 TEXT", "date_src TEXT", "added REAL"):  # added after the first release
            try:
                c.execute("ALTER TABLE assets ADD COLUMN " + col)
            except sqlite3.OperationalError:
                pass
        c.execute("UPDATE assets SET added=mtime WHERE added IS NULL")  # PH70: rows from before 'added' arrived when their file did
        c.commit()
        _schema_done.add(str(path))
    return c


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def _close(_):
    c = g.pop("db", None)
    if c:
        c.close()


# ---- auth (HouseAuthPattern: last-hop client IP, fail closed) ----
def _is_private(addr):
    try:
        ip = ipaddress.ip_address(addr)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


_DOCKER_NET = ipaddress.ip_network("172.16.0.0/12")


def _is_trusted_proxy(addr):
    """PH92: X-Forwarded-For is believed only from this host (loopback) or the Docker bridge gateway that a loopback-published
    port arrives from. A LAN address (192.168.x) is not our proxy and must not choose the address that gets logged or limited."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_loopback or ip in _DOCKER_NET


def client_ip():
    remote = request.remote_addr or "unknown"
    if _is_trusted_proxy(remote):
        hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
        if hops:
            return hops[-1]
    return remote


def _scrypt(pw: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)


def account_email():
    """The single user's email: PHOTOS_PASSWORD in the environment wins (PHOTOS_EMAIL names it), else the one created on first run."""
    if cfg("PHOTOS_PASSWORD"):
        return cfg("PHOTOS_EMAIL", DEFAULT_EMAIL)
    row = db().execute("SELECT email FROM account").fetchone()
    return row["email"] if row else None


def check_password(mail: str, pw: str) -> bool:
    env = cfg("PHOTOS_PASSWORD")
    if env:
        want, ok_pw = cfg("PHOTOS_EMAIL", DEFAULT_EMAIL), hmac.compare_digest(pw.encode(), env.encode())
    else:
        row = db().execute("SELECT * FROM account").fetchone()
        if not row:
            return False
        want, ok_pw = row["email"], hmac.compare_digest(_scrypt(pw, row["salt"]), row["hash"])
    return bool(hmac.compare_digest(mail.lower().encode(), want.lower().encode()) & ok_pw)


DEFAULT_EMAIL = "admin@photos.local"  # only used when PHOTOS_PASSWORD is set without PHOTOS_EMAIL


def user_id():
    return str(uuid.uuid5(NS, "user:" + (account_email() or "")))


def user_name():
    """The name the apps show: PHOTOS_NAME, else the email's local part capitalised ("alice@x" -> "Alice")."""
    return cfg("PHOTOS_NAME") or ((account_email() or "").split("@")[0].capitalize() or "Owner")


def _token_from_request():
    h = request.headers
    auth = h.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (h.get("x-immich-user-token") or request.cookies.get(COOKIE)
            or request.cookies.get("immich_access_token") or "")  # old cookie name, read only, until it expires


def authed():
    """A known token that was used within TOKEN_DAYS. Use slides the expiry (written at most once a minute)."""
    t = _token_from_request()
    if not t:
        return False
    h, now = hashlib.sha256(t.encode()).hexdigest(), time.time()
    row = db().execute("SELECT created FROM tokens WHERE hash=?", (h,)).fetchone()
    if row is None:
        return False
    if now - (row["created"] or 0) > TOKEN_DAYS * 86400:
        db().execute("DELETE FROM tokens WHERE hash=?", (h,))
        db().execute("DELETE FROM sessions WHERE hash=?", (h,))
        db().commit()
        return False
    if now - row["created"] > 60:
        db().execute("UPDATE tokens SET created=? WHERE hash=?", (now, h))
        db().execute("INSERT OR IGNORE INTO sessions VALUES(?,?,?,?,?)",  # a login from before the sessions table
                     (h, str(uuid.uuid4()), row["created"], now, (request.headers.get("User-Agent") or "")[:200]))
        db().execute("UPDATE sessions SET used=? WHERE hash=?", (now, h))
        db().commit()
    return True


def new_token():
    """Create a login token and its session row (for the sessions list and Revoke)."""
    tok, now = secrets.token_urlsafe(32), time.time()
    h = hashlib.sha256(tok.encode()).hexdigest()
    db().execute("INSERT INTO tokens VALUES(?,?)", (h, now))
    db().execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)",
                 (h, str(uuid.uuid4()), now, now, (request.headers.get("User-Agent") or "")[:200]))
    db().commit()
    return tok


def login_response(tok):
    resp = jsonify(accessToken=tok, userId=user_id(), userEmail=account_email(), name=user_name(), isAdmin=True,
                   isOnboarded=True, profileImagePath="", shouldChangePassword=False)
    secure = request.is_secure or (_is_private(request.remote_addr or "") and request.headers.get("X-Forwarded-Proto") == "https")
    resp.set_cookie(COOKIE, tok, httponly=True, samesite="Strict", secure=secure, max_age=TOKEN_DAYS * 86400)
    resp.delete_cookie("immich_access_token")  # do not leave our token under Immich's name
    return resp


def _limited(store, ip, now):
    """Reserve one attempt for ip under the lock; False when the limit is reached. A success gives the slot back."""
    with _lock:
        for k in [k for k, v in store.items() if not v or now - v[-1] >= FAIL_WINDOW]:
            del store[k]  # forget idle addresses, so the dict cannot grow forever
        recent = [t for t in store.get(ip, []) if now - t < FAIL_WINDOW]
        if len(recent) >= FAIL_LIMIT:
            store[ip] = recent
            return False
        store[ip] = recent + [now]
        return True


def _release(store, ip, now):
    with _lock:
        if now in store.get(ip, []):
            store[ip].remove(now)


PUBLIC = {"/api/auth/qr-login", "/api/server/ping", "/api/server/version", "/api/server/about", "/api/server/features",
          "/api/server/config", "/api/server/media-types", "/api/server/version-check",
          "/api/public/config", "/api/auth/login", "/api/auth/admin-sign-up", "/api/server/version-history",
          "/ui/share"}  # checks the login itself (a redirect, not a 401)


@app.before_request
def host_check():
    allowed = [h.strip() for h in cfg("PHOTOS_ALLOWED_HOSTS").split(",") if h.strip()]
    if allowed and request.host.split(":")[0] not in allowed:
        return err("Host not allowed", 400, "Bad Request")


@app.before_request
def csrf():
    """Cookie auth must not be usable cross-site (same rule as ContextViewer CV9)."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return err("Cross-site request refused", 403, "Forbidden")
        origin = request.headers.get("Origin")
        if request.path == "/ui/share" and origin == "null" and request.headers.get("Sec-Fetch-Site") in (None, "none", "same-origin"):
            return None  # the OS share sheet (PH87); the Strict cookie still has to come along
        if origin and origin.split("://", 1)[-1] != request.host:
            return err("Cross-origin request refused", 403, "Forbidden")


@app.after_request
def security_headers(resp):
    """No framing (clickjacking on Delete/Restore), no type sniffing, and a CSP as a second line against injected markup:
    scripts only from /app.js (no inline script or handlers anywhere in the page)."""
    h = resp.headers
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                 "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; "
                 "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    return resp


@app.before_request
def gate():
    p = request.path
    if p == "/healthz" or p in PUBLIC or not p.startswith(("/api/", "/ui/")):
        return None
    if not authed():
        return jsonify(message="Invalid user token", error="Unauthorized", statusCode=401), 401


def authlog(what):
    """One line per failed attempt in DATA_DIR/auth.log for fail2ban: 'photos-auth FAIL ip=1.2.3.4 what=login'."""
    try:
        with open(data_dir() / "auth.log", "a") as f:
            f.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} photos-auth FAIL ip={client_ip()} what={what}\n")
    except OSError:
        pass


def err(msg, code, name="Error"):
    return jsonify(message=msg, error=name, statusCode=code), code


@app.post("/api/auth/admin-sign-up")
def sign_up():
    """First run only: create the one user. Once an account exists (or PHOTOS_PASSWORD is set) this is refused."""
    if cfg("PHOTOS_PASSWORD"):
        return err("The user is configured by the server", 400, "Bad Request")
    b = request.get_json(silent=True) or {}
    mail, pw = str(b.get("email", "")).strip(), str(b.get("password", ""))
    if "@" not in mail or len(mail) > 200:
        return err("Enter a valid email", 400, "Bad Request")
    if len(pw) < 8:
        return err("Password must be at least 8 characters", 400, "Bad Request")
    pin = str(b.get("pinCode") or "")
    if pin and not valid_pin(pin):
        return err("The PIN must be 4 to 12 digits", 400, "Bad Request")
    salt = secrets.token_bytes(16)
    cur = db().execute("INSERT INTO account SELECT 1,?,?,? WHERE NOT EXISTS(SELECT 1 FROM account)",
                       (mail, salt, _scrypt(pw, salt)))  # atomic: two racing sign-ups cannot both win
    db().commit()
    if cur.rowcount != 1:
        return err("A user already exists", 400, "Bad Request")
    if pin:
        set_pin(pin)
    return jsonify(id=user_id(), email=mail, name=user_name(), isAdmin=True), 201


@app.post("/api/auth/login")
def login():
    if account_email() is None:
        return err("No user exists yet: create one first", 503, "Service Unavailable")  # fail closed
    ip, now = client_ip(), time.time()
    if not _limited(_fails, ip, now):
        return err("Too many failed logins", 429, "Too Many Requests")
    b = request.get_json(silent=True) or {}
    if not check_password(str(b.get("email", "")), str(b.get("password", ""))):
        authlog("login")
        return err("Incorrect email or password", 401, "Unauthorized")
    _release(_fails, ip, now)
    return login_response(new_token())


@app.post("/api/auth/logout")
def logout():
    t = _token_from_request()
    _unlocked.pop(_tok_hash(), None)
    db().execute("DELETE FROM tokens WHERE hash=?", (hashlib.sha256(t.encode()).hexdigest(),))
    db().execute("DELETE FROM sessions WHERE hash=?", (hashlib.sha256(t.encode()).hexdigest(),))
    db().commit()
    resp = jsonify(successful=True, redirectUri="/auth/login")
    resp.delete_cookie(COOKIE)
    resp.delete_cookie("immich_access_token")
    return resp


@app.post("/api/auth/validateToken")
def validate():
    return jsonify(authStatus=True)


# ---- PIN for the Hidden album (Immich's /auth/pin-code + /auth/session/lock|unlock shapes) ----
DEFAULT_PIN = "12341234"  # used until the user sets one
UNLOCK_SECS = 900  # an unlocked session locks again after 15 idle minutes
_unlocked: dict = {}  # token hash -> expiry
_pin_fails: dict = {}


def _tok_hash():
    return hashlib.sha256(_token_from_request().encode()).hexdigest()


def unlocked(slide=True):
    """slide=False only looks (the lock indicator's poll and the sidebar counts must not keep the album open, PH78)."""
    h, now = _tok_hash(), time.time()
    if _unlocked.get(h, 0) > now:
        if slide:
            _unlocked[h] = now + UNLOCK_SECS  # sliding: using the album keeps it open
        return True
    _unlocked.pop(h, None)
    return False


def is_hidden(path):
    """Hidden, or the clip of a hidden Live Photo (only the still's path is marked)."""
    return db().execute("SELECT 1 FROM hidden WHERE path=? UNION ALL SELECT 1 FROM live l JOIN assets v ON v.id=l.video_id "
                        "JOIN assets s ON s.id=l.still_id JOIN hidden h ON h.path=s.path WHERE v.path=? LIMIT 1",
                        (path, path)).fetchone() is not None


def pin_is_default():
    return db().execute("SELECT 1 FROM settings WHERE k='pin'").fetchone() is None


def check_pin(pin):
    row = db().execute("SELECT v FROM settings WHERE k='pin'").fetchone()
    if row is None:
        return hmac.compare_digest(pin.encode(), DEFAULT_PIN.encode())
    return hmac.compare_digest(_scrypt(pin, row["v"][:16]), row["v"][16:])


def valid_pin(pin):
    return isinstance(pin, str) and re.fullmatch(r"\d{4,12}", pin) is not None


def set_pin(pin):
    salt = secrets.token_bytes(16)
    db().execute("INSERT OR REPLACE INTO settings VALUES('pin', ?)", (salt + _scrypt(pin, salt),))
    db().commit()


def _proven(b):
    """The caller proved who they are with the current PIN or the account password. Failures are rate-limited per IP."""
    ip, now = client_ip(), time.time()
    pin, pw = b.get("pinCode"), b.get("password")
    if not _limited(_pin_fails, ip, now):
        abort(429)
    if isinstance(pw, str) and not _limited(_fails, ip, now):  # a password guess here counts against the login limit too
        _release(_pin_fails, ip, now)
        abort(429)
    ok = (isinstance(pin, str) and check_pin(pin)) or (isinstance(pw, str) and check_password(account_email() or "", pw))
    if not ok:
        authlog("pin")
    else:
        _release(_pin_fails, ip, now)
        _release(_fails, ip, now)
    return bool(ok)


@app.errorhandler(429)
def _too_many(_):
    return err("Too many failed attempts, try again later", 429, "Too Many Requests")


@app.post("/api/auth/session/unlock")
def session_unlock():
    if not _proven(request.get_json(silent=True) or {}):
        return err("Wrong PIN code", 400, "Bad Request")
    _unlocked[_tok_hash()] = time.time() + UNLOCK_SECS
    return "", 204


@app.post("/api/auth/session/lock")
def session_lock():
    _unlocked.pop(_tok_hash(), None)
    return "", 204


@app.post("/api/auth/pin-code")
def pin_setup():
    pin = (request.get_json(silent=True) or {}).get("pinCode")
    if not pin_is_default():
        return err("A PIN is already set", 400, "Bad Request")
    if not valid_pin(pin):
        return err("The PIN must be 4 to 12 digits", 400, "Bad Request")
    set_pin(pin)
    return "", 204


@app.put("/api/auth/pin-code")
def pin_change():
    b = request.get_json(silent=True) or {}
    if not valid_pin(b.get("newPinCode")):
        return err("The new PIN must be 4 to 12 digits", 400, "Bad Request")
    if not _proven(b):
        return err("Wrong PIN or password", 400, "Bad Request")
    set_pin(b["newPinCode"])
    return "", 204


@app.delete("/api/auth/pin-code")
def pin_reset():
    """Back to the default PIN."""
    if not _proven(request.get_json(silent=True) or {}):
        return err("Wrong PIN or password", 400, "Bad Request")
    db().execute("DELETE FROM settings WHERE k='pin'")
    db().commit()
    return "", 204


@app.get("/api/auth/status")
def auth_status():
    el = unlocked(slide=False)
    left = max(0, int(_unlocked.get(_tok_hash(), 0) - time.time())) if el else 0
    return jsonify(password=True, pinCode=not pin_is_default(), isElevated=el, pinIsDefault=pin_is_default(),
                   secondsLeft=left)  # PH78: the header's lock indicator shows the minutes left


@app.post("/ui/hide")
def ui_hide():
    b = request.get_json(silent=True) or {}
    set_hidden(_asset(b.get("id", "")), bool(b.get("hidden")))  # un-hiding needs the album unlocked (see _asset)
    return jsonify(ok=True)


# ---- server / handshake ----
@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/.well-known/immich")
def well_known():
    return jsonify(api={"endpoint": "/api"})


@app.get("/api/server/ping")
def ping():
    return jsonify(res="pong")


@app.get("/api/server/version")
def version():
    a, b, c = IMMICH_VERSION
    return jsonify(major=a, minor=b, patch=c, prerelease=0)


@app.get("/api/server/about")
def about():
    return jsonify(version="v%d.%d.%d" % IMMICH_VERSION, versionUrl="", licensed=False)


@app.get("/api/server/features")
def features():
    # what the server really does: the phone hides People, context search and place names when these are false
    on = dict(map=True, trash=False, search=True, passwordLogin=True, reverseGeocoding=True,
              smartSearch=clipmod.available() and cfg("PHOTOS_CLIP", "1") != "0",
              facialRecognition=faces.available() and cfg("PHOTOS_FACES", "1") != "0")
    keys = "configFile duplicateDetection email importFaces oauth oauthAutoLaunch ocr realtimeTranscoding sidecar".split()
    return jsonify({**{k: False for k in keys}, **on})


@app.get("/api/server/config")
def server_config():
    return jsonify(externalDomain="", isInitialized=account_email() is not None, isOnboarded=True, loginPageMessage="",
                   maintenanceMode=False, mapDarkStyleUrl="", mapLightStyleUrl="", minFaces=3,
                   oauthButtonText="", publicUsers=False, trashDays=30, userDeleteDelay=7)


@app.get("/api/server/media-types")
def media_types():
    return jsonify(image=sorted(IMAGE_EXT), video=sorted(VIDEO_EXT), sidecar=[".xmp"])


@app.get("/api/server/version-check")
def version_check():
    return jsonify(checkedAt=None, releaseVersion="v%d.%d.%d" % IMMICH_VERSION)


@app.get("/api/public/config")
def public_config():
    return jsonify(oauth=dict(enabled=False, buttonText="", autoLaunch=False), passwordLogin=dict(enabled=True),
                   server=dict(loginPageMessage="", publicUsers=False, externalDomain=""), theme=dict(customCss=""))


def _cluster_group():
    """Real Immich always puts a user in a cluster group (a non-null uuid v4 since 3.2.0); a stable one per user."""
    u = uuid.uuid5(NS, "cluster:" + user_id())
    return str(uuid.UUID(bytes=u.bytes, version=4))


def _user():
    mail = account_email()
    return dict(id=user_id(), email=mail, name=user_name(), isAdmin=True, avatarColor="primary",
                createdAt="2026-11-06T00:00:00.000Z", updatedAt="2026-11-06T00:00:00.000Z",
                profileChangedAt="2026-11-06T00:00:00.000Z", deletedAt=None, clusterGroupId=_cluster_group(), license=None,
                oauthId="", profileImagePath="", quotaSizeInBytes=None, quotaUsageInBytes=0,
                shouldChangePassword=False, status="active", storageLabel=None)


@app.get("/api/users/me")
def me():
    return jsonify(_user())


@app.get("/api/users")
def users():
    return jsonify([_user()])


# ---- favorites: a JSON file {sha256: {path, name, size}}; never a copy of an image ----
def fav_load(name="favorites.json"):
    try:
        return json.loads((data_dir() / name).read_text())
    except (OSError, ValueError):
        return {}


def fav_save(d, name="favorites.json"):
    tmp = (data_dir() / name).with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
    tmp.replace(data_dir() / name)


def sha_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fav_paths():
    return {v["path"] for v in fav_load().values()}


_fav_lock = threading.RLock()  # favorites.json / hidden.json are read-modify-written from several threads
HASH_INLINE_MAX = 64 << 20  # bigger files (videos) are hashed in a background thread, not inside the request


def _hash_later(name, key, path):
    def run():
        try:
            sha = sha_of(path)
        except OSError:
            return
        with _fav_lock:
            d = fav_load(name)
            if key in d and sha not in d:
                d[sha] = d.pop(key)
                fav_save(d, name)
    if app.config.get("TESTING"):
        return run()
    threading.Thread(target=run, daemon=True).start()


def _mark(name, a, on):
    """Add or remove one file in favorites.json / hidden.json, keyed by its content hash (survives renames)."""
    later = None
    with _fav_lock:
        d = fav_load(name)
        for k in [k for k, v in d.items() if v["path"] == a["path"]]:
            del d[k]
        if on:
            if (a["size"] or 0) <= HASH_INLINE_MAX:
                key = sha_of(a["path"])
            else:
                key = later = "pending:" + a["id"]
            d[key] = dict(path=a["path"], name=a["name"], size=a["size"])
        fav_save(d, name)
    if later:
        _hash_later(name, later, a["path"])


def set_favorite(a, on):
    _mark("favorites.json", a, on)


def _forget_marks(path):
    """A file leaving the library (deleted in the app) leaves favorites and hidden too; else relink hunts for it forever."""
    with _fav_lock:
        for name in ("favorites.json", "hidden.json"):
            d = fav_load(name)
            keep = {k: v for k, v in d.items() if v["path"] != path}
            if len(keep) != len(d):
                fav_save(keep, name)


def fav_relink(c, name="favorites.json"):
    """A favorite whose file moved/renamed inside the library is found again by size + content hash.
    Each lost entry is searched once per change in the number of same-size files, not on every scan."""
    with _fav_lock:
        d = fav_load(name)
        known = {r["path"] for r in c.execute("SELECT path FROM assets")}
        changed = False
        for sha, v in list(d.items()):
            if v["path"] in known or sha.startswith("pending:"):
                continue
            cands = c.execute("SELECT path FROM assets WHERE size=?", (v["size"],)).fetchall()
            if v.get("tried") == len(cands):
                continue
            v["tried"], changed = len(cands), True
            for r in cands:
                if r["path"] not in {x["path"] for x in d.values()} and sha_of(r["path"]) == sha:
                    v["path"], v["name"] = r["path"], Path(r["path"]).name
                    v.pop("tried", None)
                    break
        if changed:
            fav_save(d, name)


# ---- hidden photos: same durable JSON as favorites (hidden.json) + a table the queries filter on ----
def set_hidden(a, on):
    _mark("hidden.json", a, on)
    if on:
        db().execute("INSERT OR IGNORE INTO hidden VALUES(?)", (a["path"],))
    else:
        db().execute("DELETE FROM hidden WHERE path=?", (a["path"],))
    db().commit()


def hidden_sync(c):
    """After a scan: find moved files again, and make the table match hidden.json."""
    fav_relink(c, "hidden.json")
    with _fav_lock:
        rows = [(v["path"],) for v in fav_load("hidden.json").values()]
    if {r[0] for r in c.execute("SELECT path FROM hidden")} == {r[0] for r in rows}:
        return  # nothing changed: no write
    c.execute("DELETE FROM hidden")
    c.executemany("INSERT OR IGNORE INTO hidden VALUES(?)", rows)
    c.commit()


# ---- folders = albums ----
def aid(path):
    return str(uuid.uuid5(NS, "dir:" + path))


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def local_iso(ts):
    """A real (UTC) timestamp as local wall time in the same form EXIF dates are stored (wall time, 'Z' as Immich writes
    localDateTime), so photos, videos and file dates sort together. TZ comes from the container."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def probe_video(p: Path):
    """(width, height, taken_ts|None, seconds|None) from ffprobe; zeros/None when ffprobe is missing or the file is unreadable."""
    try:
        out = subprocess.run(["ffprobe", *FF_SAFE, "-v", "error", "-select_streams", "v:0", "-print_format", "json",
                              "-show_entries", "stream=width,height:stream_tags=rotate:stream_side_data=rotation"
                              ":format=duration:format_tags=creation_time", "-i", str(p)],
                             capture_output=True, timeout=15, check=True).stdout
        j = json.loads(out)
        st = (j.get("streams") or [{}])[0]
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
        rot = (st.get("tags") or {}).get("rotate") or next((d.get("rotation") for d in st.get("side_data_list") or [] if "rotation" in d), 0)
        if abs(int(float(rot or 0))) % 180 == 90:
            w, h = h, w  # phone videos are stored sideways and flagged; show them as they play
        ct = (j.get("format", {}).get("tags") or {}).get("creation_time")
        taken = datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp() if ct else None
        dur = float(j.get("format", {}).get("duration") or 0) or None
        return w, h, taken, dur
    except Exception:
        return 0, 0, None, None


def video_frame(p: Path, size: int):
    """One JPEG frame (about a second in, or the first frame of a very short clip) as a PIL image."""
    for at in ("1", "0"):
        r = subprocess.run(["ffmpeg", *FF_SAFE, "-v", "error", "-ss", at, "-i", str(p), "-frames:v", "1",
                            "-vf", f"scale='min({size},iw)':-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                           capture_output=True, timeout=30)
        if r.stdout:
            return Image.open(io.BytesIO(r.stdout))
    raise OSError("no frame")


INDEX_VER = 3  # bump when read_meta changes what it stores: every file is read again on the next scan (3: PH68 dates from names)

_DATE_FULL = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
                        r"(?:[-_ T.]{0,4}(?:at[ _])?([01]\d|2[0-3])[-_.:h]?([0-5]\d)[-_.:m]?([0-5]\d))?(?!\d)")
_DATE_MONTH = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)[-_. ](0[1-9]|1[0-2])(?!\d)")
_DATE_YEAR = re.compile(r"(?<![\d])(19[5-9]\d|20[0-4]\d)(?![\d])")


def name_date(p: Path, mtime: float):
    """PH68: (wall-time ISO, source) for a file with no capture date in it: a date in its own name (IMG_20190101_120000,
    'WhatsApp Image 2019-01-01 at 10.11.12', PXL_..., VID-20190101-WA0001), else in a folder name up to the library
    root ('2019-06 Trip', '1987'), else the file's mtime. A folder giving only a year (or year and month) loses to the
    mtime when the mtime falls inside it (it is more precise then). Never a date in the future."""
    today = datetime.now().strftime("%Y-%m-%d")
    fm = datetime.fromtimestamp(mtime)

    def ok(y, mo, d, hh="12", mi="00", ss="00"):
        try:
            v = datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss))
        except ValueError:
            return None
        iso_ = v.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return iso_ if iso_[:10] <= today else None
    m = _DATE_FULL.search(p.stem)
    if m:
        y, mo, d, hh, mi, ss = m.groups()
        v = ok(y, mo, d, *((hh, mi, ss) if hh else ()))
        if v:
            return v, "name"
    try:
        root = browse_root()
    except Exception:
        root = None
    for d in p.parents:
        if root is None or d == root or root not in d.parents:
            break
        n = d.name
        m = _DATE_FULL.search(n)
        if m and ok(*m.groups()[:3]):
            return ok(*m.groups()[:3]), "folder"
        m = _DATE_MONTH.search(n)
        if m:
            y, mo = m.groups()
            if fm.year == int(y) and fm.month == int(mo):
                break
            if ok(y, mo, 15):
                return ok(y, mo, 15), "folder"
        m = _DATE_YEAR.search(n)
        if m:
            if fm.year == int(m.group(1)):
                break
            if ok(m.group(1), 7, 1):
                return ok(m.group(1), 7, 1), "folder"
    return local_iso(mtime), "file"


def read_meta(folder_id, p: Path, st=None):
    """One file's row (the file is only read). Slow (opens the file, ffprobe): call it with no transaction open."""
    st = st or p.stat()
    ext = p.suffix.lower()
    kind = "IMAGE" if ext in IMAGE_EXT else "VIDEO"
    taken, w, h, dur, src = None, 0, 0, None, None
    if kind == "IMAGE":
        try:
            with Image.open(p) as im:  # one open for size and date
                w, h = im.size
                ex = im.getexif()
                v = ex.get_ifd(0x8769).get(0x9003) or ex.get(0x0132)
                if v:  # EXIF is local wall time already
                    taken, src = datetime.strptime(v[:19], "%Y:%m:%d %H:%M:%S").strftime("%Y-%m-%dT%H:%M:%S.000Z"), "exif"
        except Exception:
            pass
    else:
        w, h, ts, dur = probe_video(p)
        if ts and ts >= 86400 * 366:  # 1970/1904-epoch placeholders mean "unknown"
            taken, src = local_iso(ts), "video"  # QuickTime creation_time is UTC
    if not taken:
        taken, src = name_date(p, st.st_mtime)
    return (str(uuid.uuid5(NS, "file:" + str(p))), folder_id, str(p), p.name, kind, taken,
            st.st_mtime, st.st_size, w, h, INDEX_VER, dur, src, time.time())


ROW_COLS = ("id", "folder_id", "path", "name", "kind", "taken", "mtime", "size", "w", "h", "ver", "duration", "date_src", "added")


def write_rows(c, rows):
    """Upsert rows. A re-read of an unchanged file keeps its checksum (the hash pass need not read 58 GB again) and a
    row keeps the time it first arrived ('added', PH70)."""
    c.executemany("INSERT INTO assets(id, folder_id, path, name, kind, taken, mtime, size, w, h, ver, duration, date_src, added) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET folder_id=excluded.folder_id, "
                  "path=excluded.path, name=excluded.name, kind=excluded.kind, taken=excluded.taken, mtime=excluded.mtime, "
                  "size=excluded.size, w=excluded.w, h=excluded.h, ver=excluded.ver, duration=excluded.duration, "
                  "date_src=excluded.date_src, sha1=CASE WHEN assets.mtime=excluded.mtime AND assets.size=excluded.size "
                  "THEN assets.sha1 ELSE NULL END", rows)
    _thumbs_idle.clear()  # PH82: new or changed files: the thumbnail passes walk the library again


def index_file(c, folder_id, p: Path, st=None):
    """Read one file's metadata (never writes to it) and upsert its row."""
    write_rows(c, [read_meta(folder_id, p, st)])


LIVE_STILL_EXT = {".heic", ".heif", ".jpg", ".jpeg"}  # HEIC first: IMG_1.HEIC wins IMG_1.MOV over a converted IMG_1.JPG
LIVE_VIDEO_MAX_BYTES = int(cfg("PHOTOS_LIVE_VIDEO_MAX_MB", "40")) * 1024 * 1024  # a Live Photo's clip is seconds long
LIVE_CLIP = "id NOT IN (SELECT video_id FROM live)"  # the clip half of a Live Photo is not a tile of its own
NOT_HIDDEN = LIVE_CLIP + " AND path NOT IN (SELECT path FROM hidden)"  # what the upload album itself shows
# PH62: the upload album's photos show only inside that album (after the PIN), never in the timeline, search, people or places.
NOT_LIVE_VIDEO = NOT_HIDDEN + " AND NOT in_upload(path)"  # what the timeline, albums, people and places show
ONLY_HIDDEN = LIVE_CLIP + " AND path IN (SELECT path FROM hidden)"  # the Hidden album


def live_pairs(files):
    """{still name: video name} for each photo with a same-stem .mov beside it (how an iPhone exports a Live Photo)."""
    movs = {}
    for n, size in files.items():
        stem, ext = os.path.splitext(n)
        if ext.lower() == ".mov" and size <= LIVE_VIDEO_MAX_BYTES:
            movs.setdefault(stem.lower(), n)
    out, used = {}, set()
    for n in sorted(files, key=lambda x: (os.path.splitext(x)[1].lower() not in (".heic", ".heif"), x)):
        stem, ext = os.path.splitext(n)
        if ext.lower() in LIVE_STILL_EXT and stem.lower() in movs and stem.lower() not in used:
            out[n] = movs[stem.lower()]
            used.add(stem.lower())  # IMG_1.JPG and IMG_1.HEIC cannot both own IMG_1.MOV
    return out


_jobs: dict = {}  # folder_id -> {"name", "done"}; one gunicorn worker (many threads), so this is the whole truth
_jobs_lock = threading.Lock()
_orphans = threading.Event()  # set when assets were removed: the workers then clean faces/geo/caches once
_orphans.set()  # once at start
_last_scan = {"at": 0.0}  # PH81: when a folder scan last finished


class _Unpicked(Exception):
    pass


def forget_unpicked(c):
    """Drop every asset that is no longer inside a picked folder (after an un-pick, or a scan that outlived it)."""
    keep = [r["path"].rstrip("/") + "/" for r in c.execute("SELECT path FROM folders")]
    gone = [(r["path"],) for r in c.execute("SELECT path FROM assets").fetchall() if not any(r["path"].startswith(k) for k in keep)]
    if gone:
        c.executemany("DELETE FROM assets WHERE path=?", gone)
        _orphans.set()
    c.commit()


def scan_folder(folder_id: str, root: Path, c=None):
    """Index root recursively. Read-only on the files; only DATA_DIR is written. Files are read with no transaction
    open and written in short batches, so user actions never wait behind a scan for the write lock."""
    own = c is None
    c = c or connect()
    seen, batch = set(), []
    bydir: dict = {}  # folder -> {file name: size}, to find Live Photo pairs
    errors = []  # folders os.walk could not read: their files are NOT treated as deleted
    job = _jobs.setdefault(folder_id, {"name": root.name, "done": 0})

    def flush():
        rows = [read_meta(folder_id, p, st) for p, st in batch]
        batch.clear()
        if c.execute("SELECT 1 FROM folders WHERE id=?", (folder_id,)).fetchone() is None:
            raise _Unpicked()  # un-picked while we were scanning: stop, do not bring its photos back
        write_rows(c, rows)
        c.commit()
    try:
        for dp, dn, fn in os.walk(root, onerror=errors.append):
            dn[:] = [d for d in dn if not d.startswith(".")]
            for f in fn:
                ext = Path(f).suffix.lower()
                if f.startswith(".") or ext not in IMAGE_EXT | VIDEO_EXT:
                    continue
                p = Path(dp) / f
                if p.is_symlink():  # never follow a link out of the library
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                seen.add(str(p))
                bydir.setdefault(dp, {})[f] = st.st_size
                job["done"] = len(seen)
                row = c.execute("SELECT mtime, ver FROM assets WHERE path=?", (str(p),)).fetchone()
                if row and row["mtime"] == st.st_mtime and row["ver"] == INDEX_VER:
                    continue
                batch.append((p, st))
                if len(batch) >= 200:
                    flush()
        flush()
        cond, par = under(str(root))
        known = [r["path"] for r in c.execute(f"SELECT path FROM assets WHERE {cond}", par).fetchall()]
        bad = [str(getattr(e, "filename", "") or "") for e in errors]
        if not root.is_dir() or str(root) in bad or (not seen and len(known) > 20):
            app.logger.warning("scan of %s: folder unreadable or empty, nothing removed", root)
            known = []
        gone = [(p,) for p in known if p not in seen and not any(p.startswith(b.rstrip("/") + "/") for b in bad if b)]
        if gone:
            c.executemany("DELETE FROM assets WHERE path=?", gone)
            _orphans.set()
        pairs = [(str(uuid.uuid5(NS, "file:" + os.path.join(dp, st_))), str(uuid.uuid5(NS, "file:" + os.path.join(dp, vd))))
                 for dp, files in bydir.items() for st_, vd in live_pairs(files).items()]
        c.execute(f"DELETE FROM live WHERE still_id IN (SELECT id FROM assets WHERE {cond})", par)
        c.executemany("INSERT OR REPLACE INTO live VALUES(?,?)", pairs)
        c.execute("DELETE FROM live WHERE still_id NOT IN (SELECT id FROM assets) OR video_id NOT IN (SELECT id FROM assets)")
        c.commit()
        fav_relink(c)
        hidden_sync(c)
    except _Unpicked:
        c.rollback()
        forget_unpicked(c)
    finally:
        with _jobs_lock:
            _jobs.pop(folder_id, None)
        _last_scan["at"] = time.time()
        if own:
            c.close()


def start_scan(folder_id: str, root: Path):
    """Scan in a background thread (a 58 GB library takes minutes; a request would time out). Tests scan inline."""
    with _jobs_lock:  # seed, the periodic rescan and a pick must not start the same folder twice
        if folder_id in _jobs:
            return
        _jobs[folder_id] = {"name": root.name, "done": 0}
    if app.config.get("TESTING"):
        return scan_folder(folder_id, root)
    threading.Thread(target=lambda: _safe_scan(folder_id, root), daemon=True).start()


def _safe_scan(folder_id, root):
    try:
        scan_folder(folder_id, root)
    except Exception:
        app.logger.exception("scan of %s failed", root)


def rescan_all():
    c = connect()
    try:
        for f in c.execute("SELECT * FROM folders").fetchall():
            if Path(f["path"]).is_dir():
                start_scan(f["id"], Path(f["path"]))
    finally:
        c.close()


def seed_default_albums():
    """PHOTOS_DEFAULT_ALBUMS (comma list, relative to the library root) are added to albums.yml once; an album the owner
    later un-picks stays gone because the names already seeded are remembered."""
    names = [n.strip() for n in cfg("PHOTOS_DEFAULT_ALBUMS").split(",") if n.strip()]
    if not names or not cfg("PHOTOS_BROWSE_ROOT"):
        return
    mark = data_dir() / "seeded.json"
    try:
        done = set(json.loads(mark.read_text()))
    except (OSError, ValueError):
        done = set()
    todo = [n for n in names if n not in done]
    if not todo:
        return
    apply_albums()  # makes albums.yml from the database if there is none yet
    try:
        up, ents = read_albums()
    except (OSError, ValueError, yaml.YAMLError) as e:
        app.logger.error("albums.yml unreadable, default albums not added: %s", e)
        return
    for n in todo:
        p = album_path(n)
        if p is not None and p.is_dir() and all(album_path(e) != p for e, _ in ents):
            ents.append((n, None))
        if p is not None and p.is_dir():
            done.add(n)
    write_albums(up, ents)
    mark.write_text(json.dumps(sorted(done)))
    apply_albums()


# ---- PH59: albums.yml, the album list as a file the owner can edit ----
ALBUMS_HEADER = """\
# Photos albums (PH59). Every folder listed here is an album, in the web app and in the Immich phone app.
# Paths are relative to the library root (PHOTOS_BROWSE_ROOT, /library in the container).
# An entry is a path, or {path: ..., name: ...} to show the album under another name.
# upload_folder: where web uploads go (PH58); created at the first upload, and an album once it exists.
# Edits are picked up within a few seconds. "Add folder" and removing an album in the web app rewrite this file.
"""
UPLOAD_DEFAULT = "_Organize"
_albums_seen = None  # (mtime_ns, size) of the albums.yml last applied
_albums_lock = threading.RLock()
_albums_checked = 0.0


def albums_file() -> Path:
    return Path(cfg("PHOTOS_ALBUMS_FILE") or data_dir() / "albums.yml")


def album_path(rel):
    """A path from albums.yml (relative to the library root) as an absolute Path, or None if it leaves the root."""
    p = (browse_root() / str(rel)).resolve()
    return p if inside_root(p) else None


def read_albums():
    """(upload folder, [(path, name or None)]) as written in albums.yml. Raises ValueError on a malformed file."""
    d = yaml.safe_load(albums_file().read_text()) or {}
    if not isinstance(d, dict):
        raise ValueError("the file must be a mapping with upload_folder and albums")
    if d.get("covers") is not None and not isinstance(d["covers"], dict):
        raise ValueError("covers must be a mapping of album path to photo path")
    up = d.get("upload_folder") or UPLOAD_DEFAULT
    if not isinstance(up, str):
        raise ValueError("upload_folder must be a path")
    out = []
    for e in d.get("albums") or []:
        if isinstance(e, str):
            out.append((e, None))
        elif isinstance(e, dict) and isinstance(e.get("path"), str):
            out.append((e["path"], str(e["name"]) if e.get("name") else None))
        else:
            raise ValueError(f"bad album entry: {e!r}")
    return up, out


def read_covers():
    """PH79: {album path (as listed): photo path relative to the library root} from albums.yml's `covers`."""
    try:
        d = yaml.safe_load(albums_file().read_text()) or {}
        cv = d.get("covers") if isinstance(d, dict) else None
        return {str(k): str(v) for k, v in cv.items()} if isinstance(cv, dict) else {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def album_covers():
    """{absolute album path: absolute cover photo path} for the albums that have a chosen cover."""
    if not cfg("PHOTOS_BROWSE_ROOT"):
        return {}
    out = {}
    for a, ph in read_covers().items():
        pa, pp = album_path(a), album_path(ph)
        if pa is not None and pp is not None:
            out[str(pa)] = str(pp)
    return out


def ordered_folders():
    """PH79: albums in albums.yml order (drag to reorder in the sidebar); ones not listed (the upload folder) last."""
    rows = db().execute("SELECT * FROM folders ORDER BY name COLLATE NOCASE").fetchall()
    try:
        order = {str(album_path(p)): i for i, (p, _) in enumerate(read_albums()[1]) if album_path(p) is not None}
    except (OSError, ValueError, yaml.YAMLError):
        order = {}
    return sorted(rows, key=lambda f: order.get(f["path"], len(order)))


def write_albums(up, ents, covers=None):
    """covers=None keeps the covers already in the file (for albums still listed or the upload folder)."""
    if covers is None:
        covers = read_covers()
    keep = {str(album_path(q)) for q, _ in ents} | {str(album_path(up))}
    covers = {a: v for a, v in covers.items() if str(album_path(a)) in keep}
    body = {"upload_folder": up, "albums": [p if not n else {"path": p, "name": n} for p, n in ents]}
    if covers:
        body["covers"] = covers
    f = albums_file()
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(ALBUMS_HEADER + yaml.safe_dump(body, sort_keys=False, allow_unicode=True, default_flow_style=False))
    os.replace(tmp, f)
    global _upl_cache
    _upl_cache = (0.0, "", None)  # PH62: the upload folder may have changed


def upload_dir():
    """Where web uploads go: albums.yml's upload_folder (default _Organize), or None if it points outside the library."""
    try:
        up = read_albums()[0]
    except (OSError, ValueError, yaml.YAMLError):
        up = UPLOAD_DEFAULT
    p = album_path(up)
    return p if p is not None and p != browse_root() else None


def apply_albums(force=False):
    """Make the album list match albums.yml (made from the database the first time). Albums no longer listed are
    forgotten (index only, never a file); a listed folder that is missing right now is kept, so an unmounted disk
    never empties the library; a malformed file changes nothing."""
    global _albums_seen
    if not cfg("PHOTOS_BROWSE_ROOT"):
        return
    with _albums_lock:
        f = albums_file()
        if not f.exists():
            c = connect()
            try:
                rows = c.execute("SELECT path, name FROM folders ORDER BY name").fetchall()
            finally:
                c.close()
            ents = []
            for r in rows:
                if inside_root(Path(r["path"])):
                    rel = str(Path(r["path"]).relative_to(browse_root()))
                    ents.append((rel, r["name"] if r["name"] != Path(r["path"]).name else None))
            write_albums(UPLOAD_DEFAULT, ents)
        st = f.stat()
        key = (st.st_mtime_ns, st.st_size)
        if key == _albums_seen and not force:
            return
        _albums_seen = key
        try:
            up, ents = read_albums()
        except (OSError, ValueError, yaml.YAMLError) as e:
            app.logger.error("albums.yml not applied, albums unchanged: %s", e)
            return
        listed, want = set(), {}
        for rel, name in ents:
            p = album_path(rel)
            if p is None:
                app.logger.warning("albums.yml: %s is outside the library, skipped", rel)
                continue
            listed.add(str(p))
            if p.is_dir():
                want[str(p)] = name or p.name
        u = upload_dir()
        if u is not None:
            listed.add(str(u))
            if u.is_dir():
                want.setdefault(str(u), u.name)
        c = connect()
        try:
            have = {r["path"]: r["name"] for r in c.execute("SELECT path, name FROM folders")}
            new = [p for p in want if p not in have]
            for p in new:
                c.execute("INSERT OR IGNORE INTO folders VALUES(?,?,?,?)", (aid(p), p, want[p], iso(time.time())))
            for p in want:
                if p in have and have[p] != want[p]:
                    c.execute("UPDATE folders SET name=? WHERE path=?", (want[p], p))
            drop = [p for p in have if p not in listed]
            c.executemany("DELETE FROM folders WHERE path=?", [(p,) for p in drop])
            c.commit()
            if drop:
                forget_unpicked(c)
        finally:
            c.close()
        for p in new:
            start_scan(aid(p), Path(p))


_rescan_started = False
_seeded = False


@app.before_request
def _seed_once():
    global _seeded
    if not _seeded:
        _seeded = True
        seed_default_albums()
        apply_albums()


@app.before_request
def _albums_follow_file():
    """Edits to albums.yml show within 5 s (one stat per check; every request in tests)."""
    global _albums_checked
    if not app.config.get("TESTING") and time.time() - _albums_checked < 5:
        return
    _albums_checked = time.time()
    try:
        apply_albums()
    except Exception:
        app.logger.exception("albums.yml check failed")


# ---- PH81: the library is watched (inotify), so a photo Syncthing drops in shows within seconds ----
_watch = {"on": False, "events": 0, "last": 0.0, "error": None}
_watch_pending: dict = {}  # path -> time of its last event
_watch_dirs: dict = {}  # folder (a whole folder moved/deleted) -> time
_watch_lock = threading.Lock()
WATCH_SETTLE = 5.0  # seconds a path must be quiet before it is indexed (Syncthing writes, then renames)


def _watch_state():
    return dict(on=_watch["on"], events=_watch["events"], lastEvent=iso(_watch["last"]) if _watch["last"] else None, error=_watch["error"])


def _media(path):
    n = os.path.basename(path)
    return not n.startswith(".") and os.path.splitext(n)[1].lower() in IMAGE_EXT | VIDEO_EXT


def watch_note(path, is_dir=False):
    """One file-system event (from the watcher, or a test)."""
    with _watch_lock:
        _watch["events"] += 1
        _watch["last"] = time.time()
        if is_dir:
            _watch_dirs[path] = time.time()
        elif _media(path):
            _watch_pending[path] = time.time()


def watch_apply(now=None, settle=WATCH_SETTLE):
    """Index what settled: a file that exists is (re)read; one that is gone is removed, but only when its folder is
    still there (an unmounted disk or a vanished parent never deletes rows: the next scan decides). A whole folder
    that moved or vanished gets a scan of the album it is in. Returns how many paths were handled."""
    now = now or time.time()
    with _watch_lock:
        ready = [p for p, t in _watch_pending.items() if now - t >= settle]
        for p in ready:
            _watch_pending.pop(p, None)
        dirs = [d for d, t in _watch_dirs.items() if now - t >= settle]
        for d in dirs:
            _watch_dirs.pop(d, None)
    if not ready and not dirs:
        return 0
    c = connect()
    try:
        folders = [dict(r) for r in c.execute("SELECT id, path FROM folders")]

        def album_of(p):
            best = None
            for f in folders:
                if p.startswith(f["path"].rstrip("/") + "/") and (best is None or len(f["path"]) > len(best["path"])):
                    best = f
            return best
        touched = set()
        for p in ready:
            f = album_of(p)
            if f is None or os.path.islink(p):
                continue
            if os.path.isfile(p):
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                row = c.execute("SELECT mtime, ver FROM assets WHERE path=?", (p,)).fetchone()
                if not (row and row["mtime"] == st.st_mtime and row["ver"] == INDEX_VER):
                    write_rows(c, [read_meta(f["id"], Path(p), st)])
                touched.add(os.path.dirname(p))
            elif os.path.isdir(os.path.dirname(p)) and os.path.isdir(f["path"]):
                if c.execute("DELETE FROM assets WHERE path=?", (p,)).rowcount:
                    _orphans.set()
                touched.add(os.path.dirname(p))
        c.commit()
        for d in touched:
            relink_live(c, Path(d))
        c.commit()
        if touched:
            fav_relink(c)
            hidden_sync(c)
        for d in dirs:
            f = album_of(d.rstrip("/") + "/x")
            if f is not None and os.path.isdir(f["path"]):
                start_scan(f["id"], Path(f["path"]))
        return len(ready) + len(dirs)
    finally:
        c.close()


def start_watcher():
    """Watch the library root (PHOTOS_WATCH=0 turns it off). Without the watchdog module, or past the inotify limit,
    the periodic full rescan stays the only way and the status says why."""
    if cfg("PHOTOS_WATCH", "1") == "0" or not cfg("PHOTOS_BROWSE_ROOT"):
        return False
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        _watch["error"] = "watchdog is not installed"
        return False

    class H(FileSystemEventHandler):
        def on_any_event(self, e):
            if e.event_type in ("opened", "closed_no_write"):
                return
            for path in (e.src_path, getattr(e, "dest_path", "") or ""):
                if path:
                    watch_note(os.fsdecode(path), e.is_directory and e.event_type in ("moved", "deleted"))
    try:
        obs = Observer()
        obs.schedule(H(), str(browse_root()), recursive=True)
        obs.daemon = True
        obs.start()
    except Exception as e:  # e.g. the inotify watch limit
        _watch["error"] = str(e)[:200]
        app.logger.warning("library watcher not started: %s", e)
        return False
    _watch["on"] = True

    def flusher():
        while True:
            time.sleep(2)
            try:
                watch_apply()
            except Exception:
                app.logger.exception("watcher apply failed")
    threading.Thread(target=flusher, daemon=True).start()
    return True


@app.before_request
def _periodic_rescan():
    """Albums follow their folders: the watcher (PH81) sees changes at once; a full rescan every PHOTOS_RESCAN_SECONDS
    (default 600; 6 hours while the watcher runs, as a safety net) catches anything it missed. 0 = off."""
    global _rescan_started
    every = int(cfg("PHOTOS_RESCAN_SECONDS", "600"))
    if _rescan_started or app.config.get("TESTING"):
        return
    _rescan_started = True
    if start_watcher():
        every = max(every, 21600) if every > 0 else 0
    if every <= 0:
        return

    def loop():
        while True:
            time.sleep(every)
            try:
                rescan_all()
            except Exception:
                app.logger.exception("periodic rescan failed")
    threading.Thread(target=loop, daemon=True).start()


def browse_root() -> Path:
    r = cfg("PHOTOS_BROWSE_ROOT")
    if not r:
        resp = jsonify(message="PHOTOS_BROWSE_ROOT is not set: no library to browse", error="Service Unavailable", statusCode=503)
        resp.status_code = 503
        abort(resp)
    return Path(r).resolve()


def inside_root(p: Path) -> bool:
    try:
        p.resolve().relative_to(browse_root())
        return True
    except ValueError:
        return False


@app.post("/ui/folders")
def add_folder():
    """Pick a folder: it becomes an album. Own endpoint (the Immich app has no folder picker)."""
    path = Path((request.get_json(silent=True) or {}).get("path", "")).resolve()
    if not path.is_dir() or not inside_root(path):
        return err("Not a folder inside the library root", 400, "Bad Request")
    with _albums_lock:
        apply_albums()
        try:
            up, ents = read_albums()
        except (OSError, ValueError, yaml.YAMLError) as e:
            return err(f"albums.yml cannot be read, fix it first: {e}", 409, "Conflict")
        if all(album_path(p) != path for p, _ in ents):
            ents.append((str(path.relative_to(browse_root())) if path != browse_root() else ".", None))
            write_albums(up, ents)
        apply_albums(force=True)
    return jsonify(id=aid(str(path)), path=str(path), scanning=not app.config.get("TESTING")), 202


UPLOAD_MAX_BYTES = int(cfg("PHOTOS_UPLOAD_MAX_MB", "4096")) * 1024 * 1024
_upload_lock = threading.Lock()
_uploading: set = set()  # checksums of uploads being saved right now


NAME_MAX_BYTES = 200  # Linux allows 255 bytes per name; leave room for " (12)" and the hidden ".part" name


def _upload_name(raw):
    """A safe file name from the browser's: no folders, no leading dot (scans skip those), a known photo/video type,
    no control characters (TAB/newline would travel to every device through Syncthing), at most 200 UTF-8 bytes (PH71)."""
    n = Path(str(raw).replace("\\", "/")).name
    n = re.sub(r"[\x00-\x1f\x7f]+", " ", n).strip().lstrip(".").strip()
    stem, ext = os.path.splitext(n)
    if not stem or ext.lower() not in IMAGE_EXT | VIDEO_EXT:
        return None
    room = NAME_MAX_BYTES - len(ext.encode())
    b = stem.encode()[:room]
    return b.decode("utf-8", "ignore").rstrip() + ext


def _real_media(path: Path, video: bool) -> bool:
    """PH71: the bytes are a photo/video, not just named like one (Pillow reads the header; ffprobe finds a stream)."""
    if not video:
        try:
            with Image.open(path) as im:
                im.verify()
            return True
        except Exception:
            return False
    try:
        out = subprocess.run(["ffprobe", *FF_SAFE, "-v", "error", "-show_entries", "stream=codec_type", "-print_format", "json",
                              "-i", str(path)], capture_output=True, timeout=30)
    except FileNotFoundError:
        return True  # no ffprobe (tests on a bare machine): only the extension is checked
    except subprocess.SubprocessError:
        return False
    try:
        return any(x.get("codec_type") == "video" for x in json.loads(out.stdout or b"{}").get("streams", []))
    except ValueError:
        return False


@app.post("/ui/upload")
def ui_upload():
    """PH58: one photo or video per request, the raw file as the body (?name=, ?mtime= in ms)."""
    n = request.content_length
    if n is None or n <= 0:
        return err("Empty upload", 400, "Bad Request")
    return _save_upload(request.args.get("name", ""), request.stream, n, request.args.get("mtime", ""))


def _save_upload(raw_name, stream, n, mtime=""):
    """Written byte for byte (nothing re-encoded, every bit of metadata kept) into the upload folder, made if missing;
    streamed straight to a hidden .part file there (the container's /tmp is small), then renamed. A file whose content
    is already in the library is not saved again. n = announced size, or None (share target: read to the end)."""
    name = _upload_name(raw_name)
    if not name:
        return err("Only photos and videos can be uploaded", 400, "Bad Request")
    if n is not None and n > UPLOAD_MAX_BYTES:
        return err("File too large", 413, "Payload Too Large")
    dest = upload_dir()
    if dest is None:
        return err("upload_folder in albums.yml is outside the library", 409, "Conflict")
    dest.mkdir(parents=True, exist_ok=True)
    import base64
    h, got = hashlib.sha1(), 0
    part = dest / f".{secrets.token_hex(6)}.part"  # the name is not repeated here: a 200-byte name plus this must fit 255
    target = None
    try:
        with open(part, "wb") as out:
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if (n is not None and got > n) or got > UPLOAD_MAX_BYTES:
                    raise ValueError("more data than announced" if n is not None else "file too large")
                h.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if (n is not None and got != n) or got == 0:
            raise ValueError("upload was cut short")
        if not _real_media(part, Path(name).suffix.lower() in VIDEO_EXT):
            part.unlink(missing_ok=True)
            return err("This file is not a photo or video it says it is", 415, "Unsupported Media Type")
        sha = base64.b64encode(h.digest()).decode()
        with _upload_lock:  # two uploads of the same bytes at once: only the first one is kept
            dup = db().execute("SELECT path FROM assets WHERE sha1=? LIMIT 1", (sha,)).fetchone()
            if dup or sha in _uploading:
                part.unlink()
                return jsonify(status="duplicate", name=name, existing=Path(dup["path"]).name if dup else name)
            _uploading.add(sha)
        try:
            ms = float(mtime or "")
            if 0 < ms < 4e12:
                os.utime(part, (ms / 1000, ms / 1000))  # the phone's own date for the file, when it sent one
        except ValueError:
            pass
        stem, ext = Path(name).stem, Path(name).suffix
        target, i = dest / name, 1
        while True:
            try:
                os.link(part, target)  # never replaces a file, even one another upload made a moment ago
                break
            except FileExistsError:
                target, i = dest / f"{stem} ({i}){ext}", i + 1
        part.unlink()
    except Exception as e:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass  # the cleanup must never replace the real error with a raw 500 (PH71)
        if "sha" in locals():
            _uploading.discard(sha)
        if isinstance(e, (ValueError, OSError)):
            return err(f"Upload failed: {e}", 400 if isinstance(e, ValueError) else 500, "Upload failed")
        raise
    try:
        apply_albums(force=True)  # the folder may be new: it becomes an album (its scan may index the file too)
        f = db().execute("SELECT id FROM folders WHERE path=?", (str(dest),)).fetchone()
        if f:
            c = connect()
            try:  # row and checksum in one transaction; a scan's later upsert keeps the checksum (same mtime and size)
                index_file(c, f["id"], target)
                c.execute("UPDATE assets SET sha1=? WHERE path=?", (sha, str(target)))
                c.commit()
            finally:
                c.close()
    finally:
        _uploading.discard(sha)
    return jsonify(status="saved", name=target.name, folder=str(dest.relative_to(browse_root())), album=f["id"] if f else None), 201


@app.post("/ui/share")
def ui_share():
    """PH87: the PWA share target (Android's share sheet -> Photos): multipart files saved like uploads, then back to
    the app with a summary. Its own auth check (a redirect, not JSON: this is a page navigation); the cookie is
    SameSite=Strict, so a cross-site form cannot post here with it."""
    if not authed():
        return Response(status=303, headers={"Location": "/#/all"})
    saved = dup = bad = 0
    for f in request.files.getlist("photos"):
        r = _save_upload(f.filename or "", f.stream, None)
        resp = r[0] if isinstance(r, tuple) else r
        st = (resp.get_json(silent=True) or {}).get("status")
        saved, dup, bad = saved + (st == "saved"), dup + (st == "duplicate"), bad + (st not in ("saved", "duplicate"))
    return Response(status=303, headers={"Location": f"/#/shared/{saved}/{dup}/{bad}"})


@app.post("/ui/rescan")
def rescan():
    rescan_all()
    return jsonify(ok=True), 202


@app.get("/ui/scan")
def scan_status():
    return jsonify([dict(id=k, name=v["name"], done=v["done"]) for k, v in list(_jobs.items())])


@app.get("/ui/scan/info")
def scan_info():
    """PH81: when the library was last fully scanned, and whether it is watched live."""
    return jsonify(lastScan=iso(_last_scan["at"]) if _last_scan["at"] else None, ago=int(time.time() - _last_scan["at"]) if _last_scan["at"] else None,
                   watching=_watch_state(), running=len(_jobs))


def under(root: str):
    """SQL range for "every path inside root, any depth": uses the unique index on path ('0' is the char after '/')."""
    return "(path >= ? AND path < ?)", [root.rstrip("/") + "/", root.rstrip("/") + "0"]


def folder_by_id(fid):
    return db().execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()


def _shown(f, up):
    """What an album lists: the upload album lists its own photos, which every other view leaves out."""
    return NOT_HIDDEN if Path(f["path"]) == up else NOT_LIVE_VIDEO


_NO_UP = object()


def album_dto(f, up=_NO_UP, covers=None):
    """up = upload_dir() of this request (computed once per list, not once per album: PH65)."""
    if up is _NO_UP:
        up = upload_dir()
    cond, par = under(f["path"])
    s = db().execute(f"SELECT COUNT(*) n, MIN(taken) a, MAX(taken) b FROM assets WHERE {cond} AND {_shown(f, up)}", par).fetchone()
    thumb = None
    cov = (covers if covers is not None else album_covers()).get(f["path"])
    if cov:  # PH79: the cover the owner chose, if that photo is still in the album
        thumb = db().execute(f"SELECT id FROM assets WHERE path=? AND {cond} AND {_shown(f, up)}", [cov] + par).fetchone()
    thumb = thumb or db().execute(f"SELECT id FROM assets WHERE {cond} AND {_shown(f, up)} ORDER BY taken DESC LIMIT 1", par).fetchone()
    nested = [r["name"] for r in db().execute("SELECT name FROM folders WHERE path>? AND path<? ", par)]
    return dict(id=f["id"], albumName=f["name"], description=f["path"], albumThumbnailAssetId=thumb["id"] if thumb else None,
                nested=nested, insideOf=[r["name"] for r in db().execute(
                    "SELECT name FROM folders WHERE ? > rtrim(path,'/') || '/' AND ? < rtrim(path,'/') || '0'", (f["path"], f["path"]))],
                albumUsers=[], assetCount=s["n"], createdAt=f["created"], updatedAt=f["created"], hasSharedLink=False,
                isActivityEnabled=False, shared=False, startDate=s["a"], endDate=s["b"],
                lastModifiedAssetTimestamp=s["b"], order="desc",
                isUploadFolder=up is not None and Path(f["path"]) == up,  # PH60: the one album the web app accepts drops on
                relPath=_rel(Path(f["path"])) if cfg("PHOTOS_BROWSE_ROOT") and inside_root(Path(f["path"])) else f["path"])


@app.get("/api/albums")
def albums():
    if request.args.get("shared") == "true":
        return jsonify([])
    up, covers = upload_dir(), album_covers()
    return jsonify([album_dto(f, up, covers) for f in ordered_folders()])


def rot_of(asset_id) -> int:
    """Clockwise degrees (0/90/180/270) the app shows this photo turned. The file itself is never changed."""
    r = db().execute("SELECT deg FROM rotations WHERE id=?", (asset_id,)).fetchone()
    return r["deg"] if r else 0


def live_video_of(asset_id):
    r = db().execute("SELECT video_id FROM live WHERE still_id=?", (asset_id,)).fetchone()
    return r["video_id"] if r else None


def _checksum(a):
    """Immich's checksum: base64 of the file's SHA-1, which the phone app matches against its camera roll. Until the
    background pass (checksum_pass) has read the file, the asset id stands in (it never matches a phone photo)."""
    return (a["sha1"] if "sha1" in a.keys() else None) or a["id"]


def _sha1_b64(path, chunk=8 << 20, pause=0.02):
    import base64
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
            time.sleep(pause)  # at most ~400 MB/s, usually far less: never saturates the disk for the rest of the machine
    return base64.b64encode(h.digest()).decode()


def checksum_pass(limit=20):
    """SHA-1 of files not hashed yet (a changed file is re-indexed, which clears its hash). Read with no transaction
    open; the row is written only if the file did not change meanwhile."""
    c = connect()
    try:
        todo = c.execute("SELECT id, path, mtime FROM assets WHERE sha1 IS NULL LIMIT ?", (limit,)).fetchall()
        for a in todo:
            try:
                h = _sha1_b64(a["path"])
            except OSError:
                h = ""  # unreadable: marked, so it is not retried every pass (cleared when the file changes)
            c.execute("UPDATE assets SET sha1=? WHERE id=? AND mtime=?", (h, a["id"], a["mtime"]))
            c.commit()
        return len(todo)
    finally:
        c.close()


def _uid():
    """user_id() once per request (it reads the account table)."""
    if "uid" not in g:
        g.uid = user_id()
    return g.uid


def dto_ctx():
    """Everything asset_dto needs, loaded once for a list (instead of 3-4 queries per row)."""
    d = db()
    return dict(favs=fav_paths(), rots={r[0]: r[1] for r in d.execute("SELECT id, deg FROM rotations")},
                lives={r[0]: r[1] for r in d.execute("SELECT still_id, video_id FROM live")},
                hidden={r[0] for r in d.execute("SELECT path FROM hidden")})


def _duration_ms(a):
    """Immich 3.x: video duration as integer milliseconds, null for still images (it was an "H:MM:SS.sssss" string)."""
    if a["kind"] != "VIDEO":
        return None
    sec = a["duration"] if "duration" in a.keys() else None
    return int(round(float(sec) * 1000)) if sec else 0


def asset_dto(a, favs=None, ctx=None):
    if ctx is None:
        ctx = dict(favs=fav_paths() if favs is None else favs, rots={a["id"]: rot_of(a["id"])},
                   lives={a["id"]: live_video_of(a["id"])}, hidden={a["path"]} if is_hidden(a["path"]) else set())
    w, h = (a["h"], a["w"]) if ctx["rots"].get(a["id"], 0) % 180 else (a["w"], a["h"])
    return dict(id=a["id"], type=a["kind"], originalFileName=a["name"], originalPath=a["path"], ownerId=_uid(),
                fileCreatedAt=a["taken"], fileModifiedAt=iso(a["mtime"]), createdAt=a["taken"], updatedAt=iso(a["mtime"]),
                localDateTime=a["taken"], width=w, height=h, duration=_duration_ms(a), isFavorite=a["path"] in ctx["favs"],
                isArchived=False, isTrashed=False, isOffline=False, isEdited=False, hasMetadata=True,
                visibility="locked" if a["path"] in ctx["hidden"] else "timeline", thumbhash=None, checksum=_checksum(a),
                livePhotoVideoId=ctx["lives"].get(a["id"]), people=[], tags=[],
                originalMimeType=mimetypes.guess_type(a["name"])[0], libraryId=None)


@app.get("/api/albums/<album_id>")
def album(album_id):
    f = folder_by_id(album_id)
    return jsonify(album_dto(f, upload_dir())) if f else err("Not found", 404, "Not Found")


@app.get("/api/albums/statistics")
def album_stats():
    n = db().execute("SELECT COUNT(*) FROM folders").fetchone()[0]
    return jsonify(owned=n, shared=0, notShared=n)


# ---- timeline ----
FORMATS = {"heic": (".heic", ".heif"), "jpg": (".jpg", ".jpeg", ".jpe", ".jfif"), "png": (".png",), "gif": (".gif",),
           "webp": (".webp",), "bmp": (".bmp",), "tiff": (".tif", ".tiff"), "avif": (".avif",),
           "mov": (".mov",), "mp4": (".mp4", ".m4v"), "avi": (".avi",), "flv": (".flv",), "wmv": (".wmv",),
           "mkv": (".mkv",), "webm": (".webm",), "3gp": (".3gp",), "mpeg": (".mpg", ".mpeg", ".mts", ".m2ts")}
_EXT_FMT = {e: k for k, v in FORMATS.items() for e in v}


def _bucket_rows(args):
    sql, par = f"SELECT * FROM assets WHERE {NOT_LIVE_VIDEO}", []
    if args.get("hidden") == "true" or args.get("visibility") == "locked":
        if not unlocked():
            abort(403)
        sql = f"SELECT * FROM assets WHERE {ONLY_HIDDEN}"
    if args.get("albumId"):
        f = folder_by_id(args["albumId"])
        if f and Path(f["path"]) == upload_dir():
            if not unlocked():
                abort(403)  # PH60: the upload album opens with the PIN, like Hidden
            sql = sql.replace(NOT_LIVE_VIDEO, NOT_HIDDEN)  # PH62: its photos are left out everywhere else
        cond, p = under(f["path"] if f else "/nonexistent")
        sql += " AND " + cond
        par += p
    if args.get("isFavorite") == "true":
        favs = sorted(fav_paths())
        sql += " AND path IN (%s)" % ",".join("?" * len(favs)) if favs else " AND 0"
        par += favs
    if args.get("place"):
        city, _, cc = args["place"].rpartition("|")
        sql += " AND id IN (SELECT asset_id FROM geo WHERE city=? AND cc=?)"
        par += [city, cc]
    if args.get("onThisDay", "").isdigit():
        _, days = _this_day(int(args["onThisDay"]), window=_window(args))
        sql += " AND " + _day_sql(days)
        par += days
    if args.get("thisMonth", "").isdigit():  # PH85: this month in an earlier year
        y, mth = _this_month(int(args["thisMonth"]))
        sql += " AND substr(taken,1,7)=?"
        par.append(f"{y}-{mth}")
    if args.get("bbox"):  # PH77: the photos of one spot on the map (south, west, north, east)
        try:
            so, we, no, ea = (float(v) for v in args["bbox"].split(","))
        except ValueError:
            abort(400)
        sql += " AND id IN (SELECT asset_id FROM geo WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?)"
        par += [so, no, we, ea]
    if args.get("personId"):
        sql += " AND id IN (SELECT asset_id FROM faces WHERE person_id=?)"
        par.append(args["personId"])
    if args.get("search"):  # PH57: the web search box; shown in date order like every other view
        hit = smart_ids(args["search"])
        sql += " AND id IN (%s)" % ",".join("?" * len(hit)) if hit else " AND 0"
        par += hit
    fmts = [f for f in (args.get("format") or "").split(",") if f in FORMATS]
    if fmts:
        exts = [e for f in fmts for e in FORMATS[f]]
        sql += " AND (" + " OR ".join(["name LIKE ?"] * len(exts)) + ")"  # LIKE is case-insensitive for ASCII
        par += ["%" + e for e in exts]
    return sql, par


PAGE = 500  # photos per group when the order is not by date
_EXT_SQL = "lower(substr(name, length(rtrim(name, replace(name, '.', ''))) + 1))"
SORTS = {"capture": ["taken"], "modified": ["mtime"], "name": ["name COLLATE NOCASE"], "size": ["size"],
         "type": [_EXT_SQL, "name COLLATE NOCASE"], "added": ["COALESCE(added, mtime)", "taken"]}
# PH75: a date-like sort is grouped by month (of that date); name/size/type by first letter, size band and extension,
# so the headers and the rail let you jump to a letter, a size or a type instead of "1 - 500".
MONTH_EXPR = {"capture": "substr(taken,1,7)", "modified": "strftime('%Y-%m', mtime, 'unixepoch', 'localtime')",
              "added": "strftime('%Y-%m', COALESCE(added, mtime), 'unixepoch', 'localtime')"}
SIZE_BANDS = [(1 << 20, "< 1 MB"), (5 << 20, "1 - 5 MB"), (20 << 20, "5 - 20 MB"), (100 << 20, "20 - 100 MB"), (1 << 30, "100 MB - 1 GB")]
GROUP_EXPR = {"name": "CASE WHEN upper(substr(name,1,1)) BETWEEN 'A' AND 'Z' THEN upper(substr(name,1,1)) ELSE '#' END",
              "size": "CASE " + " ".join(f"WHEN size < {b} THEN '{i}'" for i, (b, _) in enumerate(SIZE_BANDS)) + f" ELSE '{len(SIZE_BANDS)}' END",
              "type": _EXT_SQL}


def group_label(sort, g):
    if sort == "size":
        try:
            return (SIZE_BANDS[int(g)][1] if int(g) < len(SIZE_BANDS) else "> 1 GB")
        except ValueError:
            return g
    return (g or "?").upper() if sort == "type" else g


def _order(args):
    d = "ASC" if args.get("order") == "asc" else "DESC"
    key = SORTS.get(args.get("sort"), SORTS["capture"])
    return ", ".join(f"{k} {d}" for k in key) + f", id {d}"


def _sort_of(args):
    s_ = args.get("sort")
    if s_ == "match":
        return "match" if args.get("search") else "capture"  # "Best match" exists only for a search (PH76)
    return s_ if s_ in SORTS else "capture"


@app.get("/api/timeline/buckets")
def buckets():
    sql, par = _bucket_rows(request.args)
    sort, d = _sort_of(request.args), "ASC" if request.args.get("order") == "asc" else "DESC"
    if sort == "match":
        n = len([i for i in smart_ids(request.args["search"]) if i in _ids_of(sql, par)])
        return jsonify([dict(timeBucket=f"match:{i}", count=min(PAGE, n - i * PAGE), label="Best matches") for i in range((n + PAGE - 1) // PAGE)])
    if sort in GROUP_EXPR:
        out = []
        for r in db().execute(sql.replace("SELECT *", f"SELECT {GROUP_EXPR[sort]} gk, COUNT(*) n", 1) + f" GROUP BY gk ORDER BY {_group_order(sort, d)}", par):
            for i in range((r["n"] + PAGE - 1) // PAGE):  # a big group is cut into pages of PAGE
                out.append(dict(timeBucket=f"grp:{r['gk']}:{i}", count=min(PAGE, r["n"] - i * PAGE), label=group_label(sort, r["gk"])))
        return jsonify(out)
    m = MONTH_EXPR.get(sort, MONTH_EXPR["capture"])
    rows = db().execute(sql.replace("SELECT *", f"SELECT {m} m, COUNT(*) n", 1) + f" GROUP BY m ORDER BY m {d}", par).fetchall()
    return jsonify([dict(timeBucket=(r["m"] or "1970-01") + "-01T00:00:00.000Z", count=r["n"]) for r in rows])


def _group_order(sort, d):
    if sort == "size":
        return f"CAST(gk AS INTEGER) {d}"
    return f"gk = '#' {d}, gk {d}" if sort == "name" else f"gk {d}"


def _ids_of(sql, par):
    return {r[0] for r in db().execute(sql.replace("SELECT *", "SELECT id", 1), par)}


@app.get("/api/timeline/bucket")
def bucket():
    tb = request.args.get("timeBucket") or ""
    sql, par = _bucket_rows(request.args)
    sort = _sort_of(request.args)
    if tb.startswith(("page:", "match:", "grp:")):
        try:
            page = max(0, int(tb.rpartition(":")[2]))
        except ValueError:
            abort(400)
        if tb.startswith("match:"):
            rank = smart_ids(request.args.get("search", ""))
            shown = _ids_of(sql, par)
            part = [i for i in rank if i in shown][page * PAGE:(page + 1) * PAGE]
            got = {r["id"]: r for r in db().execute(f"SELECT * FROM assets WHERE id IN ({','.join('?' * len(part))})", part)} if part else {}
            rows = [got[i] for i in part if i in got]
        elif tb.startswith("grp:") and sort in GROUP_EXPR:
            gk = tb[4:].rpartition(":")[0]
            rows = db().execute(sql + f" AND {GROUP_EXPR[sort]}=? ORDER BY {_order(request.args)} LIMIT {PAGE} OFFSET {page * PAGE}", par + [gk]).fetchall()
        else:
            rows = db().execute(sql + f" ORDER BY {_order(request.args)} LIMIT {PAGE} OFFSET {page * PAGE}", par).fetchall()
    else:
        m = MONTH_EXPR.get(sort, MONTH_EXPR["capture"])
        args = {**request.args, "sort": sort if sort in MONTH_EXPR else "capture"}
        rows = db().execute(sql + f" AND {m}=? ORDER BY {_order(args)}", par + [tb[:7]]).fetchall()
    col = lambda f: [f(a) for a in rows]
    favs = fav_paths()
    rots, lives = {}, {}
    ids = [a["id"] for a in rows]
    for k in range(0, len(ids), 500):  # PH66: only this month's rows, not the whole tables per month
        part, q = ids[k:k + 500], ",".join("?" * len(ids[k:k + 500]))
        rots.update({r["id"]: r["deg"] for r in db().execute(f"SELECT id, deg FROM rotations WHERE id IN ({q})", part)})
        lives.update({r["still_id"]: r["video_id"] for r in db().execute(f"SELECT still_id, video_id FROM live WHERE still_id IN ({q})", part)})
    turned = {i for i, d in rots.items() if d in (90, 270)}
    ratio = lambda a: (a["h"] / a["w"] if a["id"] in turned else a["w"] / a["h"]) if a["h"] and a["w"] else 1
    return jsonify(
        id=col(lambda a: a["id"]), ownerId=[_uid()] * len(rows), isImage=col(lambda a: a["kind"] == "IMAGE"),
        fileName=col(lambda a: a["name"]),  # PH86: the web app labels tiles for screen readers
        isFavorite=col(lambda a: a["path"] in favs), isTrashed=col(lambda a: False), fileCreatedAt=col(lambda a: a["taken"]),
        createdAt=col(lambda a: a["taken"]), localOffsetHours=col(lambda a: 0),
        duration=col(_duration_ms),
        ratio=col(lambda a: round(ratio(a), 4)), rotation=col(lambda a: rots.get(a["id"], 0)), thumbhash=col(lambda a: None),
        livePhotoVideoId=col(lambda a: lives.get(a["id"])), projectionType=col(lambda a: None),
        visibility=col(lambda a: "locked" if request.args.get("hidden") == "true" or request.args.get("visibility") == "locked" else "timeline"), stack=col(lambda a: None), city=col(lambda a: None),
        country=col(lambda a: None), latitude=col(lambda a: None), longitude=col(lambda a: None))


# ---- assets (read-only) ----
def _asset(asset_id):
    a = db().execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone() or abort(404)
    if is_hidden(a["path"]) or _in_upload(a["path"]):  # also the clip of a hidden Live Photo; the upload album needs the PIN too
        if not unlocked():
            abort(403)
        g.private = True  # see _no_store
    return a


@app.after_request
def _no_store(resp):
    """A hidden photo must not stay in the browser cache after the album is locked again."""
    if g.get("private"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def cache_key(a):
    """Cache files are named after the asset AND its mtime: a file replaced at the same path gets new thumbnails."""
    return f"{a['id']}-{int((a['mtime'] or 0) * 1000)}"


def _file(a):
    """Path of an asset whose file still exists; a missing one answers 404. Only a scan removes rows (PH64): a disk that
    is briefly unmounted, or Syncthing mid-sync, must not delete index rows (and with them faces, places, search) on a GET."""
    if not os.path.isfile(a["path"]):
        abort(404)
    return a["path"]


@app.get("/api/assets/<asset_id>")
def asset(asset_id):
    return jsonify(asset_dto(_asset(asset_id)))


WEB_VCODECS, WEB_ACODECS, WEB_EXTS = {"h264", "vp8", "vp9", "av1"}, {"aac", "mp3", "opus", "vorbis"}, {".mp4", ".m4v", ".mov", ".webm"}
# HEVC (every recent iPhone video, all Live Photo clips) plays in Safari, the Immich app and most Chrome builds:
# it is sent as-is, and the web app asks for a transcode only when its <video> reports an error.
NATIVE_VCODECS = WEB_VCODECS | {"hevc"}
_playable: dict = {}  # (id, mtime) -> codec names; ffprobe once per file, not on every Range request
_tjobs: dict = {}  # asset id -> {"state", "error"} for background transcodes
_tslot = threading.Semaphore(1)  # one transcode at a time (it uses 4 cores; the face worker and llama.cpp need the rest)


def _codecs(a, p: Path):
    k = (a["id"], a["mtime"])
    if k not in _playable:
        try:
            out = subprocess.run(["ffprobe", *FF_SAFE, "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                                  "-print_format", "json", "-i", str(p)], capture_output=True, timeout=15, check=True).stdout
            _playable[k] = {s["codec_type"]: s["codec_name"] for s in json.loads(out).get("streams", [])}
        except Exception:
            _playable[k] = None
        if len(_playable) > 5000:
            _playable.pop(next(iter(_playable)))
    return _playable[k]


def web_playable(p: Path, a=None, codecs=WEB_VCODECS) -> bool:
    """True when a player can show the file as-is; an unprobeable file is sent untouched."""
    if p.suffix.lower() not in WEB_EXTS:
        return False
    names = _codecs(a or dict(id=str(p), mtime=p.stat().st_mtime), p)
    if names is None:
        return True
    return names.get("video") in codecs and names.get("audio", "aac") in WEB_ACODECS


def _transcode(src: Path, out: Path):
    tmp = out.with_name(out.name + f".{secrets.token_hex(4)}.part.mp4")
    try:
        r = subprocess.run(["nice", "-n", "10", "ffmpeg", *FF_SAFE, "-v", "error", "-nostdin", "-y", "-i", str(src),
                            "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264",
                            "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-threads", "4", "-c:a", "aac",
                            "-ac", "2", "-movflags", "+faststart", str(tmp)], capture_output=True, timeout=900)
        if r.returncode or not tmp.exists():
            raise OSError("ffmpeg failed: " + r.stderr.decode(errors="replace").strip()[-300:])
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)


def playback_path(a):
    return data_dir() / "playback" / f"{cache_key(a)}.mp4"


def playback_copy(a, wait=True):
    """Cached H.264/AAC MP4 of a video players cannot decode (3GP/H.263/AMR, AVI, WMV...). None if ffmpeg fails.
    wait=False starts the encode in the background and returns None until it is ready."""
    src = Path(_file(a))
    out = playback_path(a)
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)

    def run():
        with _tslot:
            if out.exists():
                return
            _tjobs[a["id"]] = {"state": "running"}
            try:
                _transcode(src, out)
                _tjobs[a["id"]] = {"state": "done"}
            except (OSError, subprocess.SubprocessError) as e:
                _tjobs[a["id"]] = {"state": "error", "error": str(e)[:300]}
    if wait:
        run()
        return out if out.exists() else None
    if _tjobs.get(a["id"], {}).get("state") not in ("queued", "running"):
        _tjobs[a["id"]] = {"state": "queued"}
        threading.Thread(target=run, daemon=True).start()
    return None


@app.get("/api/assets/<asset_id>/video/playback")
def video_playback(asset_id):
    """The phone app: HEVC and other natively playable videos as-is; others from the cached MP4 (made on first call)."""
    a = _asset(asset_id)
    if a["kind"] == "VIDEO" and not web_playable(Path(_file(a)), a, NATIVE_VCODECS):
        try:
            copy = playback_copy(a)
        except (OSError, subprocess.SubprocessError):
            copy = None
        if copy:
            return send_file(copy, mimetype="video/mp4", conditional=True)
    return original(asset_id)


@app.get("/ui/playback/<asset_id>")
def ui_playback(asset_id):
    """The web app plays the original first and comes here when its <video> fails. ?check=1 says whether the original
    plays as-is; ?state=1 starts the MP4 copy and reports queued/running/ready/error; without either, the MP4 copy
    (or 202 + state while it is made: no request ever waits for an encode)."""
    a = _asset(asset_id)
    src = Path(_file(a))
    if request.args.get("check"):
        return jsonify(native=a["kind"] != "VIDEO" or web_playable(src, a, NATIVE_VCODECS),
                       web=a["kind"] != "VIDEO" or web_playable(src, a))
    try:
        copy = playback_copy(a, wait=False)
    except (OSError, subprocess.SubprocessError):
        copy = None
    if copy and request.args.get("state"):
        return jsonify(state="ready")
    if copy:
        return send_file(copy, mimetype="video/mp4", conditional=True)
    job = _tjobs.get(a["id"], {"state": "queued"})
    return jsonify(job), 500 if job["state"] == "error" else 202


@app.get("/api/assets/<asset_id>/original")
def original(asset_id):
    a = _asset(asset_id)
    return send_file(_file(a), mimetype=mimetypes.guess_type(a["name"])[0] or "application/octet-stream",
                     conditional=True, as_attachment=request.args.get("download") == "1", download_name=a["name"])


BROWSER_IMG = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".webp", ".avif", ".bmp"}


_render_slot = threading.BoundedSemaphore(3)  # at most 3 decodes at once: other requests always keep a thread and memory


def _full_size(a, deg):
    """The photo at its own pixel size in a format every browser shows (HEIC/TIFF are rendered once and cached)."""
    if not os.path.isfile(a["path"]):  # PH93: disk unplugged: a cached render, else the best cached preview
        base = data_dir() / "thumbs" / f"{cache_key(a)}-full{deg}"
        for ext, mt in ((".jpg", "image/jpeg"), (".png", "image/png")):
            if base.with_suffix(ext).exists():
                return send_file(base.with_suffix(ext), mimetype=mt, max_age=300)
        out = cached_thumb(a, 1440) or abort(404)
        return send_file(out, mimetype="image/webp", max_age=300)
    p = _file(a)
    if not deg and os.path.splitext(a["name"])[1].lower() in BROWSER_IMG:
        return send_file(p, mimetype=mimetypes.guess_type(a["name"])[0], conditional=True, max_age=300)
    base = data_dir() / "thumbs" / f"{cache_key(a)}-full{deg}"
    for ext, mt in ((".jpg", "image/jpeg"), (".png", "image/png")):
        if base.with_suffix(ext).exists():
            return send_file(base.with_suffix(ext), mimetype=mt, max_age=300)
    try:
        with _render_slot, Image.open(p) as im:
            im = ImageOps.exif_transpose(im)
            if deg:
                im = im.rotate(-deg, expand=True)
            alpha = im.mode in ("RGBA", "LA", "P")
            out = base.with_suffix(".png" if alpha else ".jpg")
            tmp = out.with_name(f"{out.name}.{secrets.token_hex(4)}.tmp")
            im.convert("RGBA" if alpha else "RGB").save(tmp, "PNG" if alpha else "JPEG", **({} if alpha else {"quality": 92}))
            tmp.replace(out)
    except BAD_IMAGE:
        abort(404)
    return send_file(out, mimetype="image/png" if alpha else "image/jpeg", max_age=300)


def open_small(path, size):
    """Open an image for a size-px render as cheaply as possible: JPEG decodes at 1/2..1/8 scale, and HEIC uses its
    embedded thumbnail when that is big enough (pillow-heif's draft), instead of decoding all 12-48 MP."""
    im = Image.open(path)
    try:
        if im.format == "JPEG":
            im.draft("RGB", (size * 2, size * 2))  # DCT scaling: keep 2x for quality
        elif im.width and im.height:  # HEIC: the smallest embedded thumbnail that still fills a size x size box
            r = size / max(im.width, im.height)
            im.draft("RGB", (max(1, round(im.width * r)), max(1, round(im.height * r))))
    except Exception:
        pass
    return im


@app.get("/api/assets/<asset_id>/thumbnail")
def thumbnail(asset_id):
    a = _asset(asset_id)
    deg = rot_of(asset_id)
    if request.args.get("size") == "full" and a["kind"] == "IMAGE":
        return _full_size(a, deg)
    size = 1440 if request.args.get("size") in ("preview", "fullsize") else 250
    out = cached_thumb(a, size)
    if out is None:
        try:
            out = make_thumb(a, size, deg, _file(a))
        except BAD_IMAGE + (subprocess.SubprocessError,):
            abort(404)  # unreadable or undecodable: the tile shows its placeholder
    return send_file(out, mimetype="image/webp", max_age=86400)


def cached_thumb(a, size):
    """PH93: a render already in the cache is served without touching the original, so the library still shows while
    its disk is unplugged. With the original gone, the other size stands in (a sharp tile from the preview, or a soft
    preview from the tile) rather than nothing."""
    out = thumb_path(a, size)
    if out.exists():
        return out
    if not os.path.isfile(a["path"]):
        for other in (THUMB_SIZES[::-1] if size == 250 else THUMB_SIZES):
            if thumb_path(a, other).exists():
                return thumb_path(a, other)
    return None


THUMB_SIZES = (1440, 250)  # largest first


def thumb_path(a, size):
    return data_dir() / "thumbs" / f"{cache_key(a)}-{size}.webp"


def make_thumb(a, size, deg, path):
    """The cached size-px webp of an asset, made if missing (atomic: no half-written file is ever served)."""
    out = thumb_path(a, size)
    if out.exists():
        return out
    with _render_slot:
        im = video_frame(Path(path), size) if a["kind"] == "VIDEO" else open_small(path, size)
        with im:
            im = ImageOps.exif_transpose(im)
            if deg:
                im = im.rotate(-deg, expand=True)
            im.thumbnail((size, size))
            tmp = out.with_name(f"{out.name}.{secrets.token_hex(4)}.tmp")
            im.convert("RGB").save(tmp, "WEBP", quality=80)
            tmp.replace(out)
    return out


_thumb_failed: set = set()  # (cache key, size) that could not be rendered: not retried until the file changes
_thumbs_idle: dict = {}  # PH82: size -> (count, newest mtime, rotations) of the library when its last full pass found nothing to do


def thumbs_pass(limit=50, pause=0.05, size=250):
    """Make the size-px renders (photos and videos) that are missing, newest first, so a month opens without a decode
    storm and (PH93) the library still shows when its disk is unplugged. One render at a time and a pause between them:
    requests from the app always find a free render slot. A file that is not there right now (disk unplugged,
    Syncthing mid-sync) is skipped, not marked failed, and keeps the pass from going idle so it is tried again."""
    c = connect()
    try:
        key = tuple(c.execute("SELECT COUNT(*), MAX(mtime), (SELECT COUNT(*) FROM rotations) FROM assets").fetchone())
        if _thumbs_idle.get(size) == key:
            return 0  # nothing new since the last full walk: no 24,000 stat() calls every 5 minutes
        rots = {r[0]: r[1] for r in c.execute("SELECT id, deg FROM rotations")}
        n, absent = 0, 0
        for a in c.execute(f"SELECT * FROM assets WHERE {LIVE_CLIP} ORDER BY taken DESC"):
            if n >= limit:
                break
            k = (cache_key(a), size)
            if k in _thumb_failed or thumb_path(a, size).exists():
                continue
            if not os.path.isfile(a["path"]):
                absent += 1
                continue
            n += 1
            try:
                make_thumb(a, size, rots.get(a["id"], 0), a["path"])
            except BAD_IMAGE + (subprocess.SubprocessError,):
                _thumb_failed.add(k)
            time.sleep(pause)
        if n == 0 and absent == 0:
            _thumbs_idle[size] = key
        return n
    finally:
        c.close()


@app.put("/api/assets")
def update_assets():
    """The app may mark favorites; every other change is refused."""
    b = request.get_json(silent=True) or {}
    keys = set(b) - {"ids"}
    if not keys or keys - {"isFavorite", "visibility"} or not b.get("ids") or b.get("visibility", "timeline") not in ("timeline", "locked"):
        return refuse_write()
    if not isinstance(b["ids"], list) or len(b["ids"]) > 1000:
        return err("ids must be a list of up to 1000 asset ids", 400, "Bad Request")
    targets = [_asset(i) for i in b["ids"]]  # every id is checked (404/403) before anything changes: no partial apply
    for a in targets:  # "locked" is Immich's Locked folder = our Hidden album
        if "isFavorite" in b:
            set_favorite(a, bool(b["isFavorite"]))
        if "visibility" in b:
            set_hidden(a, b["visibility"] == "locked")
    return "", 204


@app.post("/api/assets")
@app.delete("/api/assets")
def refuse_write():  # PH6: no uploads, no batch deletes
    return err("This server is view-only: uploads and deletes are not supported", 403, "Forbidden")


@app.post("/api/assets/bulk-upload-check")
def bulk_check():
    items = (request.get_json(silent=True) or {}).get("assets", [])
    return jsonify(results=[dict(id=i.get("id"), action="reject", reason="duplicate") for i in items])


@app.get("/api/assets/statistics")
def asset_stats():
    r = db().execute("SELECT SUM(kind='IMAGE') i, SUM(kind='VIDEO') v, COUNT(*) t FROM assets WHERE " + NOT_LIVE_VIDEO).fetchone()
    return jsonify(images=r["i"] or 0, videos=r["v"] or 0, total=r["t"] or 0)


# ---- web-app helpers ----
@app.post("/ui/favorite")
def ui_favorite():
    b = request.get_json(silent=True) or {}
    set_favorite(_asset(b.get("id", "")), bool(b.get("favorite")))
    return jsonify(ok=True)


@app.get("/ui/tree")
def ui_tree():
    """Sub-folders of a folder inside the library root, for the folder picker."""
    p = Path(request.args.get("path") or browse_root()).resolve()
    if not p.is_dir() or not inside_root(p):
        return err("Not a folder inside the library root", 400, "Bad Request")
    picked = {r["path"] for r in db().execute("SELECT path FROM folders")}
    dirs = sorted((d for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")), key=lambda d: d.name.lower())
    return jsonify(path=str(p), root=str(browse_root()), parent=str(p.parent) if p != browse_root() else None,
                   dirs=[dict(name=d.name, path=str(d), picked=str(d) in picked) for d in dirs])


@app.delete("/ui/folders/<folder_id>")
def ui_unpick(folder_id):
    """Un-pick an album: takes it out of albums.yml and forgets the index only, never touches a file. A scan of it still
    running stops at its next batch (scan_folder checks the folder row)."""
    f = folder_by_id(folder_id)
    if f is None:
        return jsonify(ok=True)
    p = Path(f["path"])
    if p == upload_dir():
        return err("This is the upload folder: change upload_folder in albums.yml to stop showing it", 400, "Bad Request")
    with _albums_lock:
        try:
            up, ents = read_albums()
        except (OSError, ValueError, yaml.YAMLError) as e:
            return err(f"albums.yml cannot be read, fix it first: {e}", 409, "Conflict")
        write_albums(up, [(q, n) for q, n in ents if album_path(q) != p])
        apply_albums(force=True)
    return jsonify(ok=True)


def _rel(p):
    return str(p.relative_to(browse_root())) if p != browse_root() else "."


def _albums_edit(fn):
    """Read albums.yml under the lock, let fn(up, ents) -> (up, ents) or an error response, write it, re-apply."""
    with _albums_lock:
        apply_albums()
        try:
            up, ents = read_albums()
        except (OSError, ValueError, yaml.YAMLError) as e:
            return err(f"albums.yml cannot be read, fix it first: {e}", 409, "Conflict")
        r = fn(up, ents)
        if not isinstance(r, tuple):
            return r
        write_albums(*r)  # (up, ents) or (up, ents, covers)
        apply_albums(force=True)
    return None


@app.post("/ui/albums")
def ui_new_album():
    """PH61: a new album = a new empty folder under the library root (made here) listed in albums.yml."""
    name = str((request.get_json(silent=True) or {}).get("name", "")).strip()
    if not name or len(name) > 100 or name.startswith(".") or any(c in name for c in '/\\\0'):
        return err("Give a plain folder name (no slashes, not starting with a dot)", 400, "Bad Request")
    p = (browse_root() / name).resolve()
    if p.parent != browse_root().resolve():
        return err("Give a plain folder name", 400, "Bad Request")
    if p.exists():
        return err("A folder with that name already exists: use Add folder to pick it", 409, "Conflict")
    p.mkdir()
    bad = _albums_edit(lambda up, ents: (up, ents + [(_rel(p), None)]))
    return bad or (jsonify(id=aid(str(p)), path=str(p)), 201)


@app.put("/ui/folders/<folder_id>")
def ui_rename_album(folder_id):
    """PH61: show an album under another name (albums.yml `name`); the folder keeps its name. Empty = the folder's own."""
    f = folder_by_id(folder_id)
    if f is None:
        return err("Not found", 404, "Not Found")
    name = str((request.get_json(silent=True) or {}).get("name", "")).strip()[:100] or None
    p = Path(f["path"])

    def edit(up, ents):
        if p == upload_dir() and not any(album_path(q) == p for q, _ in ents):
            ents = ents + [(_rel(p), None)]  # the upload folder is only implicitly listed: naming it lists it
        return up, [(q, name if album_path(q) == p else n) for q, n in ents]
    return _albums_edit(edit) or jsonify(ok=True)


@app.post("/ui/folders/<folder_id>/upload-folder")
def ui_set_upload_folder(folder_id):
    """PH61: make this album the upload album (uploads land in it, it opens with the PIN). Needs the PIN itself: it moves the lock.
    The old upload folder stays listed as an ordinary album (remove it from the menu if you do not want it)."""
    if not unlocked():
        abort(403)
    f = folder_by_id(folder_id)
    if f is None:
        return err("Not found", 404, "Not Found")
    p, old = Path(f["path"]), upload_dir()

    def edit(up, ents):
        if old is not None and old != p and old.is_dir() and not any(album_path(q) == old for q, _ in ents):
            ents = ents + [(_rel(old), None)]
        return _rel(p), ents
    return _albums_edit(edit) or jsonify(ok=True)


@app.post("/ui/albums/order")
def ui_album_order():
    """PH79: the sidebar order (drag and drop): ids of the albums in the wanted order, written to albums.yml."""
    ids = (request.get_json(silent=True) or {}).get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return err("ids must be a list of album ids", 400, "Bad Request")
    rank = {i: k for k, i in enumerate(ids)}

    def edit(up, ents):
        return up, sorted(ents, key=lambda e: rank.get(aid(str(album_path(e[0]))), len(rank)))
    return _albums_edit(edit) or jsonify(ok=True)


@app.post("/ui/folders/<folder_id>/cover")
def ui_album_cover(folder_id):
    """PH79: "Use as album cover": the photo must be inside the album; {assetId: null} goes back to the newest photo."""
    f = folder_by_id(folder_id) or abort(404)
    a_id = (request.get_json(silent=True) or {}).get("assetId")
    rel = None
    if a_id:
        a = _asset(a_id)
        if not a["path"].startswith(f["path"].rstrip("/") + "/"):
            return err("That photo is not in this album", 400, "Bad Request")
        rel = _rel(Path(a["path"]))
    key = _rel(Path(f["path"]))

    def edit(up, ents):
        cv = {k: v for k, v in read_covers().items() if str(album_path(k)) != f["path"]}
        if rel:
            cv[key] = rel
        if Path(f["path"]) != upload_dir() and not any(album_path(q) == Path(f["path"]) for q, _ in ents):
            return err("Not an album in albums.yml", 409, "Conflict")
        return up, ents, cv
    return _albums_edit(edit) or jsonify(ok=True)


# ---- trash (PH18): one photo at a time, outside the Syncthing tree, restorable, 30 days ----
TRASH_DAYS = 30


def trash_dir():
    p = data_dir() / "trash"
    p.mkdir(exist_ok=True)
    return p


def trash_purge():
    cutoff = time.time() - TRASH_DAYS * 86400
    for d in trash_dir().iterdir():
        try:
            meta = json.loads((d / "meta.json").read_text())
            if meta["deleted"] < cutoff:
                shutil.rmtree(d)
        except (OSError, ValueError, KeyError):
            pass


def relink_live(c, directory: Path):
    """Pair the Live Photos of one folder again (after a restore)."""
    try:
        files = {de.name: de.stat().st_size for de in os.scandir(directory) if de.is_file() and not de.name.startswith(".")}
    except OSError:
        return
    for st_, vd in live_pairs(files).items():
        c.execute("INSERT OR REPLACE INTO live VALUES(?,?)", (str(uuid.uuid5(NS, "file:" + str(directory / st_))),
                                                              str(uuid.uuid5(NS, "file:" + str(directory / vd)))))
    c.execute("DELETE FROM live WHERE still_id NOT IN (SELECT id FROM assets) OR video_id NOT IN (SELECT id FROM assets)")


def _trash_asset(a, extra=None, hidden=None):
    """Move one file into the trash (a folder with the file and a meta.json) and forget it. Not a copy: the file leaves
    the library. The row goes first (committed), so a database lock error can never leave a moved file on the timeline."""
    src = Path(a["path"])
    if not src.is_file() or not inside_root(src):
        return False
    hidden = is_hidden(a["path"]) if hidden is None else hidden
    fav = a["path"] in fav_paths()
    dest = trash_dir() / a["id"]
    dest.mkdir(exist_ok=True)
    (dest / "meta.json").write_text(json.dumps(dict(
        id=a["id"], name=a["name"], path=a["path"], folder_id=a["folder_id"], kind=a["kind"], taken=a["taken"],
        size=a["size"], w=a["w"], h=a["h"], deleted=time.time(), hidden=hidden, favorite=fav,
        upload=bool(_in_upload(a["path"])), **(extra or {})), ensure_ascii=False))  # PH72: an Organize photo stays behind the PIN in Trash
    db().execute("DELETE FROM assets WHERE id=?", (a["id"],))
    db().commit()
    try:
        shutil.move(str(src), str(dest / a["name"]))
    except OSError:
        write_rows(db(), [tuple(a[k] if k in a.keys() else None for k in ROW_COLS)])
        db().commit()
        shutil.rmtree(dest, ignore_errors=True)
        raise
    _forget_marks(a["path"])
    if hidden:
        db().execute("DELETE FROM hidden WHERE path=?", (a["path"],))
    _orphans.set()
    return True


def _delete_asset(asset_id):
    """Move one photo (and its Live Photo clip) to the trash; False when the file is gone or outside the library."""
    a = _asset(asset_id)
    if not Path(a["path"]).is_file() or not inside_root(Path(a["path"])):
        return False
    vid = live_video_of(asset_id)
    v = db().execute("SELECT * FROM assets WHERE id=?", (vid,)).fetchone() if vid else None
    partner, hid = None, is_hidden(a["path"])
    if v is not None and _trash_asset(v, dict(liveOf=asset_id), hidden=hid):  # a Live Photo goes as one: its clip leaves with it
        partner = vid
    _trash_asset(a, dict(livePartner=partner) if partner else None, hidden=hid)
    db().execute("DELETE FROM live WHERE still_id=? OR video_id=?", (asset_id, asset_id))
    db().commit()
    return True


@app.delete("/ui/assets/<asset_id>")
def ui_delete(asset_id):
    if (request.get_json(silent=True) or {}).get("confirm") is not True:
        return err("Deleting needs confirm: true", 400, "Bad Request")
    if not _delete_asset(asset_id):
        return err("File not found", 404, "Not Found")
    return jsonify(ok=True)


@app.post("/ui/assets/delete")
def ui_delete_many():
    """Several photos picked by hand (never a folder or album) go to the trash; each one is restorable."""
    j = request.get_json(silent=True) or {}
    ids = j.get("ids")
    if j.get("confirm") is not True:
        return err("Deleting needs confirm: true", 400, "Bad Request")
    if not isinstance(ids, list) or not ids or len(ids) > 1000 or not all(isinstance(i, str) for i in ids):
        return err("ids must be 1 to 1000 asset ids", 400, "Bad Request")
    done = 0
    for i in dict.fromkeys(ids):
        try:
            done += bool(_delete_asset(i))
        except HTTPException:  # unknown (404) or hidden while locked (403): counts as failed, the rest still go
            pass
    return jsonify(deleted=done, failed=len(set(ids)) - done)


@app.delete("/ui/assets/<asset_id>/live-video")
def ui_delete_live_video(asset_id):
    """Delete only the clip of a Live Photo (to the trash, restorable); the photo stays and becomes a plain photo."""
    if (request.get_json(silent=True) or {}).get("confirm") is not True:
        return err("Deleting needs confirm: true", 400, "Bad Request")
    _asset(asset_id)
    vid = live_video_of(asset_id)
    v = db().execute("SELECT * FROM assets WHERE id=?", (vid,)).fetchone() if vid else None
    if v is None:
        return err("This photo has no Live Photo video", 404, "Not Found")
    _trash_asset(v, hidden=is_hidden(v["path"]))
    db().execute("DELETE FROM live WHERE still_id=?", (asset_id,))
    db().commit()
    return jsonify(ok=True)


def _trash_meta(asset_id):
    """meta.json of one trashed item; 404 if unknown, 403 if it was hidden and the album is locked."""
    d = trash_dir() / Path(asset_id).name
    try:
        m = json.loads((d / "meta.json").read_text())
    except (OSError, ValueError):
        abort(404)
    if m.get("hidden") or m.get("upload"):
        if not unlocked():
            abort(403)
        g.private = True
    return d, m


def _trash_items(open_):
    """(meta, folder) of every item the caller may see: the clip of a Live Photo goes with its photo; hidden and
    upload-album photos only while unlocked (PH72)."""
    out = []
    for d in trash_dir().iterdir():
        try:
            m = json.loads((d / "meta.json").read_text())
            if m.get("liveOf") or ((m.get("hidden") or m.get("upload")) and not open_):
                continue
            out.append((m, d))
        except (OSError, ValueError, KeyError):
            pass
    return out


@app.get("/ui/trash")
def ui_trash():
    """A read never deletes: expired items are purged by the background worker once a day (PH73)."""
    out = [dict(id=m["id"], name=m["name"], path=m["path"], deleted=iso(m["deleted"]), kind=m.get("kind"),
                daysLeft=max(0, int(TRASH_DAYS - (time.time() - m["deleted"]) / 86400)))
           for m, _ in _trash_items(unlocked())]
    return jsonify(sorted(out, key=lambda x: x["deleted"], reverse=True))


_trash_count_cache = (None, 0)


def trash_count():
    """How many items Trash lists, cached by the trash folder's mtime (PH65: the sidebar never reads every meta.json)."""
    global _trash_count_cache
    open_ = unlocked(slide=False)
    key = (trash_dir().stat().st_mtime_ns, open_)
    if _trash_count_cache[0] != key:
        _trash_count_cache = (key, len(_trash_items(open_)))
    return _trash_count_cache[1]


@app.get("/ui/counts")
def ui_counts():
    """PH63: the sidebar numbers: All photos counted like the timeline (nested albums once, no upload album, no hidden)."""
    n = db().execute(f"SELECT COUNT(*) FROM assets WHERE {NOT_LIVE_VIDEO}").fetchone()[0]
    fav = len([p for p in fav_paths()])
    return jsonify(all=n, trash=trash_count(), favorites=fav)


@app.get("/ui/trash/<asset_id>/thumbnail")
def ui_trash_thumb(asset_id):
    """Made once and kept beside meta.json (PH73), so opening Trash is no decode storm."""
    d, m = _trash_meta(asset_id)
    cached = d / ".thumb.webp"
    if not cached.exists():
        try:
            with _render_slot:
                src = d / m["name"]
                im = video_frame(src, 250) if m.get("kind") == "VIDEO" else open_small(src, 250)
                with im:
                    im = ImageOps.exif_transpose(im)
                    im.thumbnail((250, 250))
                    tmp = d / f".thumb.{secrets.token_hex(4)}.tmp"
                    im.convert("RGB").save(tmp, "WEBP", quality=70)
                    tmp.replace(cached)
        except BAD_IMAGE + (subprocess.SubprocessError, KeyError):
            abort(404)
    resp = send_file(cached, mimetype="image/webp", max_age=86400)
    resp.headers["Cache-Control"] = "private, max-age=86400"  # _no_store turns it into no-store for a private item
    return resp


def _free_name(dst: Path, taken=()):
    """'IMG_1 (1).jpg' style: the first name beside dst that is free (never overwrites, like uploads)."""
    stem, ext, i, out = dst.stem, dst.suffix, 1, dst
    while out.exists() or str(out) in taken:
        out, i = dst.with_name(f"{stem} ({i}){ext}"), i + 1
    return out


def _restore_check(d, dst=None):
    m = json.loads((d / "meta.json").read_text())
    dst = dst or Path(m["path"])
    if dst.exists():
        raise FileExistsError(dst)
    if not inside_root(dst):
        raise PermissionError(dst)
    return m


def _restore_one(d, dst=None):
    m = _restore_check(d, dst)
    dst = dst or Path(m["path"])
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(d / m["name"]), str(dst))
    shutil.rmtree(d)
    for f in db().execute("SELECT * FROM folders").fetchall():
        if str(dst).startswith(f["path"].rstrip("/") + "/"):
            index_file(db(), f["id"], dst)
            a = db().execute("SELECT * FROM assets WHERE path=?", (str(dst),)).fetchone()
            if a is not None and m.get("hidden") and not m.get("liveOf"):
                set_hidden(a, True)  # it was hidden when deleted: it comes back hidden
            if a is not None and m.get("favorite"):
                set_favorite(a, True)
            break
    return m


def _restore_item(d, m, rename=False):
    """Restore one item (and its Live Photo clip, both or neither). rename=True puts it back under a free name, the
    clip under the same new stem, when the old name is taken (PH74)."""
    pd = trash_dir() / m["livePartner"] if m.get("livePartner") else None
    if pd is not None and not (pd / "meta.json").exists():
        pd = None
    dst = Path(m["path"])
    pm = json.loads((pd / "meta.json").read_text()) if pd is not None else None
    pdst = Path(pm["path"]) if pm else None
    if rename and (dst.exists() or (pdst is not None and pdst.exists())):
        i = 1
        while True:
            dst = Path(m["path"]).with_name(f"{Path(m['path']).stem} ({i}){Path(m['path']).suffix}")
            pdst = Path(pm["path"]).with_name(f"{Path(m['path']).stem} ({i}){Path(pm['path']).suffix}") if pm else None
            if not dst.exists() and (pdst is None or not pdst.exists()):
                break
            i += 1
    for x, t in ((d, dst), (pd, pdst)):  # check both halves of a Live Photo before moving either: never half a restore
        if x is not None:
            _restore_check(x, t)
    _restore_one(d, dst)
    if pd is not None:
        _restore_one(pd, pdst)
    relink_live(db(), dst.parent)
    db().commit()
    return dst


@app.post("/ui/trash/<asset_id>/restore")
def ui_restore(asset_id):
    d, m = _trash_meta(asset_id)
    rename = bool((request.get_json(silent=True) or {}).get("rename"))
    try:
        dst = _restore_item(d, m, rename)
    except FileExistsError:
        return err("A file with that name is already there", 409, "Conflict")
    except PermissionError:
        return err("Original location is outside the library", 400, "Bad Request")
    return jsonify(ok=True, name=dst.name, renamed=dst != Path(m["path"]))


def _picked_trash(ids):
    """Trash items by id (all visible ones when ids is None), each gated like a single one."""
    if ids is None:
        return _trash_items(unlocked())
    if not isinstance(ids, list) or not ids or len(ids) > 5000 or not all(isinstance(i, str) for i in ids):
        abort(400)
    out = []
    for i in dict.fromkeys(ids):
        try:
            d, m = _trash_meta(i)
        except HTTPException:
            continue
        if not m.get("liveOf"):
            out.append((m, d))
    return out


@app.post("/ui/trash/restore")
def ui_restore_many():
    """PH74: Restore all (no ids) or the selected ones; a taken name is restored under a free one."""
    ok, failed = 0, 0
    for m, d in _picked_trash((request.get_json(silent=True) or {}).get("ids")):
        try:
            _restore_item(d, m, rename=True)
            ok += 1
        except (OSError, ValueError, KeyError):
            failed += 1
    return jsonify(restored=ok, failed=failed)


@app.post("/ui/trash/delete")
def ui_trash_delete():
    """PH74: Delete forever: the selected items, or every visible item with {"all": true, "confirm": "EMPTY"} (typed by
    the user). Hidden/upload items stay unless the album is unlocked. Besides the 30-day purge, the only place a file is destroyed."""
    b = request.get_json(silent=True) or {}
    if b.get("all"):
        if b.get("confirm") != "EMPTY":
            return err('Emptying the trash needs confirm: "EMPTY"', 400, "Bad Request")
        items = _picked_trash(None)
    else:
        if b.get("confirm") is not True:
            return err("Deleting forever needs confirm: true", 400, "Bad Request")
        items = _picked_trash(b.get("ids"))
    n = 0
    for m, d in items:
        if m.get("livePartner"):
            shutil.rmtree(trash_dir() / Path(m["livePartner"]).name, ignore_errors=True)
        shutil.rmtree(d, ignore_errors=True)
        n += 1
    return jsonify(deleted=n)


# ---- the web app ----
@app.get("/")
@app.get("/photos")
def index():
    return send_file(Path(__file__).with_name("index.html"), mimetype="text/html", max_age=0)


@app.get("/app.js")
def app_js():
    return send_file(Path(__file__).with_name("app.js"), mimetype="text/javascript", max_age=0)


@app.get("/manifest.webmanifest")
def manifest():
    """PH87: installable app; Android's share sheet lists Photos and posts the files to /ui/share."""
    m = dict(name="Negative", short_name="Negative", start_url="/#/all", scope="/", display="standalone",
             background_color="#edeef0", theme_color="#2c68a6",
             icons=[dict(src="/icon-192.png", sizes="192x192", type="image/png"), dict(src="/icon-512.png", sizes="512x512", type="image/png")],
             share_target=dict(action="/ui/share", method="POST", enctype="multipart/form-data",
                               params=dict(files=[dict(name="photos", accept=["image/*", "video/*", ".heic", ".heif"])])))
    return Response(json.dumps(m), mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return send_file(Path(__file__).with_name("sw.js"), mimetype="text/javascript", max_age=0)


@app.get("/icon-<int:n>.png")
def app_icon(n):
    if n not in (192, 512):
        abort(404)
    return send_file(Path(__file__).with_name(f"icon-{n}.png"), mimetype="image/png", max_age=86400)


VENDOR = {"leaflet.js": "text/javascript", "leaflet.markercluster.js": "text/javascript", "leaflet.css": "text/css",
          "MarkerCluster.css": "text/css", "MarkerCluster.Default.css": "text/css", "world.json": "application/json"}


@app.get("/vendor/<name>")
def vendor(name):
    """PH77: the map's libraries and world outline, served from here (nothing is fetched from the internet)."""
    if name not in VENDOR:
        abort(404)
    return send_file(Path(__file__).with_name("vendor") / name, mimetype=VENDOR[name], max_age=86400)


@app.get("/favicon.png")
@app.get("/favicon.ico")
def favicon():
    return send_file(Path(__file__).with_name("favicon.png"), mimetype="image/png", max_age=86400)


# ---- formats and conversions ----
@app.get("/ui/formats")
def ui_formats():
    """Count per file format in the current scope (album / favorites), ignoring any format filter."""
    sql, par = _bucket_rows({k: v for k, v in request.args.items() if k != "format"})
    n: dict = {}
    for r in db().execute(sql.replace("SELECT *", f"SELECT {_EXT_SQL} e, COUNT(*) k", 1) + " GROUP BY e", par):  # PH66: counted in SQL
        f = _EXT_FMT.get("." + (r["e"] or ""))
        if f:
            n[f] = n.get(f, 0) + r["k"]
    return jsonify([dict(format=k, count=n[k]) for k in FORMATS if k in n])


@app.get("/ui/convert/ops")
def ui_convert_ops():
    a = _asset(request.args.get("id", ""))
    return jsonify(convert.ops_for(os.path.splitext(a["name"])[1]))


_cjobs: dict = {}  # job id -> {"state","progress","assetId","name","error"}
_vid_slot = _tslot  # video conversions share the transcode slot: one ffmpeg encode at a time in the whole app
_img_slot = threading.Semaphore(1)  # one image conversion at a time: a big resize needs hundreds of MB


def _index_new(folder_id, out: Path, hidden=False):
    """Index a converted file; the copy of a hidden photo is hidden too (else it would show on the timeline)."""
    with app.app_context():
        index_file(db(), folder_id, out)
        db().commit()
        new = db().execute("SELECT * FROM assets WHERE path=?", (str(out),)).fetchone()
        if hidden and new is not None:
            set_hidden(new, True)
    return str(uuid.uuid5(NS, "file:" + str(out)))


@app.post("/ui/convert")
def ui_convert():
    b = request.get_json(silent=True) or {}
    a = _asset(b.get("id", ""))
    src = Path(_file(a))
    video = a["kind"] == "VIDEO"
    try:
        p = convert.check(b.get("op", ""), src.suffix, b, video)
    except convert.BadRequest as e:
        return err(str(e), 400, "Bad Request")
    work = data_dir() / "convert-tmp"
    work.mkdir(exist_ok=True)
    op, fid, hid = b["op"], a["folder_id"], is_hidden(a["path"])
    if not video:
        try:
            with _img_slot:
                out = convert.convert_image(src, op, p, work)
        except convert.BadRequest as e:
            return err(str(e), 400, "Bad Request")
        except BAD_IMAGE as e:
            return err("Could not convert: %s" % e, 422, "Unprocessable Entity")
        return jsonify(assetId=_index_new(fid, out, hid), name=out.name), 201
    jid = secrets.token_hex(6)
    job = _cjobs[jid] = {"state": "queued", "progress": 0, "name": src.name}
    for k in [k for k, v in _cjobs.items() if v["state"] in ("done", "error")][:-20]:
        _cjobs.pop(k, None)  # keep the last 20 finished jobs

    def run():
        with _vid_slot:
            job["state"] = "running"
            try:
                out = convert.convert_video(src, op, p, work, lambda n: job.update(progress=n))
                job.update(state="done", progress=100, assetId=_index_new(fid, out, hid), name=out.name)
            except Exception as e:
                app.logger.exception("video conversion failed")
                job.update(state="error", error=str(e))
    threading.Thread(target=run, daemon=True).start()
    return jsonify(job=jid), 202


@app.get("/ui/convert/<jid>")
def ui_convert_job(jid):
    return jsonify(_cjobs.get(jid) or abort(404))


@app.post("/ui/rotate")
def ui_rotate():
    """Turn a photo left or right in the app. Stored here only: the file on disk is not modified."""
    b = request.get_json(silent=True) or {}
    a = _asset(b.get("id", ""))
    if a["kind"] != "IMAGE" or b.get("direction") not in ("left", "right"):
        return err("Only photos can be turned, left or right", 400, "Bad Request")
    if not os.path.isfile(a["path"]):  # PH93: turning drops the cached renders, which could not be made again now
        return err("The photo's disk is not connected right now; try again when it is", 409, "Conflict")
    deg = (rot_of(a["id"]) + (90 if b["direction"] == "right" else 270)) % 360
    if deg:
        db().execute("INSERT OR REPLACE INTO rotations VALUES(?,?)", (a["id"], deg))
    else:
        db().execute("DELETE FROM rotations WHERE id=?", (a["id"],))
    db().commit()
    for f in (data_dir() / "thumbs").glob(f"{a['id']}-*.webp"):
        f.unlink(missing_ok=True)
    return jsonify(rotation=deg)


def _fmt(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace").strip("\x00") if len(v) <= 64 and v.isascii() else f"({len(v)} bytes)"
    if isinstance(v, (tuple, list)):
        return ", ".join(_fmt(x) for x in v[:16]) + (" ..." if len(v) > 16 else "")
    if hasattr(v, "numerator") and not isinstance(v, int):
        return f"{float(v):.6g}"
    return str(v)[:300]


DATE_SOURCES = {"exif": "the photo's EXIF capture date", "video": "the video's creation time", "name": "the file name",
                "folder": "the folder name", "file": "the file's modification time"}


def human_size(n):
    """PH84: 17,835 -> '17.4 KB'."""
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,} bytes" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def nice_date(v):
    """PH84: the stored wall time ('2019-08-23T10:00:00.000Z', the Z is Immich's, not UTC) as '23 Aug 2019, 10:00'."""
    try:
        d = datetime.strptime(str(v)[:19], "%Y-%m-%dT%H:%M:%S")
        return f"{d.day} {d.strftime('%b %Y, %H:%M')}"
    except ValueError:
        return str(v)


def _dms(v, ref):
    d = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
    return -d if ref in ("S", "W") else d


def image_metadata(p: Path):
    sec = []
    with Image.open(p) as im:
        info = [("Format", im.format), ("Mode", im.mode), ("Pixels", f"{im.width} x {im.height}")]
        if getattr(im, "n_frames", 1) > 1:
            info.append(("Frames", im.n_frames))
        if im.info.get("dpi"):
            info.append(("DPI", _fmt(im.info["dpi"])))
        icc = im.info.get("icc_profile")
        if icc:
            try:
                from PIL import ImageCms
                info.append(("Colour profile", ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip()))
            except Exception:
                info.append(("Colour profile", f"embedded ({len(icc)} bytes)"))
        sec.append(("Image", info))
        ex = im.getexif()
        groups = [("Camera and picture (EXIF)", {**dict(ex), **ex.get_ifd(0x8769), **ex.get_ifd(0xA005)}, ExifTags.TAGS)]
        gps = ex.get_ifd(0x8825)
        for title, d, names in groups:
            rows = [(names.get(k, f"Tag 0x{k:04X}"), _fmt(v)) for k, v in d.items()
                    if k not in (0x8769, 0x8825, 0xA005, 0x927C, 0x02BC, 0x8773)]  # sub-IFD pointers, MakerNote, XMP, ICC
            if rows:
                sec.append((title, sorted(rows)))
        if gps:
            rows = [(ExifTags.GPSTAGS.get(k, f"Tag {k}"), _fmt(v)) for k, v in gps.items()]
            try:
                rows.insert(0, ("Location", f"{_dms(gps[2], gps[1]):.6f}, {_dms(gps[4], gps[3]):.6f}"))
            except Exception:
                pass
            sec.append(("GPS", rows))
        text = [(k, _fmt(v)) for k, v in im.info.items() if isinstance(v, str)]
        if text:
            sec.append(("Text", text))
    return sec


def video_metadata(p: Path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)],
                         capture_output=True, timeout=20, check=True).stdout
    j = json.loads(out)
    f = j.get("format", {})
    sec = [("Video file", [("Container", f.get("format_long_name")), ("Duration", f"{float(f.get('duration') or 0):.2f} s"),
                           ("Bit rate", f"{int(f.get('bit_rate') or 0) // 1000} kb/s")] +
                          [(k, _fmt(v)) for k, v in (f.get("tags") or {}).items()])]
    for i, s in enumerate(j.get("streams", [])):
        rows = [("Codec", s.get("codec_long_name") or s.get("codec_name"))]
        if s.get("codec_type") == "video":
            rows += [("Size", f"{s.get('width')} x {s.get('height')}"), ("Frame rate", s.get("avg_frame_rate")),
                     ("Pixel format", s.get("pix_fmt"))]
        elif s.get("codec_type") == "audio":
            rows += [("Sample rate", s.get("sample_rate")), ("Channels", s.get("channels"))]
        rows += [(k, _fmt(v)) for k, v in (s.get("tags") or {}).items()]
        sec.append((f"Stream {i}: {s.get('codec_type')}", [(k, v) for k, v in rows if v not in (None, "", "0/0")]))
    return sec


@app.get("/ui/metadata/<asset_id>")
def ui_metadata(asset_id):
    """Everything readable about one file: filesystem facts, then image EXIF/GPS/profile or video streams."""
    a = _asset(asset_id)
    p = Path(_file(a))
    st = p.stat()
    src = a["date_src"] if "date_src" in a.keys() else None
    rel = _rel(p) if inside_root(p) else a["path"]
    sec = [("File", [("Name", a["name"]), ("Folder", str(Path(rel).parent)), ("Size", human_size(st.st_size)),
                     ("Type", mimetypes.guess_type(a["name"])[0] or "unknown"), ("Modified", nice_date(local_iso(st.st_mtime))),
                     ("Date shown", nice_date(a["taken"])), ("Date from", DATE_SOURCES.get(src, "unknown"))]
            + ([("Added", nice_date(local_iso(a["added"])))] if "added" in a.keys() and a["added"] else [])
            + ([("Shown turned", f"{rot_of(a['id'])} degrees (the file is unchanged)")] if rot_of(a["id"]) else []))]
    gp = db().execute("SELECT lat, lon, city, cc FROM geo WHERE asset_id=? AND lat IS NOT NULL", (a["id"],)).fetchone()
    if gp:
        sec.append(("Place", [("Location", f"{gp['lat']:.5f}, {gp['lon']:.5f}")] + ([("Near", f"{gp['city']}, {gp['cc']}")] if gp["city"] else [])
                    + [("Map", f"#/map/{gp['lat']:.5f},{gp['lon']:.5f}")]))
    try:
        sec += video_metadata(p) if a["kind"] == "VIDEO" else image_metadata(p)
        lv = db().execute("SELECT a.* FROM live l JOIN assets a ON a.id=l.video_id WHERE l.still_id=?", (a["id"],)).fetchone()
        if lv:
            sec.append(("Live Photo video", [("Name", lv["name"]), ("Size", f"{lv['size']:,} bytes")]))
            sec += [("Live Photo video: " + t, r) for t, r in video_metadata(Path(lv["path"]))[:1]]
    except Exception as e:
        sec.append(("Metadata", [("Could not read", str(e)[:200])]))
    return jsonify([dict(title=t, rows=[[k, "" if v is None else str(v)] for k, v in r]) for t, r in sec])


# ---- PH77 map, PH80 library dashboard ----
@app.get("/ui/map")
def ui_map():
    """Every photo with GPS as compact columns (id, lat, lon), the same set the timeline shows (never hidden or upload
    album photos). The page clusters them; nothing leaves the tailnet (no tile server: a bundled world outline)."""
    rows = db().execute(f"SELECT g.asset_id id, g.lat, g.lon FROM geo g WHERE g.lat IS NOT NULL "
                        f"AND g.asset_id IN (SELECT id FROM assets WHERE {NOT_LIVE_VIDEO})").fetchall()
    return jsonify(id=[r["id"] for r in rows], lat=[round(r["lat"], 5) for r in rows], lon=[round(r["lon"], 5) for r in rows])


@app.get("/ui/stats")
def ui_stats():
    """One page of numbers: per album, formats, oldest/newest, background workers, last scan, disk free."""
    d, up = db(), upload_dir()
    albums_ = []
    for f in ordered_folders():
        cond, par = under(f["path"])
        r = d.execute(f"SELECT SUM(kind='IMAGE') i, SUM(kind='VIDEO') v, COALESCE(SUM(size),0) b, MIN(taken) a, MAX(taken) z "
                      f"FROM assets WHERE {cond} AND {_shown(f, up)}", par).fetchone()
        albums_.append(dict(id=f["id"], name=f["name"], photos=r["i"] or 0, videos=r["v"] or 0, bytes=r["b"], first=r["a"], last=r["z"],
                            upload=up is not None and Path(f["path"]) == up))
    tot = d.execute(f"SELECT SUM(kind='IMAGE') i, SUM(kind='VIDEO') v, COALESCE(SUM(size),0) b, MIN(taken) a, MAX(taken) z FROM assets WHERE {NOT_LIVE_VIDEO}").fetchone()
    old = d.execute(f"SELECT id, name, taken FROM assets WHERE {NOT_LIVE_VIDEO} ORDER BY taken ASC LIMIT 1").fetchone()
    new = d.execute(f"SELECT id, name, taken FROM assets WHERE {NOT_LIVE_VIDEO} ORDER BY taken DESC LIMIT 1").fetchone()
    fmts = {}
    for r in d.execute(f"SELECT {_EXT_SQL} e, COUNT(*) k, COALESCE(SUM(size),0) b FROM assets WHERE {NOT_LIVE_VIDEO} GROUP BY e"):
        k = _EXT_FMT.get("." + (r["e"] or ""), r["e"] or "?")
        x = fmts.setdefault(k, [0, 0])
        x[0] += r["k"]
        x[1] += r["b"]
    imgs = d.execute("SELECT COUNT(*) FROM assets WHERE kind='IMAGE'").fetchone()[0]
    allv = d.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    src = {r[0] or "?": r[1] for r in d.execute(f"SELECT date_src, COUNT(*) FROM assets WHERE {NOT_LIVE_VIDEO} GROUP BY date_src")}
    try:
        du = shutil.disk_usage(str(browse_root()))
        disk = dict(free=du.free, total=du.total)
    except Exception:
        disk = None
    pick = lambda r: dict(id=r["id"], name=r["name"], taken=r["taken"]) if r else None
    return jsonify(
        total=dict(photos=tot["i"] or 0, videos=tot["v"] or 0, bytes=tot["b"]), albums=albums_, oldest=pick(old), newest=pick(new),
        formats=sorted([dict(format=k, count=v[0], bytes=v[1]) for k, v in fmts.items()], key=lambda x: -x["count"]),
        dates=src,
        workers=dict(
            thumbnails=dict(done=sum(1 for f in (data_dir() / "thumbs").glob("*-250.webp")), of=allv),
            faces=dict(done=d.execute("SELECT COUNT(*) FROM face_done WHERE asset_id IN (SELECT id FROM assets)").fetchone()[0], of=imgs),
            search=dict(done=d.execute("SELECT COUNT(*) FROM clip WHERE asset_id IN (SELECT id FROM assets)").fetchone()[0], of=allv),
            places=dict(done=d.execute("SELECT COUNT(*) FROM geo WHERE asset_id IN (SELECT id FROM assets)").fetchone()[0], of=allv,
                        withGps=d.execute("SELECT COUNT(*) FROM geo WHERE lat IS NOT NULL").fetchone()[0]),
            checksums=dict(done=d.execute("SELECT COUNT(*) FROM assets WHERE sha1 IS NOT NULL").fetchone()[0], of=allv)),
        people=d.execute("SELECT COUNT(*) FROM persons").fetchone()[0], named=d.execute("SELECT COUNT(*) FROM persons WHERE name!=''").fetchone()[0],
        lastScan=iso(_last_scan["at"]) if _last_scan["at"] else None, watching=_watch_state(), disk=disk)


# ---- people (faces) ----
_engine = None
_faces_started = False


def _person_dto(r, count=None):
    return dict(id=r["id"], name=r["name"], birthDate=None, thumbnailPath=f"/api/people/{r['id']}/thumbnail",
                isHidden=bool(r["hidden"]), isFavorite=False, updatedAt=iso(time.time()),
                otherPeople=[], sharedBy=[], sharedWith=[])


def _person_cache(c):
    rows = [dict(r) for r in c.execute("SELECT id, centroid, n, cover_q FROM persons")]
    cents = np.stack([np.frombuffer(r["centroid"], np.float32) for r in rows]) if rows else np.zeros((0, 128), np.float32)
    return {"rows": rows, "cents": cents}


def place_face(c, face_id, emb, score, area, cache=None):
    """Give a new face to the person it matches, or start a new (unnamed) person with it. The worker is the only caller.
    cache keeps the centroid matrix between faces of one pass instead of re-reading every person for every face."""
    if cache is None:
        cache = _person_cache(c)
    k, _ = faces.assign(cache["cents"], emb)
    if k is not None and c.execute("SELECT 1 FROM persons WHERE id=?", (cache["rows"][k]["id"],)).fetchone() is None:
        cache.update(_person_cache(c))  # merged or removed from the app meanwhile: start from the database again
        k, _ = faces.assign(cache["cents"], emb)
    q = score * area
    if k is None:
        pid = str(uuid.uuid4())
        c.execute("INSERT INTO persons(id, centroid, n, cover, cover_q) VALUES(?,?,?,?,?)", (pid, emb.tobytes(), 1, face_id, q))
        cache["rows"].append(dict(id=pid, centroid=emb.tobytes(), n=1, cover_q=q))
        cache["cents"] = np.vstack([cache["cents"], emb[None, :].astype(np.float32)])
    else:
        r = cache["rows"][k]
        pid = r["id"]
        cen = faces.merge_centroid(cache["cents"][k], r["n"], emb, 1)
        c.execute("UPDATE persons SET centroid=?, n=n+1 WHERE id=?", (cen.tobytes(), pid))
        cache["cents"][k], r["n"] = cen, r["n"] + 1
        if q > (r["cover_q"] or 0):
            c.execute("UPDATE persons SET cover=?, cover_q=? WHERE id=?", (face_id, q, pid))
            r["cover_q"] = q
    c.execute("UPDATE faces SET person_id=? WHERE id=?", (pid, face_id))


def refresh_person(c, pid):
    """Recount one person from the faces it still has: n, centroid and cover follow; a person with no faces goes."""
    left = c.execute("SELECT id, emb, score, w, h FROM faces WHERE person_id=?", (pid,)).fetchall()
    if not left:
        c.execute("DELETE FROM persons WHERE id=?", (pid,))
        return
    m = np.mean([np.frombuffer(r["emb"], np.float32) for r in left], axis=0)
    best = max(left, key=lambda r: (r["score"] or 0) * (r["w"] or 0) * (r["h"] or 0))
    c.execute("UPDATE persons SET centroid=?, n=?, cover=?, cover_q=? WHERE id=?",
              ((m / (np.linalg.norm(m) or 1)).astype(np.float32).tobytes(), len(left), best["id"],
               (best["score"] or 0) * (best["w"] or 0) * (best["h"] or 0), pid))


def clean_orphans(c):
    """Faces, places and cached files of assets that are gone. Run only after something was removed (_orphans)."""
    hit = [r[0] for r in c.execute("SELECT DISTINCT person_id FROM faces WHERE person_id IS NOT NULL "
                                   "AND asset_id NOT IN (SELECT id FROM assets)")]
    c.execute("DELETE FROM faces WHERE asset_id NOT IN (SELECT id FROM assets)")
    c.execute("DELETE FROM face_manual WHERE face_id NOT IN (SELECT id FROM faces)")
    c.execute("DELETE FROM face_done WHERE asset_id NOT IN (SELECT id FROM assets)")
    c.execute("DELETE FROM geo WHERE asset_id NOT IN (SELECT id FROM assets)")
    c.execute("DELETE FROM clip WHERE asset_id NOT IN (SELECT id FROM assets)")
    for pid in hit:
        refresh_person(c, pid)
    c.commit()
    prune_caches(c)


def prune_caches(c):
    """Delete thumbnails, full-size renders, face crops and playback copies whose asset is gone or has changed."""
    keys = {f"{r['id']}-{int((r['mtime'] or 0) * 1000)}" for r in c.execute("SELECT id, mtime FROM assets")}
    fids = {str(r[0]) for r in c.execute("SELECT id FROM faces")}
    for d in (data_dir() / "thumbs", data_dir() / "playback"):
        if not d.is_dir():
            continue
        for f in d.iterdir():
            n = f.name
            if n.endswith(".tmp") or ".part." in n:
                if time.time() - f.stat().st_mtime > 3600:
                    f.unlink(missing_ok=True)
                continue
            if n.startswith("face-"):
                keep = n[5:].split(".")[0] in fids
            else:
                parts = n.split("-")
                keep = len(parts) >= 6 and "-".join(parts[:6]).split(".")[0] in keys
            if not keep:
                f.unlink(missing_ok=True)


RECLUSTER_SIM = 0.45  # stricter than SAME_PERSON: only groups that are clearly one person are joined


def recluster_pass(sim=RECLUSTER_SIM):
    """Join unnamed groups that are the same person (the greedy first pass fragments people: it depends on scan order).
    Average linkage over the groups' centroids, weighted by size. Only unnamed, not-hidden groups take part: named
    people, people marked 'Don't show more' and merges you made are never changed, and a face you moved out with
    'Not this person' is never joined back to that group. Returns how many groups were merged away."""
    c = connect()
    try:
        rows = c.execute("SELECT id, centroid, n FROM persons WHERE name='' AND COALESCE(hidden,0)=0 AND n>0").fetchall()
        if len(rows) < 2:
            return 0
        ids = [r["id"] for r in rows]
        idx = {p: i for i, p in enumerate(ids)}
        C = np.stack([np.frombuffer(r["centroid"], np.float32) for r in rows]).copy()
        n = np.array([r["n"] for r in rows], np.float64)
        forbid = [set() for _ in ids]  # group index -> group ids it must not join
        for f in c.execute("SELECT fn.person_id bad, f.person_id cur FROM face_not fn JOIN faces f ON f.id=fn.face_id"):
            if f["cur"] in idx:
                forbid[idx[f["cur"]]].add(f["bad"])
            if f["bad"] in idx:
                forbid[idx[f["bad"]]].add(f["cur"])
        alive = np.ones(len(ids), bool)
        S = C @ C.T
        np.fill_diagonal(S, -1)
        for i, bad in enumerate(forbid):
            for p in bad:
                if p in idx:
                    S[i, idx[p]] = S[idx[p], i] = -1
        merged_into: dict = {}
        while True:
            k = int(np.argmax(S))
            i, j = divmod(k, len(ids))
            if S[i, j] < sim:
                break
            if n[j] > n[i]:
                i, j = j, i  # the bigger group keeps its id
            v = C[i] * n[i] + C[j] * n[j]
            C[i] = v / (np.linalg.norm(v) or 1)
            n[i] += n[j]
            alive[j] = False
            merged_into[ids[j]] = ids[i]
            forbid[i] |= forbid[j]
            S[j, :] = S[:, j] = -1
            row = C @ C[i]
            row[~alive] = -1
            row[i] = -1
            for p in forbid[i]:
                if p in idx:
                    row[idx[p]] = -1
            for jj, f_ in enumerate(forbid):  # a group that may not join j may not join i now either
                if ids[j] in f_:
                    f_.add(ids[i])
                    row[jj] = -1
            S[i, :] = S[:, i] = row
        if not merged_into:
            return 0

        def root(p):
            while p in merged_into:
                p = merged_into[p]
            return p
        for src in merged_into:
            c.execute("UPDATE faces SET person_id=? WHERE person_id=?", (root(src), src))
            c.execute("DELETE FROM persons WHERE id=?", (src,))
        for dst in {root(p) for p in merged_into}:
            refresh_person(c, dst)
        c.commit()
        return len(merged_into)
    finally:
        c.close()


def _maybe_clean(c):
    if _orphans.is_set():
        _orphans.clear()
        clean_orphans(c)


def faces_pass(limit=100):
    """Detect faces in photos not looked at yet (or changed since). Returns how many photos were processed."""
    global _engine
    c = connect()
    try:
        _maybe_clean(c)
        todo = c.execute("SELECT a.id, a.path, a.mtime FROM assets a LEFT JOIN face_done d ON d.asset_id=a.id "
                         "WHERE a.kind='IMAGE' AND (d.asset_id IS NULL OR d.mtime!=a.mtime) LIMIT ?", (limit,)).fetchall()
        if todo and _engine is None:
            _engine = faces.Engine()
        cache = _person_cache(c) if todo else None
        for a in todo:
            try:
                found = _engine.detect(a["path"])  # slow: runs with no transaction open
            except Exception:
                found = []  # unreadable photo: remembered as done so it is not retried every pass
            by_hand = "id NOT IN (SELECT face_id FROM face_manual)"   # PH49: hand-placed tags survive a re-scan
            olds = [r[0] for r in c.execute(f"SELECT DISTINCT person_id FROM faces WHERE asset_id=? AND person_id IS NOT NULL AND {by_hand}", (a["id"],))]
            c.execute(f"DELETE FROM faces WHERE asset_id=? AND {by_hand}", (a["id"],))  # the write lock is held only from here to the commit
            for pid in olds:
                refresh_person(c, pid)
            for x, y, w, h, score, emb in found:
                x, y, w, h, score = (float(v) for v in (x, y, w, h, score))  # numpy scalars would be stored as blobs
                cur = c.execute("INSERT INTO faces(asset_id,x,y,w,h,score,emb) VALUES(?,?,?,?,?,?,?)",
                                (a["id"], x, y, w, h, score, emb.tobytes()))
                place_face(c, cur.lastrowid, emb, score, w * h, cache)
            c.execute("INSERT OR REPLACE INTO face_done VALUES(?,?)", (a["id"], a["mtime"]))
            c.commit()
        return len(todo)
    finally:
        c.close()


@app.before_request
def _faces_worker():
    """One background thread looks for faces in new photos, a little at a time (PHOTOS_FACES=0 turns it off)."""
    global _faces_started
    if _faces_started or app.config.get("TESTING") or cfg("PHOTOS_FACES", "1") == "0" or not faces.available():
        return
    _faces_started = True

    def loop():
        last_rc, faces_at_rc = 0.0, -1
        while True:
            try:
                if not faces_pass():
                    # idle: re-cluster unnamed groups at most every 6 hours, and only if faces were added since
                    c = connect()
                    try:
                        nf = c.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
                    finally:
                        c.close()
                    if nf != faces_at_rc and time.time() - last_rc > 6 * 3600:
                        app.logger.info("re-cluster: %d unnamed groups joined", recluster_pass())
                        last_rc, faces_at_rc = time.time(), nf
                    time.sleep(30)
            except Exception:
                app.logger.exception("face pass failed")
                time.sleep(60)
    threading.Thread(target=loop, daemon=True).start()


_bg_started = False


@app.before_request
def _bg_worker():
    """Thumbnails and checksums, in one low-priority thread, after the rest: a little work, then a pause."""
    global _bg_started
    if _bg_started or app.config.get("TESTING"):
        return
    _bg_started = True

    def loop():
        time.sleep(60)
        last_daily = 0.0
        while True:
            try:
                if time.time() - last_daily > 86400:
                    last_daily = time.time()
                    daily_sweep()
                busy = (thumbs_pass() or thumbs_pass(size=1440)) + checksum_pass()  # PH93: previews once the grid is done
                time.sleep(2 if busy else 300)
            except Exception:
                app.logger.exception("background pass failed")
                time.sleep(300)
    threading.Thread(target=loop, daemon=True).start()


def daily_sweep():
    """Once a day: expired logins (PH67: a phone that never comes back leaves no rows) and trash older than 30 days
    (PH73: a GET never deletes)."""
    c = connect()
    try:
        c.execute("DELETE FROM tokens WHERE created < ?", (time.time() - TOKEN_DAYS * 86400,))
        c.execute("DELETE FROM sessions WHERE hash NOT IN (SELECT hash FROM tokens)")
        c.commit()
    finally:
        c.close()
    trash_purge()


def people_rows(hidden=False, everyone=False):
    """People worth listing: named, or seen in at least MIN_SHOW photos. Most photos first."""
    having = "" if everyone else f"HAVING p.name != '' OR cnt >= {faces.MIN_SHOW}"
    return db().execute(
        f"SELECT p.id, p.name, p.hidden, COUNT(DISTINCT f.asset_id) cnt FROM persons p "
        f"JOIN faces f ON f.person_id=p.id JOIN assets a ON a.id=f.asset_id WHERE p.hidden=? AND a.path NOT IN (SELECT path FROM hidden) AND NOT in_upload(a.path) GROUP BY p.id {having} "
        f"ORDER BY (p.name = ''), cnt DESC, p.name", (1 if hidden else 0,)).fetchall()


@app.get("/ui/people")
def ui_people():
    return jsonify([dict(id=r["id"], name=r["name"], count=r["cnt"]) for r in people_rows(request.args.get("hidden") == "1")])


@app.get("/ui/faces/status")
def ui_faces_status():
    d = db()
    total = d.execute("SELECT COUNT(*) FROM assets WHERE kind='IMAGE'").fetchone()[0]
    done = d.execute("SELECT COUNT(*) FROM face_done WHERE asset_id IN (SELECT id FROM assets)").fetchone()[0]
    return jsonify(enabled=faces.available() and cfg("PHOTOS_FACES", "1") != "0", photos=total, scanned=done,
                   faces=d.execute("SELECT COUNT(*) FROM faces").fetchone()[0],
                   people=len(people_rows()))


def _person(pid):
    return db().execute("SELECT * FROM persons WHERE id=?", (pid,)).fetchone() or abort(404)


@app.put("/ui/people/<pid>")
def ui_person_update(pid):
    p = _person(pid)
    b = request.get_json(silent=True) or {}
    name = str(b["name"]).strip()[:100] if "name" in b else p["name"]
    hidden = int(bool(b["isHidden"] if "isHidden" in b else b.get("hidden", p["hidden"])))
    db().execute("UPDATE persons SET name=?, hidden=? WHERE id=?", (name, hidden, pid))
    db().commit()
    return jsonify(id=pid, name=name, hidden=bool(hidden))


@app.post("/ui/people/hide-unnamed")
def ui_people_hide_unnamed():
    """PH83: hide (Don't show more) every unnamed person seen in fewer than `below` photos (2..50); returns their ids for Undo."""
    b = request.get_json(silent=True) or {}
    try:
        below = min(50, max(2, int(b.get("below", 5))))
    except (TypeError, ValueError):
        return err("below must be a number", 400, "Bad Request")
    if b.get("confirm") is not True:
        return err("Needs confirm: true", 400, "Bad Request")
    ids = [r["id"] for r in people_rows(everyone=True) if not r["name"] and r["cnt"] < below]
    db().executemany("UPDATE persons SET hidden=1 WHERE id=?", [(i,) for i in ids])
    db().commit()
    return jsonify(hidden=len(ids), ids=ids)


@app.post("/ui/people/show")
def ui_people_show():
    """Undo of hide-unnamed: show these people again."""
    ids = (request.get_json(silent=True) or {}).get("ids")
    if not isinstance(ids, list) or len(ids) > 20000 or not all(isinstance(i, str) for i in ids):
        return err("ids must be a list", 400, "Bad Request")
    db().executemany("UPDATE persons SET hidden=0 WHERE id=?", [(i,) for i in ids])
    db().commit()
    return jsonify(shown=len(ids))


@app.post("/ui/people/<pid>/merge")
def ui_person_merge(pid):
    """Fold this person into another one (same person, found as two groups)."""
    src, dst = _person(pid), _person((request.get_json(silent=True) or {}).get("into", ""))
    if src["id"] == dst["id"]:
        return err("Pick a different person", 400, "Bad Request")
    c = db()
    cen = faces.merge_centroid(np.frombuffer(dst["centroid"], np.float32), dst["n"], np.frombuffer(src["centroid"], np.float32), src["n"])
    c.execute("UPDATE faces SET person_id=? WHERE person_id=?", (dst["id"], src["id"]))
    best = src if (src["cover_q"] or 0) > (dst["cover_q"] or 0) else dst
    c.execute("UPDATE persons SET centroid=?, n=?, name=?, cover=?, cover_q=? WHERE id=?",
              (cen.tobytes(), dst["n"] + src["n"], dst["name"] or src["name"], best["cover"], best["cover_q"], dst["id"]))
    c.execute("DELETE FROM persons WHERE id=?", (src["id"],))
    c.commit()
    return jsonify(id=dst["id"])


SUGGEST_MIN = 0.30  # centroid similarity below SAME_PERSON (0.40, which would already have joined them) but close enough to ask


@app.get("/ui/people/suggestions")
def ui_people_suggestions():
    """PH55: unnamed groups that look like a named person: one question each, best match first."""
    c = db()
    named = [r for r in people_rows(everyone=True) if r["name"]]
    cen = {r["id"]: np.frombuffer(c.execute("SELECT centroid FROM persons WHERE id=?", (r["id"],)).fetchone()[0], np.float32) for r in named}
    no = {(r[0], r[1]) for r in c.execute("SELECT src, dst FROM merge_no")}
    out = []
    for r in people_rows():
        if r["name"]:
            continue
        v = np.frombuffer(c.execute("SELECT centroid FROM persons WHERE id=?", (r["id"],)).fetchone()[0], np.float32)
        best = max(((float(cen[n["id"]] @ v), n) for n in named if (r["id"], n["id"]) not in no), key=lambda t: t[0], default=None)
        if best and best[0] >= SUGGEST_MIN:
            out.append(dict(id=r["id"], count=r["cnt"], intoId=best[1]["id"], intoName=best[1]["name"], score=round(best[0], 2)))
    return jsonify(sorted(out, key=lambda x: -x["score"])[:12])


@app.post("/ui/people/<pid>/not-same")
def ui_people_not_same(pid):
    _person(pid)
    other = (request.get_json(silent=True) or {}).get("as", "")
    _person(other)
    db().execute("INSERT OR IGNORE INTO merge_no VALUES(?,?)", (pid, other))
    db().commit()
    return jsonify(ok=True)


@app.get("/ui/people/<pid>/faces")
def ui_person_faces(pid):
    """PH56: this person's faces, the least like their average first, so a wrong one is easy to find."""
    p = _person(pid)
    cen = np.frombuffer(p["centroid"], np.float32)
    rows = db().execute("SELECT f.id, f.asset_id, f.emb FROM faces f JOIN assets a ON a.id=f.asset_id "
                        "WHERE f.person_id=? AND a.path NOT IN (SELECT path FROM hidden) AND NOT in_upload(a.path)", (pid,)).fetchall()
    sims = sorted(((float(np.frombuffer(r["emb"], np.float32) @ cen), r) for r in rows), key=lambda t: t[0])[:48]
    return jsonify([dict(faceId=r["id"], assetId=r["asset_id"], score=round(sc, 2)) for sc, r in sims])


@app.get("/ui/assets/<asset_id>/faces")
def ui_asset_faces(asset_id):
    _asset(asset_id)
    rows = db().execute("SELECT f.id fid, f.person_id, p.name FROM faces f LEFT JOIN persons p ON p.id=f.person_id "
                        "WHERE f.asset_id=? AND COALESCE(p.hidden, 0)=0 ORDER BY f.x", (asset_id,)).fetchall()
    return jsonify([dict(faceId=r["fid"], personId=r["person_id"], name=r["name"] or "") for r in rows])


TAG_BOX = 0.12   # side of the box (fraction of the picture) for a face the detector missed


@app.post("/ui/assets/<asset_id>/faces")
def ui_tag_person(asset_id):
    """PH49: tag a person who already has a name at a point (x, y = fractions of the picture as the viewer shows it, rotation
    included). A detected face under the point is given to that person; otherwise a small box is added there."""
    a = _asset(asset_id)
    j = request.get_json(silent=True) or {}
    c = db()
    pid = j.get("personId")
    p = c.execute("SELECT * FROM persons WHERE id=?", (pid,)).fetchone() if isinstance(pid, str) else None
    if p is None or not p["name"] or not p["centroid"]:
        return err("Pick a person who already has a name", 400, "Bad Request")
    if a["kind"] != "IMAGE":
        return err("Only photos can be tagged", 400, "Bad Request")
    try:
        u, v = float(j["x"]), float(j["y"])
    except (KeyError, TypeError, ValueError):
        return err("x and y must be numbers between 0 and 1", 400, "Bad Request")
    if not (0 <= u <= 1 and 0 <= v <= 1):
        return err("x and y must be numbers between 0 and 1", 400, "Bad Request")
    x, y = {90: (v, 1 - u), 180: (1 - u, 1 - v), 270: (1 - v, u)}.get(rot_of(asset_id), (u, v))  # back to the file's own orientation
    hit = [r for r in c.execute("SELECT * FROM faces WHERE asset_id=?", (asset_id,))
           if r["x"] <= x <= r["x"] + r["w"] and r["y"] <= y <= r["y"] + r["h"]]
    if hit:
        f = min(hit, key=lambda r: r["w"] * r["h"])
        fid, old = f["id"], f["person_id"]
        if old != pid:
            if old:
                op = c.execute("SELECT name, n FROM persons WHERE id=?", (old,)).fetchone()
                if op and op["name"] and c.execute("SELECT COUNT(*) FROM faces WHERE person_id=?", (old,)).fetchone()[0] <= 1:
                    return err(f"That is the only face of {op['name']}", 409, "Conflict")
            c.execute("UPDATE faces SET person_id=? WHERE id=?", (pid, fid))
            if old:
                c.execute("INSERT OR IGNORE INTO face_not VALUES(?,?)", (fid, old))  # re-clustering must not put it back
                refresh_person(c, old)
            refresh_person(c, pid)
    else:
        s = TAG_BOX
        cur = c.execute("INSERT INTO faces(asset_id,x,y,w,h,score,emb,person_id) VALUES(?,?,?,?,?,?,?,?)",
                        (asset_id, min(max(x - s / 2, 0), 1 - s), min(max(y - s / 2, 0), 1 - s), s, s, 0.05, p["centroid"], pid))
        fid = cur.lastrowid
        c.execute("INSERT INTO face_manual VALUES(?)", (fid,))
        refresh_person(c, pid)
    c.commit()
    return jsonify(faceId=fid, personId=pid, name=p["name"], moved=bool(hit))


@app.put("/ui/faces/<int:fid>/person")
def ui_face_person(fid):
    """PH51: this face on this photo is someone else (another person who already has a name)."""
    c = db()
    f = c.execute("SELECT * FROM faces WHERE id=?", (fid,)).fetchone() or abort(404)
    _asset(f["asset_id"])  # a hidden photo needs the PIN
    pid = (request.get_json(silent=True) or {}).get("personId")
    p = c.execute("SELECT * FROM persons WHERE id=?", (pid,)).fetchone() if isinstance(pid, str) else None
    if p is None or not p["name"]:
        return err("Pick a person who already has a name", 400, "Bad Request")
    old = f["person_id"]
    if old != pid:
        op = c.execute("SELECT name FROM persons WHERE id=?", (old,)).fetchone() if old else None
        if op and op["name"] and c.execute("SELECT COUNT(*) FROM faces WHERE person_id=?", (old,)).fetchone()[0] <= 1:
            return err(f"That is the only face of {op['name']}", 409, "Conflict")
        c.execute("UPDATE faces SET person_id=? WHERE id=?", (pid, fid))
        if old:
            c.execute("INSERT OR IGNORE INTO face_not VALUES(?,?)", (fid, old))
            refresh_person(c, old)
        refresh_person(c, pid)
        c.commit()
    return jsonify(faceId=fid, personId=pid, name=p["name"])


@app.post("/ui/faces/<int:fid>/not-this-person")
def ui_face_remove(fid):
    """The face was put with the wrong person: give it a group of its own (it can then be merged or named properly)."""
    f = db().execute("SELECT * FROM faces WHERE id=?", (fid,)).fetchone() or abort(404)
    old = f["person_id"]
    c = db()
    op = c.execute("SELECT name FROM persons WHERE id=?", (old,)).fetchone() if old else None
    if op and op["name"] and c.execute("SELECT COUNT(*) FROM faces WHERE person_id=?", (old,)).fetchone()[0] <= 1:
        return err(f"That is the only face of {op['name']}: it stays, or the name would be lost", 409, "Conflict")
    if c.execute("SELECT 1 FROM face_manual WHERE face_id=?", (fid,)).fetchone():  # placed by hand: just take the tag away
        c.execute("DELETE FROM face_manual WHERE face_id=?", (fid,))
        c.execute("DELETE FROM faces WHERE id=?", (fid,))
        if old:
            refresh_person(c, old)
        c.commit()
        return jsonify(personId=None)
    pid = str(uuid.uuid4())
    c.execute("INSERT INTO persons(id, centroid, n, cover, cover_q) VALUES(?,?,?,?,?)", (pid, f["emb"], 1, fid, f["score"] * f["w"] * f["h"]))
    c.execute("UPDATE faces SET person_id=? WHERE id=?", (pid, fid))
    if old:
        c.execute("INSERT OR IGNORE INTO face_not VALUES(?,?)", (fid, old))  # re-clustering must never put it back
        refresh_person(c, old)
    c.commit()
    return jsonify(personId=pid)


def _face_crop(fid):
    """Square-ish 160 px crop of one face (the photo is only read), cached as webp. A face of a hidden photo needs the PIN."""
    f = db().execute("SELECT f.*, a.path FROM faces f JOIN assets a ON a.id=f.asset_id WHERE f.id=?", (fid,)).fetchone() or abort(404)
    if is_hidden(f["path"]) or _in_upload(f["path"]):  # PH72: the upload album's faces need the PIN too
        if not unlocked():
            abort(403)
        g.private = True
    out = data_dir() / "thumbs" / f"face-{fid}.webp"
    if not out.exists():
        try:
            with _render_slot, open_small(f["path"], 1280) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                W, H = im.size
                cx, cy, s = (f["x"] + f["w"] / 2) * W, (f["y"] + f["h"] / 2) * H, max(f["w"] * W, f["h"] * H) * 0.75
                crop = im.crop((int(cx - s), int(cy - s), int(cx + s), int(cy + s))).resize((160, 160), Image.LANCZOS)
        except BAD_IMAGE:
            abort(404)
        tmp = out.with_name(f"{out.name}.{secrets.token_hex(4)}.tmp")
        crop.save(tmp, "WEBP", quality=80)
        tmp.replace(out)
    return send_file(out, mimetype="image/webp", max_age=86400)


@app.get("/ui/faces/<int:fid>/crop")
def ui_face_crop(fid):
    return _face_crop(fid)


@app.get("/api/people/<pid>/thumbnail")
@app.get("/ui/people/<pid>/thumbnail")
def person_thumbnail(pid):
    cover = _person(pid)["cover"] or abort(404)
    if not unlocked() and db().execute("SELECT 1 FROM faces f JOIN assets a ON a.id=f.asset_id WHERE f.id=? AND (a.path IN (SELECT path FROM hidden) OR in_upload(a.path))", (cover,)).fetchone():
        cover = (db().execute("SELECT f.id FROM faces f JOIN assets a ON a.id=f.asset_id WHERE f.person_id=? AND a.path NOT IN (SELECT path FROM hidden) AND NOT in_upload(a.path) "
                              "ORDER BY f.w*f.h DESC LIMIT 1", (pid,)).fetchone() or abort(404))["id"]
    return _face_crop(cover)


@app.get("/api/people")
def api_people():
    hidden = request.args.get("withHidden") == "true"
    rows = people_rows() + (people_rows(hidden=True) if hidden else [])
    nh = len(people_rows(hidden=True))
    return jsonify(hasNextPage=False, hidden=nh, total=len(rows), people=[_person_dto(r) for r in rows])


@app.get("/api/people/<pid>")
def api_person(pid):
    return jsonify(_person_dto(_person(pid)))


@app.put("/api/people/<pid>")
def api_person_update(pid):
    ui_person_update(pid)
    return jsonify(_person_dto(_person(pid)))


@app.get("/api/people/<pid>/statistics")
def api_person_stats(pid):
    _person(pid)
    n = db().execute("SELECT COUNT(DISTINCT f.asset_id) FROM faces f JOIN assets a ON a.id=f.asset_id WHERE f.person_id=? AND a.path NOT IN (SELECT path FROM hidden) AND NOT in_upload(a.path)", (pid,)).fetchone()[0]
    return jsonify(assets=n)


# ---- explore: places and "on this day" memories ----
MEMORY_DAYS = 3  # a memory is the photos taken within this many days either side of today, N years ago


def _window(args):
    """PH85: the memory window in days either side (?days=0..15), default MEMORY_DAYS."""
    try:
        return min(15, max(0, int(args.get("days", MEMORY_DAYS))))
    except (TypeError, ValueError):
        return MEMORY_DAYS


def _this_day(years_ago, today=None, window=MEMORY_DAYS):
    """(year string, ['YYYY-MM-DD', ...]): the days within `window` of today's date years_ago years back. Full dates,
    so the window around New Year crosses into the right neighbouring year (Feb 29 falls back to Feb 28)."""
    from datetime import date, timedelta
    t = today or date.today()
    try:
        base = t.replace(year=t.year - years_ago)
    except ValueError:
        base = t.replace(year=t.year - years_ago, day=28)
    return str(base.year).zfill(4), [(base + timedelta(days=d)).isoformat() for d in range(-window, window + 1)]


def _this_month(years_ago, today=None):
    from datetime import date
    t = today or date.today()
    return str(t.year - years_ago).zfill(4), f"{t.month:02d}"


def _day_sql(days):
    return "substr(taken,1,10) IN (%s)" % ",".join("?" * len(days))


def geo_pass(limit=100):
    """Read GPS of photos/videos not looked at yet and name the nearest city. Returns how many were processed.
    The slow part (reading files, ffprobe) runs with NO transaction open; the database is written briefly at the end,
    so the worker never makes a user action (delete, favorite...) wait for the write lock."""
    c = connect()
    try:
        _maybe_clean(c)
        todo = c.execute("SELECT a.id, a.path, a.kind, a.mtime FROM assets a LEFT JOIN geo g ON g.asset_id=a.id "
                         "WHERE g.asset_id IS NULL OR g.mtime != a.mtime LIMIT ?", (limit,)).fetchall()
        done = []
        for a in todo:
            pt = places.gps_of(a["path"], a["kind"])
            city = region = cc = None
            if pt:
                try:
                    city, region, cc = places.nearest(*pt)
                except Exception:
                    app.logger.exception("city lookup failed")  # keep the GPS point; the pass goes on
            done.append((a["id"], a["mtime"], pt[0] if pt else None, pt[1] if pt else None, city, region, cc))
        if done:  # idle passes write nothing (orphan rows are removed by clean_orphans)
            c.executemany("INSERT OR REPLACE INTO geo VALUES(?,?,?,?,?,?,?)", done)
            c.commit()
        return len(todo)
    finally:
        c.close()


_geo_started = False


@app.before_request
def _geo_worker():
    global _geo_started
    if _geo_started or app.config.get("TESTING"):
        return
    _geo_started = True

    def loop():
        while True:
            try:
                if not geo_pass():
                    time.sleep(60)
            except Exception:
                app.logger.exception("geo pass failed")
                time.sleep(120)
    threading.Thread(target=loop, daemon=True).start()


@app.get("/ui/places")
def ui_places():
    rows = db().execute(
        "SELECT g.city, g.cc, MAX(g.region) region, COUNT(*) n, a.id cover, MAX(a.taken) latest FROM geo g JOIN assets a ON a.id=g.asset_id "
        "WHERE g.city IS NOT NULL AND a.id NOT IN (SELECT video_id FROM live) AND a.path NOT IN (SELECT path FROM hidden) AND NOT in_upload(a.path) GROUP BY g.city, g.cc ORDER BY n DESC, g.city").fetchall()
    return jsonify([dict(city=r["city"], cc=r["cc"], region=r["region"], count=r["n"], cover=r["cover"],
                         key=f"{r['city']}|{r['cc']}") for r in rows])


MEMORY_MAX_YEARS = 60


def _memory_years(c=None, days=MEMORY_DAYS):
    """[(years ago, year string, count)] for every earlier year with photos around today: one grouped query."""
    c = c or db()
    window = {}  # 'YYYY-MM-DD' -> years ago
    for ago in range(1, MEMORY_MAX_YEARS + 1):
        for d in _this_day(ago, window=days)[1]:
            window.setdefault(d, ago)
    mmdd = sorted({d[5:] for d in window})
    n: dict = {}
    for r in c.execute("SELECT substr(taken,1,10) d, COUNT(*) k FROM assets WHERE substr(taken,6,5) IN (%s) AND %s GROUP BY d"
                       % (",".join("?" * len(mmdd)), NOT_LIVE_VIDEO), mmdd):
        if r["d"] in window:
            n[window[r["d"]]] = n.get(window[r["d"]], 0) + r["k"]
    return [(ago, _this_day(ago)[0], n[ago]) for ago in sorted(n)]


@app.get("/ui/memories")
def ui_memories():
    w = _window(request.args)
    out = []
    for ago, yr, n in _memory_years(days=w):
        _, days = _this_day(ago, window=w)
        cov = db().execute(f"SELECT id FROM assets WHERE {_day_sql(days)} AND {NOT_LIVE_VIDEO} ORDER BY (kind='IMAGE') DESC, taken DESC LIMIT 1",
                           days).fetchone()
        out.append(dict(yearsAgo=ago, year=int(yr), count=n, cover=cov["id"] if cov else None))
    return jsonify(out)


@app.get("/ui/memories/month")
def ui_memories_month():
    """PH85: "this month in past years": one card per earlier year with photos in the current month."""
    mth = _this_month(0)[1]
    out = []
    for r in db().execute(f"SELECT substr(taken,1,4) y, COUNT(*) n, MAX(id) cover FROM assets WHERE substr(taken,6,2)=? AND "
                          f"substr(taken,1,4) < ? AND {NOT_LIVE_VIDEO} GROUP BY y ORDER BY y DESC", (mth, _this_month(0)[0])):
        cov = db().execute(f"SELECT id FROM assets WHERE substr(taken,1,7)=? AND {NOT_LIVE_VIDEO} ORDER BY (kind='IMAGE') DESC, taken DESC LIMIT 1",
                           (f"{r['y']}-{mth}",)).fetchone()
        out.append(dict(yearsAgo=int(_this_month(0)[0]) - int(r["y"]), year=int(r["y"]), month=int(mth), count=r["n"], cover=cov["id"] if cov else None))
    return jsonify(out)


@app.get("/api/memories")
def api_memories():
    """Immich-shaped "on this day" memories, one per year, so the phone app can show them too."""
    ctx, out = dto_ctx(), []
    for ago, yr, n in _memory_years():
        _, days = _this_day(ago)
        rows = db().execute(f"SELECT * FROM assets WHERE {_day_sql(days)} AND {NOT_LIVE_VIDEO} ORDER BY taken LIMIT 200", days).fetchall()
        now = iso(time.time())
        out.append(dict(id=str(uuid.uuid5(NS, f"memory:{yr}:{_this_day(0)[1][MEMORY_DAYS][5:]}")), ownerId=_uid(), type="on_this_day",
                        data=dict(year=int(yr)), isSaved=False, memoryAt=rows[0]["taken"], createdAt=now, updatedAt=now,
                        assets=[asset_dto(a, ctx=ctx) for a in rows]))
    return jsonify(out)


# ---- /sync/stream: what the Immich phone app uses to build its timeline ----
# Stateless on purpose: when anything changed since the phone last acknowledged, send SyncResetV1 and the whole
# current state; when nothing changed, just SyncCompleteV1. The ack carries a fingerprint of the library.
_sync_acks: dict = {}  # token hash -> {entity type: last ack string}
_sync_last: dict = {}  # token hash -> (core part, derived part, time of the last full send)
SYNC_SHAPE = 5  # bump when the entities we send change shape, so phones that already acked the old stream get the new one
SYNC_DERIVED_SECS = int(os.environ.get("PHOTOS_SYNC_DERIVED_SECS", "3600"))


def _digest(rows):
    h = hashlib.sha1()
    for r in rows:
        h.update(repr(tuple(r)).encode())
    return h.hexdigest()[:16]


def _sync_parts(c):
    """(core, derived). Core = what the phone must never show wrong: which assets exist (ids and mtimes, so moves and
    renames count), hidden, Live pairs, rotations, albums, favorites. Derived = what the face and place workers add
    all the time (faces, people, places) plus today's memories: these are allowed to lag (see _sync_fp)."""
    core = [_digest(c.execute("SELECT id, mtime, size FROM assets ORDER BY id")),
            _digest(c.execute("SELECT path FROM hidden ORDER BY path")),
            _digest(c.execute("SELECT still_id, video_id FROM live ORDER BY still_id")),
            _digest(c.execute("SELECT id, deg FROM rotations ORDER BY id")),
            _digest(c.execute("SELECT id, path, name FROM folders ORDER BY id")), sorted(fav_paths()), SYNC_SHAPE, _upload_prefix()]
    derived = [_digest(c.execute("SELECT id, person_id FROM faces ORDER BY id")),
               _digest(c.execute("SELECT id, name, hidden FROM persons ORDER BY id")),
               _digest(c.execute("SELECT asset_id, city FROM geo ORDER BY asset_id")), _this_day(0)[1][MEMORY_DAYS],
               c.execute("SELECT COUNT(sha1) FROM assets").fetchone()[0]]  # checksums arrive in the background too
    return hashlib.sha1(json.dumps(core, default=str).encode()).hexdigest()[:20], \
        hashlib.sha1(json.dumps(derived, default=str).encode()).hexdigest()[:20]


def _sync_fp(c, th=None):
    """The ack fingerprint. A core change always resends at once; a change in derived data alone (the workers find
    faces and places for hours after a scan) resends at most once per SYNC_DERIVED_SECS, else every app open would
    download the whole library again."""
    core, derived = _sync_parts(c)
    last = _sync_last.get(th) if th else None
    if last and last[0] == core and last[1] != derived and time.time() - last[2] < SYNC_DERIVED_SECS:
        derived = last[1]  # keep reporting the state the phone has
    return hashlib.sha1((core + derived).encode()).hexdigest()[:20]


def _sync_dupes(c, ok):
    """Files with the same content (same SHA-1, e.g. one photo copied into two folders) would be two assets with one
    checksum. The Immich phone app keeps a unique (owner, checksum) index and saves with INSERT OR REPLACE, so the
    second copy silently deletes the first, and the next exif or album row for it then breaks the whole sync. Only the
    sync response is deduped (files and the web UI are untouched): keep the lowest id, map the others onto it."""
    alias, first = {}, {}
    for r in c.execute(f"SELECT id, sha1 FROM assets WHERE {ok} AND sha1 IS NOT NULL ORDER BY id"):
        if r["sha1"] in first:
            alias[r["id"]] = first[r["sha1"]]
        else:
            first[r["sha1"]] = r["id"]
    return alias


def _sync_lines(types, fp):
    """Yield (entity type, data) for everything the requested sync types cover."""
    c = connect()
    try:
        want = set(types)
        uid, now, email = user_id(), iso(time.time()), account_email() or ""
        if want & {"AuthUsersV1", "AuthUsersV2"}:
            yield "AuthUserV2", dict(id=uid, email=email, name=user_name(), isAdmin=True, hasProfileImage=False, profileChangedAt=now,
                                     avatarColor=None, deletedAt=None, oauthId="", pinCode=None if pin_is_default() else "set",
                                     quotaSizeInBytes=None, quotaUsageInBytes=0, storageLabel=None)
        if "UsersV1" in want:
            yield "UserV1", dict(id=uid, email=email, name=user_name(), hasProfileImage=False, profileChangedAt=now, avatarColor=None, deletedAt=None)
        hid = "path IN (SELECT path FROM hidden)"
        clip_of_hidden = f"id IN (SELECT video_id FROM live WHERE still_id IN (SELECT id FROM assets WHERE {hid}))"
        clip_of_up = "id IN (SELECT video_id FROM live WHERE still_id IN (SELECT id FROM assets WHERE in_upload(path)))"
        # the Hidden album and the PIN-locked upload album (PH72) never reach the phone: their files answer 403 there
        ok = f"NOT ({hid}) AND NOT ({clip_of_hidden}) AND NOT in_upload(path) AND NOT ({clip_of_up})"
        favs = fav_paths()
        clips = {r[0] for r in c.execute("SELECT video_id FROM live")}
        lives = {r[0]: r[1] for r in c.execute("SELECT still_id, video_id FROM live")}
        rots = {r[0]: r[1] for r in c.execute("SELECT id, deg FROM rotations")}
        geo = {r["asset_id"]: r for r in c.execute("SELECT * FROM geo")}
        alias = _sync_dupes(c, ok)  # dropped duplicate id -> kept id (the phone allows one asset per checksum)
        keep = lambda i: alias.get(i, i)
        lives = {k: keep(v) for k, v in lives.items() if k not in alias}
        if want & {"AssetsV1", "AssetsV2"}:
            for a in c.execute(f"SELECT * FROM assets WHERE {ok}"):
                if a["id"] in alias:
                    continue
                turned = rots.get(a["id"], 0) % 180
                yield "AssetV2", dict(id=a["id"], checksum=_checksum(a), createdAt=a["taken"], deletedAt=None, duration=_duration_ms(a), fileCreatedAt=a["taken"],
                                      fileModifiedAt=iso(a["mtime"]), height=a["w"] if turned else a["h"], width=a["h"] if turned else a["w"],
                                      isEdited=False, isFavorite=a["path"] in favs, libraryId=None, livePhotoVideoId=lives.get(a["id"]),
                                      localDateTime=a["taken"], originalFileName=a["name"], ownerId=uid, stackId=None, thumbhash=None,
                                      type=a["kind"], visibility="hidden" if a["id"] in clips else "timeline")
        if "AssetExifsV1" in want:
            for a in c.execute(f"SELECT * FROM assets WHERE {ok}"):
                if a["id"] in alias:
                    continue
                g = geo.get(a["id"])
                yield "AssetExifV1", dict(assetId=a["id"], city=g["city"] if g else None, country=g["cc"] if g else None,
                                          dateTimeOriginal=a["taken"], description=None, exifImageHeight=a["h"], exifImageWidth=a["w"],
                                          exposureTime=None, fNumber=None, fileSizeInByte=a["size"], focalLength=None, fps=None, iso=None,
                                          latitude=g["lat"] if g else None, longitude=g["lon"] if g else None, lensModel=None, make=None,
                                          model=None, modifyDate=iso(a["mtime"]), orientation=None, profileDescription=None,
                                          projectionType=None, rating=None, state=g["region"] if g else None, timeZone=None)
        folders = c.execute("SELECT * FROM folders ORDER BY name").fetchall()
        if want & {"AlbumsV1", "AlbumsV2"}:
            for f in folders:
                cond, par = under(f["path"])
                t = c.execute(f"SELECT id FROM assets WHERE {cond} AND {ok} AND {LIVE_CLIP} ORDER BY taken DESC LIMIT 1", par).fetchone()
                yield "AlbumV2", dict(id=f["id"], name=f["name"], description=f["path"], createdAt=f["created"], updatedAt=f["created"],
                                      isActivityEnabled=False, order="desc", thumbnailAssetId=keep(t["id"]) if t else None)
        if "AlbumUsersV1" in want:  # SyncAlbumV2 has no ownerId: the app learns the owner from this row (role "owner")
            for f in folders:
                yield "AlbumUserV1", dict(albumId=f["id"], userId=uid, role="owner")
        if "AlbumToAssetsV1" in want:
            for f in folders:
                cond, par = under(f["path"])
                for aid in dict.fromkeys(keep(r["id"]) for r in c.execute(f"SELECT id FROM assets WHERE {cond} AND {ok}", par)):
                    yield "AlbumToAssetV1", dict(albumId=f["id"], assetId=aid)
        if want & {"MemoriesV1", "MemoriesV2", "MemoryToAssetsV1", "MemoryToAssetsV2"}:
            for ago, yr, n in _memory_years(c):
                _, days = _this_day(ago)
                rows = c.execute(f"SELECT id, taken FROM assets WHERE {_day_sql(days)} AND {NOT_LIVE_VIDEO} ORDER BY taken LIMIT 200",
                                 days).fetchall()
                if not rows:
                    continue
                mid = str(uuid.uuid5(NS, f"memory:{yr}:{_this_day(0)[1][MEMORY_DAYS][5:]}"))
                if want & {"MemoriesV1", "MemoriesV2"}:
                    yield "MemoryV2", dict(id=mid, ownerId=uid, type="on_this_day", data=dict(year=int(yr)), isSaved=False, memoryAt=rows[0]["taken"],
                                           createdAt=now, updatedAt=now, deletedAt=None, hideAt=None, seenAt=None, showAt=None)
                if want & {"MemoryToAssetsV1", "MemoryToAssetsV2"}:
                    for aid in dict.fromkeys(keep(r["id"]) for r in rows):
                        yield "MemoryToAssetV2", dict(memoryId=mid, assetId=aid)
        if want & {"PeopleV1", "AssetFacesV1", "AssetFacesV2", "AssetFacesV3"}:
            people = people_rows()
            if "PeopleV1" in want:
                for p in people:
                    cov = c.execute("SELECT f.asset_id FROM persons p JOIN faces f ON f.id=p.cover WHERE p.id=?", (p["id"],)).fetchone()
                    yield "PersonV1", dict(id=p["id"], name=p["name"], ownerId=uid, birthDate=None, color=None, createdAt=now, updatedAt=now,
                                           faceAssetId=keep(cov["asset_id"]) if cov else None, isFavorite=False, isHidden=False)
            if want & {"AssetFacesV1", "AssetFacesV2", "AssetFacesV3"}:
                ids = {p["id"] for p in people}
                for f in c.execute(f"SELECT f.*, a.w aw, a.h ah FROM faces f JOIN assets a ON a.id=f.asset_id WHERE a.id IN (SELECT id FROM assets WHERE {ok}) AND f.person_id IS NOT NULL"):
                    if f["person_id"] in ids and f["aw"] and f["ah"] and f["asset_id"] not in alias:
                        yield "AssetFaceV3", dict(id=str(uuid.uuid5(NS, f"face:{f['id']}")), assetId=f["asset_id"], personId=f["person_id"],
                                                  imageWidth=f["aw"], imageHeight=f["ah"], boundingBoxX1=int(f["x"] * f["aw"]),
                                                  boundingBoxY1=int(f["y"] * f["ah"]), boundingBoxX2=int((f["x"] + f["w"]) * f["aw"]),
                                                  boundingBoxY2=int((f["y"] + f["h"]) * f["ah"]), deletedAt=None, isVisible=True,
                                                  sourceType="machine-learning")
    finally:
        c.close()


@app.post("/api/sync/stream")
def sync_stream():
    b = request.get_json(silent=True) or {}
    types, th = [t for t in b.get("types", []) if isinstance(t, str)], _tok_hash()
    app.logger.warning("sync/stream types=%s reset=%s", types, bool(b.get("reset")))  # what the phone app asks for; warning so gunicorn's default level shows it
    c0 = connect()
    try:
        fp = _sync_fp(c0, th)
        unchanged = not b.get("reset") and _sync_acks.get(th, {}).get("SyncCompleteV1") == f"SyncCompleteV1|{fp}"
        if not unchanged:  # a full send: remember what the phone now has
            core, derived = _sync_parts(c0)
            _sync_last[th] = (core, derived, time.time())
            fp = hashlib.sha1((core + derived).encode()).hexdigest()[:20]
    finally:
        c0.close()
    done = f"SyncCompleteV1|{fp}"

    def gen():
        if not unchanged:
            yield json.dumps(dict(type="SyncResetV1", data={}, ack=f"SyncResetV1|{fp}")) + "\n"
            for typ, data in _sync_lines(types, fp):
                yield json.dumps(dict(type=typ, data=data, ack=f"{typ}|{fp}"), separators=(",", ":")) + "\n"
        yield json.dumps(dict(type="SyncCompleteV1", data={}, ack=done)) + "\n"

    return Response(stream_with_context(gen()), mimetype="application/jsonlines+json")


@app.post("/api/sync/ack")
def sync_ack():
    acks = (request.get_json(silent=True) or {}).get("acks", [])
    app.logger.warning("sync/ack %s", [a.split("|", 1)[0] for a in acks if isinstance(a, str)])  # which entity types the phone says it stored
    store = _sync_acks.setdefault(_tok_hash(), {})
    for a in acks:
        if isinstance(a, str) and "|" in a:
            store[a.split("|", 1)[0]] = a
    return "", 204


@app.get("/api/sync/ack")
def sync_acks():
    return jsonify([dict(type=k, ack=v) for k, v in _sync_acks.get(_tok_hash(), {}).items()])


@app.delete("/api/sync/ack")
def sync_ack_delete():
    _sync_acks.pop(_tok_hash(), None)
    return "", 204


# ---- small endpoints the phone app asks for at start-up (answers shaped like the spec; nothing is stored) ----
def _example(schema):
    """A minimal valid value for an OpenAPI schema (required fields only): used for settings the app reads but we never change."""
    sch = json.loads((Path(__file__).parent / "docs" / "immich-openapi-v3.3.6.json").read_text()) if not hasattr(_example, "spec") else _example.spec
    _example.spec = sch

    def go(s, depth=0):
        if "$ref" in s:
            return go(sch["components"]["schemas"][s["$ref"].split("/")[-1]], depth)
        for k in ("allOf", "oneOf", "anyOf"):
            if k in s:
                return go(s[k][0], depth)
        if "enum" in s:
            return s["enum"][0]
        t = s.get("type")
        if t == "object" or "properties" in s:
            return {k: go(v, depth + 1) for k, v in s.get("properties", {}).items() if k in s.get("required", []) and depth < 6}
        return {"array": [], "boolean": False, "integer": 0, "number": 0, "string": ""}.get(t)
    return go(schema)


def _ref(name):
    return {"$ref": "#/components/schemas/" + name}


@app.get("/api/users/me/preferences")
@app.put("/api/users/me/preferences")
def user_preferences():
    out = _example(_ref("UserPreferencesResponseDto"))
    out["memories"].update(enabled=True, duration=5)
    return jsonify(out)


@app.get("/api/users/me/onboarding")
@app.put("/api/users/me/onboarding")
def user_onboarding():
    return jsonify(isOnboarded=True)


@app.get("/api/server/storage")
def server_storage():
    u = shutil.disk_usage(data_dir())
    gb = lambda n: f"{n / 2**30:.1f} GiB"
    return jsonify(diskAvailable=gb(u.free), diskAvailableRaw=u.free, diskSize=gb(u.total), diskSizeRaw=u.total,
                   diskUsagePercentage=round(u.used * 100 / u.total, 2), diskUse=gb(u.used), diskUseRaw=u.used)


@app.get("/api/server/statistics")
def server_statistics():
    r = db().execute(f"SELECT SUM(kind='IMAGE') p, SUM(kind='VIDEO') v, COALESCE(SUM(size),0) s FROM assets WHERE {NOT_LIVE_VIDEO}").fetchone()
    return jsonify(photos=r["p"] or 0, videos=r["v"] or 0, usage=r["s"], usagePhotos=0, usageVideos=0,
                   usageByUser=[dict(userId=user_id(), userName=user_name(), photos=r["p"] or 0, videos=r["v"] or 0, usage=r["s"],
                                     usagePhotos=0, usageVideos=0, quotaSizeInBytes=None)])


@app.get("/api/memories/statistics")
def memories_statistics():
    return jsonify(total=len(_memory_years()))


@app.get("/api/partners")
@app.get("/api/notifications")
@app.get("/api/stacks")
@app.get("/api/tags")
@app.get("/api/shared-links")
@app.get("/api/map/markers")
@app.get("/api/search/explore")
@app.get("/api/search/suggestions")
@app.get("/api/search/cities")
@app.get("/api/search/places")
@app.get("/api/search/person")
@app.post("/api/search/random")
def empty_list():
    return jsonify([])


def _session_dto(r):
    return dict(id=r["id"], createdAt=iso(r["first"]), updatedAt=iso(r["used"]), current=r["hash"] == _tok_hash(),
                deviceOS="", deviceType=(r["agent"] or "")[:80], expiresAt=iso(r["used"] + TOKEN_DAYS * 86400),
                isPendingSyncReset=False, appVersion=None)


@app.get("/api/sessions")
def sessions():
    """Every login that is still valid (Immich's /sessions shape)."""
    rows = db().execute("SELECT s.* FROM sessions s JOIN tokens t ON t.hash=s.hash ORDER BY s.used DESC").fetchall()
    return jsonify([_session_dto(r) for r in rows])


@app.delete("/api/sessions/<sid>")
def session_revoke(sid):
    r = db().execute("SELECT hash FROM sessions WHERE id=?", (sid,)).fetchone() or abort(404)
    db().execute("DELETE FROM tokens WHERE hash=?", (r["hash"],))
    db().execute("DELETE FROM sessions WHERE hash=?", (r["hash"],))
    db().commit()
    _unlocked.pop(r["hash"], None)
    return "", 204


@app.delete("/api/sessions")
def sessions_revoke_others():
    """Sign out every other device; this one stays signed in."""
    me_ = _tok_hash()
    for r in db().execute("SELECT hash FROM tokens WHERE hash != ?", (me_,)).fetchall():
        _unlocked.pop(r["hash"], None)
    db().execute("DELETE FROM tokens WHERE hash != ?", (me_,))
    db().execute("DELETE FROM sessions WHERE hash != ?", (me_,))
    db().commit()
    return "", 204


@app.get("/api/users/me/license")
@app.get("/api/server/license")
def no_license():
    return err("Not found", 404, "Not Found")


def _search_page(items, total):
    return dict(albums=dict(count=0, facets=[], items=[], total=0),
                assets=dict(count=len(items), facets=[], items=items, nextPage=None, nextCursor=None, total=total))


# ---- smart search (PH57): CLIP vectors of every photo, found by words ----
_clip_engine, _clip_lock, _clip_started = None, threading.Lock(), False
_clip_cache = None  # ((rows, newest rowid), ids, matrix)


def _clip():
    global _clip_engine
    with _clip_lock:
        if _clip_engine is None:
            _clip_engine = clipmod.Engine()
        return _clip_engine


def clip_pass(limit=48):
    """Embed photos not done yet (or changed since). An unreadable photo is stored with no vector so it is not retried."""
    c = connect()
    try:
        # photos first, then one frame of every video (PH76: videos can be found by words too), the clip of a Live Photo skipped
        todo = c.execute(f"SELECT a.id, a.path, a.mtime, a.kind FROM assets a LEFT JOIN clip k ON k.asset_id=a.id "
                         f"WHERE (k.asset_id IS NULL OR k.mtime!=a.mtime) AND a.{LIVE_CLIP} ORDER BY a.kind='VIDEO' LIMIT ?", (limit,)).fetchall()
        eng = _clip() if todo else None
        for a in todo:
            try:
                with _render_slot, (video_frame(Path(a["path"]), 448) if a["kind"] == "VIDEO" else open_small(a["path"], 448)) as im:
                    blob = eng.image(ImageOps.exif_transpose(im).convert("RGB")).astype(np.float16).tobytes()
            except Exception:
                blob = None
            c.execute("INSERT OR REPLACE INTO clip VALUES(?,?,?)", (a["id"], a["mtime"], blob))
            c.commit()
        return len(todo)
    finally:
        c.close()


@app.before_request
def _clip_worker():
    """One low-priority thread embeds new photos a few at a time (PHOTOS_CLIP=0 turns it off)."""
    global _clip_started
    if _clip_started or app.config.get("TESTING") or cfg("PHOTOS_CLIP", "1") == "0" or not clipmod.available():
        return
    _clip_started = True

    def loop():
        time.sleep(90)
        while True:
            try:
                time.sleep(1 if clip_pass() else 300)
            except Exception:
                app.logger.exception("clip pass failed")
                time.sleep(300)
    threading.Thread(target=loop, daemon=True).start()


def _clip_matrix():
    global _clip_cache
    c = db()
    key = tuple(c.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM clip WHERE emb IS NOT NULL").fetchone())
    if _clip_cache is None or _clip_cache[0] != key:
        rows = c.execute("SELECT asset_id, emb FROM clip WHERE emb IS NOT NULL").fetchall()
        ids = [r[0] for r in rows]
        m = np.stack([np.frombuffer(r[1], np.float16) for r in rows]).astype(np.float32) if rows else None
        _clip_cache = (key, ids, m)
    return _clip_cache[1], _clip_cache[2]


_smart_cache: dict = {}  # PH66: (words, clip matrix key, upload folder, hidden count) -> ids; the text model runs once per query


def smart_ids(q, limit=250):
    """Asset ids that match the words, best first. Hidden photos and the clip half of a Live Photo never match."""
    q = (q or "").strip()[:200]
    if not q or not clipmod.available():
        return []
    ids, m = _clip_matrix()
    key = (q.lower(), limit, _clip_cache[0] if _clip_cache else None, _upload_prefix(),
           tuple(db().execute("SELECT COUNT(*), (SELECT COUNT(*) FROM live), (SELECT COUNT(*) FROM assets) FROM hidden").fetchone()))
    if key in _smart_cache:
        _smart_cache[key] = _smart_cache.pop(key)  # most recently used last
        return _smart_cache[key]
    out = _smart_rank(q, ids, m, limit)
    _smart_cache[key] = out
    while len(_smart_cache) > 32:
        _smart_cache.pop(next(iter(_smart_cache)))
    return out


def _smart_rank(q, ids, m, limit):
    hits = clipmod.rank(m, _clip().words(q), limit=limit * 2)
    if not hits:
        return []
    want = [ids[i] for i, _ in hits]
    ok = set()
    for k in range(0, len(want), 500):
        part = want[k:k + 500]
        ok |= {r[0] for r in db().execute(f"SELECT id FROM assets WHERE id IN ({','.join('?' * len(part))}) AND {NOT_LIVE_VIDEO}", part)}
    return [i for i in want if i in ok][:limit]


@app.get("/ui/search/status")
def ui_search_status():
    d = db()
    return jsonify(enabled=clipmod.available() and cfg("PHOTOS_CLIP", "1") != "0",
                   photos=d.execute(f"SELECT COUNT(*) FROM assets WHERE {LIVE_CLIP}").fetchone()[0],
                   scanned=d.execute("SELECT COUNT(*) FROM clip WHERE asset_id IN (SELECT id FROM assets)").fetchone()[0])


@app.post("/api/search/smart")
def search_smart():
    b = request.get_json(silent=True) or {}
    try:
        size = min(max(int(b.get("size") or 100), 1), 250)
        page = max(int(b.get("page") or 1), 1)
    except (TypeError, ValueError):
        return err("size and page must be numbers", 400, "Bad Request")
    ids = smart_ids(str(b.get("query") or ""))
    part = ids[(page - 1) * size:page * size]
    rows = {r["id"]: r for r in db().execute(f"SELECT * FROM assets WHERE id IN ({','.join('?' * len(part))})", part)} if part else {}
    ctx = dto_ctx()
    out = _search_page([asset_dto(rows[i], ctx=ctx) for i in part if i in rows], len(ids))
    out["assets"]["nextPage"] = str(page + 1) if page * size < len(ids) else None
    return jsonify(out)


@app.post("/api/search/metadata")
def search_metadata():
    """File name, type, favorite, taken range, city and people filters; hidden photos never match."""
    b = request.get_json(silent=True) or {}
    sql, par = f"SELECT * FROM assets WHERE {NOT_LIVE_VIDEO}", []
    if b.get("originalFileName"):
        sql += " AND name LIKE ?"
        par.append("%" + str(b["originalFileName"]).replace("%", "") + "%")
    if b.get("type") in ("IMAGE", "VIDEO"):
        sql += " AND kind=?"
        par.append(b["type"])
    if b.get("takenAfter"):
        sql += " AND taken>=?"
        par.append(str(b["takenAfter"]))
    if b.get("takenBefore"):
        sql += " AND taken<=?"
        par.append(str(b["takenBefore"]))
    if b.get("city"):
        sql += " AND id IN (SELECT asset_id FROM geo WHERE city=?)"
        par.append(str(b["city"]))
    ids = [p for p in b.get("personIds", []) if isinstance(p, str)]
    if ids:
        sql += " AND id IN (SELECT asset_id FROM faces WHERE person_id IN (%s))" % ",".join("?" * len(ids))
        par += ids
    if b.get("isFavorite") is True:
        favs = sorted(fav_paths())
        sql += " AND path IN (%s)" % ",".join("?" * len(favs)) if favs else " AND 0"
        par += favs
    try:
        size = min(max(int(b.get("size") or 100), 1), 1000)
        page = max(int(b.get("page") or 1), 1)
    except (TypeError, ValueError):
        return err("size and page must be numbers", 400, "Bad Request")
    total = db().execute(sql.replace("SELECT *", "SELECT COUNT(*)", 1), par).fetchone()[0]
    rows = db().execute(sql + f" ORDER BY taken {'ASC' if b.get('order') == 'asc' else 'DESC'}, id LIMIT ? OFFSET ?", par + [size, (page - 1) * size]).fetchall()
    ctx = dto_ctx()
    out = _search_page([asset_dto(a, ctx=ctx) for a in rows], total)
    out["assets"]["nextPage"] = str(page + 1) if page * size < total else None
    return jsonify(out)


# ---- sign in on another device by scanning a QR code: one-time, 5 minutes ----
QR_SECS = 300
_qr_codes: dict = {}  # sha256(code) -> expiry


@app.post("/ui/qr")
def ui_qr():
    import segno
    b = request.get_json(silent=True) or {}
    origin = str(b.get("origin", "")).rstrip("/")
    if origin.split("://", 1)[-1] != request.host:  # the QR may only point back at this server
        return err("Bad origin", 400, "Bad Request")
    now = time.time()
    for k in [k for k, t in _qr_codes.items() if t < now]:
        del _qr_codes[k]
    code = secrets.token_urlsafe(24)
    _qr_codes[hashlib.sha256(code.encode()).hexdigest()] = now + QR_SECS
    url = f"{origin}/#/qr/{code}"
    return jsonify(url=url, server=origin, expiresIn=QR_SECS, svg=segno.make(url, error="m").svg_inline(scale=6, border=2, dark="#000", light="#fff"))


@app.post("/api/auth/qr-login")
def qr_login():
    ip, now = client_ip(), time.time()
    if not _limited(_fails, ip, now):
        return err("Too many failed logins", 429, "Too Many Requests")
    h = hashlib.sha256(str((request.get_json(silent=True) or {}).get("code", "")).encode()).hexdigest()
    if _qr_codes.pop(h, 0) < now:  # unknown, used or expired: a code works once
        authlog("qr")
        return err("This QR code is no longer valid", 401, "Unauthorized")
    _release(_fails, ip, now)
    return login_response(new_token())


def start_background():
    """Start the rescan loop, the face and place workers and the default-album seeding. Called by gunicorn when the
    worker starts (gunicorn.conf.py), so they run after a restart even before anyone opens the app; the before_request
    hooks stay as a fallback (each one starts only once)."""
    for f in (_seed_once, _periodic_rescan, _faces_worker, _geo_worker, _clip_worker, _bg_worker):
        try:
            f()
        except Exception:
            app.logger.exception("starting %s failed", f.__name__)
