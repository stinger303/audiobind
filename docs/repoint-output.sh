#!/bin/bash
# Repoint OUTPUT at audiobookss so finished books land in the library directly,
# instead of needing this move every time.
set -e
APP=/mnt/user/appdata/audiobind
LIB=/mnt/user/data/media/audiobooks
OUT=/mnt/user/data/media/audiobookss
IMAGE=ghcr.io/stinger303/audiobind:0.9.3
TPL=/boot/config/plugins/dockerMan/templates-user/my-Audiobind.xml
API=http://127.0.0.1:8686/api/control

docker rm -f Audiobind >/dev/null 2>&1 || true
sed -i 's|<Config Name="Output path".*/mnt/user/data/media/audiobooks-fixed</Config>|<Config Name="Output path" Target="/output" Default="/mnt/user/data/media/audiobookss" Mode="rw" Description="Where finished m4b files are written." Type="Path" Display="always" Required="true" Mask="false">/mnt/user/data/media/audiobookss</Config>|' "$TPL"

docker run -d --name Audiobind --net bridge -p 8686:8080 \
  -e PUID=99 -e PGID=100 -e TZ=America/Denver \
  -e LIBRARY_ROOT=/library -e OUTPUT_ROOT=/output -e STATE_DIR=/config \
  -e AUTO_CONVERT=high -e DRY_RUN=false -e SCAN_INTERVAL_MIN=30 \
  -e AUDIO_BITRATE=128k -e AUDIO_CHANNELS=1 -e JOBS=1 -e NICE=10 \
  -v "$LIB:/library:ro" -v "$OUT:/output:rw" -v "$APP:/config" \
  --restart unless-stopped \
  --label net.unraid.docker.managed=dockerman \
  --label "net.unraid.docker.icon=https://raw.githubusercontent.com/stinger303/audiobind/main/icon.svg" \
  --label "net.unraid.docker.webui=http://[IP]:[PORT:8686]" \
  "$IMAGE" >/dev/null

sleep 20
echo "=== container ==="; docker ps --filter name=Audiobind --format '{{.Names}} | {{.Status}} | {{.Image}}'
echo "=== mounts ==="; docker inspect Audiobind --format '{{range .Mounts}}{{.Destination}} <- {{.Source}}{{println}}{{end}}'
echo "=== state ==="; curl -s http://127.0.0.1:8686/api/state | python3 -c "import sys,json;d=json.load(sys.stdin);print('  v%s | out %s | counts %s' % (d['version'], d['output_root'], d['counts']))"
echo "=== template ==="; grep -o 'Output path[^<]*<[^>]*>[^<]*' "$TPL" | head -1
