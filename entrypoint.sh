#!/bin/sh
# Drop privileges to PUID/PGID (Unraid's nobody:users = 99:100) and hand off to audiobind.
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-022}"

if [ "$(id -u)" = "0" ]; then
    groupmod -o -g "$PGID" audiobind >/dev/null 2>&1 || true
    usermod -o -u "$PUID" audiobind >/dev/null 2>&1 || true

    mkdir -p "$STATE_DIR"
    chown "$PUID:$PGID" "$STATE_DIR" 2>/dev/null || true
    # never chown the library: it is a bind mount of the user's media
    umask "$UMASK"

    echo "audiobind: running as ${PUID}:${PGID} (umask ${UMASK})"
    exec gosu "$PUID:$PGID" audiobind "$@"
fi

exec audiobind "$@"
