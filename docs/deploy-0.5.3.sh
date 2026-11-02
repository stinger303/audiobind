#!/bin/bash
# Deploy 0.5.2 without interrupting a book that is mid-encode:
# pause -> wait for the current book to finish -> swap the container -> resume.
set -e
APP=/mnt/user/appdata/audiobind
OUT=/mnt/user/data/media/audiobooks-fixed
LIB=/mnt/user/data/media/audiobooks
IMAGE=ghcr.io/stinger303/audiobind:0.5.3
TPL=/boot/config/plugins/dockerMan/templates-user/my-Audiobind.xml
API=http://127.0.0.1:8686/api/control

run_state() { curl -s -m 5 http://127.0.0.1:8686/api/state | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['running'], d['paused'], d['counts'])" 2>/dev/null || echo "gone gone {}"; }

echo "1) pausing so the current book finishes cleanly"
curl -s -X POST -H 'Content-Type: application/json' -d '{"action":"pause"}' $API >/dev/null || true

echo "2) waiting for the in-flight book"
for i in $(seq 1 40); do
  read -r running paused _rest <<< "$(run_state)"
  if [ "$running" = "False" ]; then echo "   idle after ${i} checks"; break; fi
  sleep 8
done
run_state

echo "3) swapping to $IMAGE"
docker rm -f Audiobind >/dev/null 2>&1 || true
sed -i 's|audiobind:0\.[0-9.]*|audiobind:0.5.3|' "$TPL"
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
sleep 18

echo "4) resuming (dry-run state is remembered from before)"
curl -s -X POST -H 'Content-Type: application/json' -d '{"action":"resume"}' $API >/dev/null || true
sleep 3
echo "=== result ==="
docker ps --filter name=Audiobind --format '{{.Names}} | {{.Status}} | {{.Image}}'
curl -s http://127.0.0.1:8686/api/state | python3 -c "import sys,json;d=json.load(sys.stdin);print('  v%s | dry_run %s | running %s | counts %s' % (d['version'],d['dry_run'],d['running'],d['counts']))"
curl -s http://127.0.0.1:8686/ | grep -o 'Scan now\|Pause after this book\|Resume\|Stop now\|Dry run: [A-Z]*' | sort -u | sed 's/^/  button: /'
