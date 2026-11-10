#!/bin/bash
# Deploy audiobind 0.15.0-dev2 (why-this-match + series shelf) to Tower.
# Args captured live from the running 0.14.4 container on 2026-11-09
# (see /opt/data/claude-usage/live-args-0.14.4.json) - image tag is the ONLY change.
set -euo pipefail
SSH="ssh -i /opt/data/ssh/id_ed25519 -o IdentitiesOnly=yes root@192.168.5.2"

echo "== 1. sync repo to build dir (tar-over-ssh; this container has no rsync) =="
$SSH "rm -rf /mnt/user/appdata/audiobind-build && mkdir -p /mnt/user/appdata/audiobind-build"
tar -C /opt/data/audiobind -cf - . | ssh -i /opt/data/ssh/id_ed25519 -o IdentitiesOnly=yes root@192.168.5.2 "tar -C /mnt/user/appdata/audiobind-build -xf -"

echo "== 2. build image on Tower =="
$SSH "docker build -t audiobind:0.15.0-dev2 /mnt/user/appdata/audiobind-build/"

echo "== 3. pre-flight: library idle =="
IDLE=$(curl -s http://192.168.5.2:8686/api/state | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok' if not d.get('running') and not d.get('current_unit') and not d.get('queue',{}).get('count') else 'BUSY')")
[ "$IDLE" = "ok" ] || { echo "library is BUSY - aborting"; exit 1; }

echo "== 4. recreate container (args verbatim from live capture) =="
$SSH "docker rm -f Audiobind && docker run -d --name Audiobind \
  --restart no \
  -v /mnt/user/data/media/audiobooks-fixed:/output:rw \
  -v /mnt/user/appdata/audiobind:/config \
  -v /mnt/user/data/media/audiobooks:/library:rw \
  -e PUID=99 -e PGID=100 -e OUTPUT_ROOT=/output -e LIBRARY_ROOT=/library \
  -e DRY_RUN=false -e AUTO_CONVERT=high -e AUDIO_CHANNELS=1 -e AUDIO_BITRATE=128k \
  -e NICE=10 -e WEB_PORT=8080 -e TZ=America/Denver \
  -e HOST_OUTPUT=/mnt/user/data/media/audiobooks-fixed -e STATE_DIR=/config \
  -e HOST_LIBRARY=/mnt/user/data/media/audiobooks -e SCAN_INTERVAL_MIN=30 \
  -p 8686:8080 \
  audiobind:0.15.0-dev2 daemon"

echo "== 5. verify =="
sleep 5
$SSH "docker inspect Audiobind --format '{{range .HostConfig.Binds}}{{.}} {{end}}'"
$SSH "docker inspect Audiobind --format '{{json .Config.Cmd}}'"
curl -s http://192.168.5.2:8686/api/state | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('version:', d['version'])
print('counts:', d['counts'])
print('stats:', d['stats'])
assert d['version'] == '0.15.0-dev2', 'WRONG VERSION'
"
echo "deploy complete"
