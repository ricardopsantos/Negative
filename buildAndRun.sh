#!/usr/bin/env bash
# summary: Builds and runs Photos from this repo on the shared dev/test port 9700, in the foreground (Ctrl-C stops it); own dev data, never live data.
#
# ============================================================================
# buildAndRun.sh - Photos
# ============================================================================
# WHAT IT DOES
#   Runs Photos with gunicorn (config from gunicorn.conf.py, bind overridden to loopback) and a generated login.
#
# USAGE
#   ./buildAndRun.sh            build (install what is missing) and run, in the foreground; Ctrl-C stops it
#   ./buildAndRun.sh run        same as above
#   ./buildAndRun.sh stop       stop a copy left running elsewhere, but only if it was started from this folder
#   ./buildAndRun.sh reset      delete this app's dev data + venv (~/.cache/buildAndRun/Photos); nothing else
#   ./buildAndRun.sh --help     print this header
#
# THE PORT
#   Port 9700 is reserved for tests and development on every machine. No deployed app
#   uses it, so this never collides with a live service. It listens on 127.0.0.1 only (not the LAN,
#   not the tailnet). BR_PORT=NNNN overrides it for a one-off; do not make that permanent.
#   Only one buildAndRun.sh can run at a time per machine (they share the port); the script refuses
#   to start and names the holder if 9700 is taken.
#
# WHERE THINGS LIVE
#   Code     : this repo, used as-is (nothing is modified inside it; only the Immich spec is downloaded once into docs/).
#   Dev data : ~/.cache/buildAndRun/Photos/data   (throw-away; its database, thumbnails and auth log; a pictures folder to try)
#   Venv     : ~/.cache/buildAndRun/Photos/venv   (only where Python packages are needed)
#   Live data and deployed copies are never read or written by this script.
#
# NEEDS
#   python3 with venv; pip downloads requirements.txt (several hundred MB: numpy, opencv, onnxruntime) on the first run
#
# NOTES
#   The login password is generated on the first run and printed on every start. Put photos in data/pictures to try the scan. Face and search models are not downloaded.
#
# Part of the "build and run file on a fixed port for all the apps" card (2026-11-09).
# Same file, same behaviour, on every machine. Edit it in the repo and push; never by hand on a machine.
set -euo pipefail

# --- settings ---------------------------------------------------------------
APP="Photos"
PORT="${BR_PORT:-9700}"                                    # the shared dev/test port
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"       # the repo root, wherever it is cloned
WORK="${XDG_CACHE_HOME:-$HOME/.cache}/buildAndRun/$APP"    # everything this script creates goes here
DATA="$WORK/data"
VENV="$WORK/venv"
cd "$HERE"

# pid listening on $PORT (empty when free)
holder() { ss -H -ltnp "sport = :$PORT" 2>/dev/null | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1; }

# --- sub-commands -----------------------------------------------------------
case "${1:-run}" in
  -h|--help) sed -n '4,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
  run) ;;
  stop)
    pid="$(holder)"
    [ -n "$pid" ] || { echo "nothing is listening on :$PORT"; exit 0; }
    cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
    # only stop what this folder started; never somebody else's process
    case "$cwd/" in
      "$HERE"/*|"$WORK"/*) kill "$pid"; echo "stopped pid $pid"; exit 0 ;;
      *) echo "pid $pid on :$PORT was not started from $HERE (cwd: $cwd); not touching it" >&2; exit 1 ;;
    esac ;;
  reset)
    # removes only the cache folder this script created
    [ -n "$APP" ] && [ -d "$WORK" ] && rm -rf -- "$WORK" && echo "removed $WORK" || echo "nothing to remove"
    exit 0 ;;
  *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

# --- preflight --------------------------------------------------------------
if [ -n "$(holder)" ]; then
  echo "port $PORT is already in use (pid $(holder)); run './buildAndRun.sh stop' or free it" >&2
  exit 1
fi
mkdir -p "$DATA"

# --- helpers ----------------------------------------------------------------
# py_venv FILE... | pkg:NAME...  : create the venv and install requirement files or loose packages.
# Reinstalls only when the requirement files change (a checksum is kept in the venv).
py_venv() {
  command -v python3 >/dev/null || { echo "python3 is missing" >&2; exit 1; }
  [ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
  local stamp="$VENV/.reqs-stamp" sum args=()
  sum="$(for a in "$@"; do case "$a" in pkg:*) echo "$a" ;; *) cat "$HERE/$a" ;; esac; done | sha256sum | cut -d' ' -f1)"
  if [ "$(cat "$stamp" 2>/dev/null || true)" != "$sum" ]; then
    for a in "$@"; do case "$a" in pkg:*) args+=("${a#pkg:}") ;; *) args+=(-r "$HERE/$a") ;; esac; done
    echo "installing dependencies into $VENV ..."
    "$VENV/bin/pip" install --quiet --disable-pip-version-check "${args[@]}"
    echo "$sum" > "$stamp"
  fi
}
# rand : a fresh 16-character dev password/token (never a real secret)
rand() { head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16; }

# --- build and run ----------------------------------------------------------
echo "[$APP] http://127.0.0.1:$PORT  (dev data: $DATA)"
# (everything above is the same in every repo; the lines below are specific to Photos)
# dependencies: venv in the cache folder, installed once and again only when they change
py_venv requirements.txt
# the Immich OpenAPI spec is downloaded, not shipped (once; checked by sha256)
"$VENV/bin/python" "$HERE/tools/fetch_immich_spec.py" >/dev/null
mkdir -p "$DATA/pictures"
PW="$( [ -f "$DATA/password" ] && cat "$DATA/password" || { rand | tee "$DATA/password"; chmod 600 "$DATA/password"; } )"
echo "[$APP] login password: $PW   (put photos to try in $DATA/pictures)"
# configuration: every path points at the dev data above, every port at $PORT, every bind at 127.0.0.1
export PHOTOS_DATA_DIR="$DATA/data" PHOTOS_PASSWORD="$PW"
# start in the foreground; exec so Ctrl-C and 'stop' reach the app itself
exec "$VENV/bin/gunicorn" -c "$HERE/gunicorn.conf.py" --bind "127.0.0.1:$PORT" --chdir "$HERE" server:app
