#!/bin/sh
# Drop privileges to PUID/PGID (Unraid's nobody:users = 99:100) and hand off to audiobind.
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-022}"

# never run the app as root by accident: PUID/PGID 0 needs an explicit ALLOW_ROOT=1
case "${PUID}${PGID}" in
    ""|*[!0-9]*) echo "audiobind: PUID and PGID must be numbers (got '$PUID' / '$PGID')" >&2; exit 1;;
esac
if [ "$(expr "$PUID" + 0)" = "0" ] || [ "$(expr "$PGID" + 0)" = "0" ]; then
    if [ "${ALLOW_ROOT:-}" != "1" ]; then
        echo "audiobind: refusing to run as root (PUID=$PUID PGID=$PGID). Use a non-root PUID/PGID" >&2
        echo "audiobind: (Unraid: 99/100), or set ALLOW_ROOT=1 if you really mean it." >&2
        exit 1
    fi
    echo "audiobind: WARNING - running as root because ALLOW_ROOT=1" >&2
fi

if [ "$(id -u)" = "0" ]; then
    # cosmetic (names in /etc/passwd); fails harmlessly on a --read-only root fs because
    # gosu below switches to the numeric PUID:PGID either way
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
