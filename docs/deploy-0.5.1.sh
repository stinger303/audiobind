#!/bin/bash
# Roll the installed container onto 0.4.0 (Stop button, runtime column).
set -e
APP=/mnt/user/appdata/audiobind
OUT=/mnt/user/data/media/audiobooks-fixed
LIB=/mnt/user/data/media/audiobooks
IMAGE=ghcr.io/stinger303/audiobind:0.5.1
TPL=/boot/config/plugins/dockerMan/templates-user/my-Audiobind.xml

docker rm -f Audiobind >/dev/null 2>&1 || true
sed -i 's|audiobind:0\.[0-9.]*|audiobind:0.5.1|' "$TPL"

docker run -d --name Audiobind --net bridge -p 8686:8080 \
  -e PUID=99 -e PGID=100 -e TZ=America/Denver \
  -e LIBRARY_ROOT=/library -e OUTPUT_ROOT=/output -e STATE_DIR=/config \
  -e AUTO_CONVERT=high -e DRY_RUN=true -e SCAN_INTERVAL_MIN=30 \
  -e AUDIO_BITRATE=64k -e AUDIO_CHANNELS=1 -e JOBS=1 -e NICE=10 \
  -v "$LIB:/library:ro" -v "$OUT:/output" -v "$APP:/config" \
  --restart unless-stopped \
  --label net.unraid.docker.managed=dockerman \
  --label "net.unraid.docker.icon=https://raw.githubusercontent.com/stinger303/audiobind/main/icon.svg" \
  --label "net.unraid.docker.webui=http://[IP]:[PORT:8686]" \
  "$IMAGE" >/dev/null

sleep 20
echo "=== container ==="; docker ps --filter name=Audiobind --format '{{.Names}} | {{.Status}} | {{.Image}}'
echo "=== buttons ==="; curl -s http://127.0.0.1:8686/ | grep -o 'Scan now\|Pause after this book\|Resume\|Stop now\|Dry run: [A-Z]*' | sort -u | sed 's/^/  /'
echo "=== state ==="; curl -s http://127.0.0.1:8686/api/state | python3 -c "import sys,json;d=json.load(sys.stdin);print('  v%s | dry_run %s | running %s | counts %s' % (d['version'],d['dry_run'],d['running'],d['counts']))"
