#!/bin/bash
# Move the installed container to 0.1.4 and verify icon + page + changelog surfaces.
set -e
APP=/mnt/user/appdata/audiobind
OUT=/mnt/user/data/media/audiobooks-fixed
LIB=/mnt/user/data/media/audiobooks
IMAGE=ghcr.io/stinger303/audiobind:0.1.4
TPL_DIR=/boot/config/plugins/dockerMan/templates-user

dock() { docker run -d --name Audiobind --net bridge -p 8686:8080 \
  -e PUID=99 -e PGID=100 -e TZ=America/Denver \
  -e LIBRARY_ROOT=/library -e OUTPUT_ROOT=/output -e STATE_DIR=/config \
  -e AUTO_CONVERT=high -e DRY_RUN=true -e SCAN_INTERVAL_MIN=30 \
  -e AUDIO_BITRATE=64k -e AUDIO_CHANNELS=1 -e JOBS=1 -e NICE=10 \
  -v "$LIB:/library:ro" -v "$OUT:/output" -v "$APP:/config" \
  --restart unless-stopped \
  --label net.unraid.docker.managed=dockerman \
  --label "net.unraid.docker.icon=https://raw.githubusercontent.com/stinger303/audiobind/main/icon.svg" \
  --label "net.unraid.docker.webui=http://[IP]:[PORT:8686]" \
  "$IMAGE" >/dev/null; }

docker rm -f Audiobind >/dev/null 2>&1 || true
sed -i 's|audiobind:0.1.3|audiobind:0.1.4|' "$TPL_DIR/my-Audiobind.xml"
dock
sleep 18
echo "=== container ==="; docker ps --filter name=Audiobind --format '{{.Names}} | {{.Status}} | {{.Image}}'
echo "=== page + api ==="; curl -s -o /dev/null -w "page %{http_code}\n" http://127.0.0.1:8686/
curl -s http://127.0.0.1:8686/api/state; echo
echo "=== icon served from GitHub (what Unraid/Docker tab loads) ==="
curl -s -o /dev/null -w "icon.svg %{http_code} %{size_download} bytes\n" https://raw.githubusercontent.com/stinger303/audiobind/main/icon.svg
echo "=== template in place ==="; grep -o 'audiobind:[0-9.]*' "$TPL_DIR/my-Audiobind.xml" | head -1
