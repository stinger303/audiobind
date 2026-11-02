#!/bin/bash
# Deploy 0.6.0: pause -> wait for the in-flight book -> swap -> resume.
set -e
APP=/mnt/user/appdata/audiobind
OUT=/mnt/user/data/media/audiobooks-fixed
LIB=/mnt/user/data/media/audiobooks
IMAGE=ghcr.io/stinger303/audiobind:0.6.0
TPL=/boot/config/plugins/dockerMan/templates-user/my-Audiobind.xml
API=http://127.0.0.1:8686/api/control

run_state() { curl -s -m 5 http://127.0.0.1:8686/api/state | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['running'], d['paused'])" 2>/dev/null || echo "gone gone"; }

echo "1) pause"; curl -s -X POST -H 'Content-Type: application/json' -d '{"action":"pause"}' $API >/dev/null || true
echo "2) wait for the in-flight book"
for i in $(seq 1 40); do
  read -r running _ <<< "$(run_state)"
  if [ "$running" = "False" ]; then echo "   idle after ${i} checks"; break; fi
  sleep 8
done

echo "3) swap to $IMAGE (bitrate floor is now 128k)"
docker rm -f Audiobind >/dev/null 2>&1 || true
sed -i 's|audiobind:0\.[0-9.]*|audiobind:0.6.0|' "$TPL"
sed -i 's|Default="64k"|Default="128k"|; s|>64k</Config>|>128k</Config>|' "$TPL"
docker run -d --name Audiobind --net bridge -p 8686:8080 \
  -e PUID=99 -e PGID=100 -e TZ=America/Denver \
  -e LIBRARY_ROOT=/library -e OUTPUT_ROOT=/output -e STATE_DIR=/config \
  -e AUTO_CONVERT=high -e DRY_RUN=true -e SCAN_INTERVAL_MIN=30 \
  -e AUDIO_BITRATE=128k -e AUDIO_CHANNELS=1 -e JOBS=1 -e NICE=10 \
  -v "$LIB:/library:ro" -v "$OUT:/output" -v "$APP:/config" \
  --restart unless-stopped \
  --label net.unraid.docker.managed=dockerman \
  --label "net.unraid.docker.icon=https://raw.githubusercontent.com/stinger303/audiobind/main/icon.svg" \
  --label "net.unraid.docker.webui=http://[IP]:[PORT:8686]" \
  "$IMAGE" >/dev/null
sleep 18

echo "4) resume"
curl -s -X POST -H 'Content-Type: application/json' -d '{"action":"resume"}' $API >/dev/null || true
sleep 3
echo "=== result ==="
docker ps --filter name=Audiobind --format '{{.Names}} | {{.Status}} | {{.Image}}'
curl -s http://127.0.0.1:8686/api/state | python3 -c "import sys,json;d=json.load(sys.stdin);print('  v%s | dry_run %s | running %s | counts %s' % (d['version'],d['dry_run'],d['running'],d['counts']))"
echo "=== a detail row renders? ==="
curl -s http://127.0.0.1:8686/ | grep -c 'class="drow"' | sed 's/^/  detail panels: /'
