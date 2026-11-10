#!/bin/bash
# Deploy audiobind 0.14.4 on Tower.
# Args captured live from the running 0.14.3 container on 2026-11-09 via docker inspect
# (env, binds, port, restart policy) - do not copy from older deploy scripts.
# NOTE: library is :rw by owner decision (0.14.2) - Delete removes source + output.
set -euo pipefail
IMAGE=ghcr.io/stinger303/audiobind:0.14.4

docker pull "$IMAGE"
docker stop Audiobind >/dev/null 2>&1 || true
docker rm Audiobind >/dev/null 2>&1 || true

docker run -d --name Audiobind \
  -e PGID=100 -e PUID=99 -e TZ=America/Denver \
  -e OUTPUT_ROOT=/output -e HOST_OUTPUT=/mnt/user/data/media/audiobooks-fixed \
  -e HOST_LIBRARY=/mnt/user/data/media/audiobooks -e LIBRARY_ROOT=/library \
  -e STATE_DIR=/config -e DRY_RUN=false -e SCAN_INTERVAL_MIN=30 -e JOBS=1 \
  -e AUTO_CONVERT=high -e AUDIO_BITRATE=128k -e NICE=10 -e AUDIO_CHANNELS=1 \
  -e WEB_PORT=8080 \
  -p 8686:8080/tcp \
  -v /mnt/user/data/media/audiobooks-fixed:/output:rw \
  -v /mnt/user/appdata/audiobind:/config \
  -v /mnt/user/data/media/audiobooks:/library:rw \
  --restart no \
  "$IMAGE" daemon
