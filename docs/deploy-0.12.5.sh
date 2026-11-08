#!/bin/bash
# Deploy audiobind 0.12.5: recreate the container with the same args, new image tag.
set -e
TPL=/boot/config/plugins/dockerMan/templates-user/my-Audiobind.xml
API=http://127.0.0.1:8686/api/control
OLD=ghcr.io/stinger303/audiobind:0.12.2
NEW=ghcr.io/stinger303/audiobind:0.12.5

echo "1) pause scan loop"
curl -s -X POST -H 'Content-Type: application/json' -d '{"action":"pause"}' $API >/dev/null || true
for i in $(seq 1 30); do
  running=$(curl -s -m 5 http://127.0.0.1:8686/api/state | python3 -c "import sys,json;print(json.load(sys.stdin)['running'])" 2>/dev/null || echo False)
  if [ "$running" = "False" ]; then echo "   idle"; break; fi
  sleep 8
done

echo "2) pull $NEW"
docker pull "$NEW"

echo "3) swap container"
docker rm -f Audiobind >/dev/null 2>&1 || true
docker run -d --name Audiobind \
  --net bridge -p 8686:8080 \
  -e PUID=99 -e PGID=100 -e TZ=America/Denver \
  -e LIBRARY_ROOT=/library -e OUTPUT_ROOT=/output -e STATE_DIR=/config \
  -e AUTO_CONVERT=high -e DRY_RUN=false -e SCAN_INTERVAL_MIN=30 \
  -e AUDIO_BITRATE=128k -e AUDIO_CHANNELS=1 -e JOBS=1 -e NICE=10 \
  -e HOST_LIBRARY=/mnt/user/data/media/audiobooks \
  -e HOST_OUTPUT=/mnt/user/data/media/audiobooks-fixed \
  -v /mnt/user/data/media/audiobooks:/library:rw \
  -v /mnt/user/data/media/audiobooks-fixed:/output:rw \
  -v /mnt/user/appdata/audiobind:/config \
  --label net.unraid.docker.managed=dockerman \
  --label "net.unraid.docker.icon=https://raw.githubusercontent.com/stinger303/audiobind/main/images/audiobind-icon.png" \
  --label "net.unraid.docker.webui=http://[IP]:[PORT:8080]/" \
  "$NEW" >/dev/null
sed -i "s|audiobind:0\.[0-9.]*|audiobind:0.12.5|" "$TPL"
sleep 15

echo "4) resume"
curl -s -X POST -H 'Content-Type: application/json' -d '{"action":"resume"}' $API >/dev/null || true
sleep 3

echo "=== verify ==="
docker ps --filter name=Audiobind --format '{{.Names}} | {{.Status}} | {{.Image}}'
curl -s http://127.0.0.1:8686/api/state | python3 -c "import sys,json;d=json.load(sys.stdin);print('v%s | dry_run %s | running %s | counts %s' % (d['version'],d['dry_run'],d['running'],d['counts']))"
curl -s http://127.0.0.1:8686/api/state | python3 -c "import sys,json;print('rev key present:', 'rev' in json.load(sys.stdin))"
curl -s http://127.0.0.1:8686/ | grep -c "Why it's waiting for you" | sed 's/^/  plain-English headers on page: /'
curl -s http://127.0.0.1:8686/ | grep -c 'data-reopen="1"' | sed 's/^/  reopen-flagged panels: /'
