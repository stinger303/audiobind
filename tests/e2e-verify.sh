#!/bin/bash
# Verify the container-produced m4b, then smoke-test the daemon + status page.
P="/output/Michael Lewis/Moneyball/Moneyball.m4b"
echo "=== streams (want: aac + mjpeg with attached_pic=1) ==="
docker exec audiobind-e2e ffprobe -v error \
  -show_entries stream=index,codec_type,codec_name,channels:stream_disposition=attached_pic \
  -of csv=p=0 "$P"
echo
echo "=== tags ==="
docker exec audiobind-e2e ffprobe -v error -show_entries format=duration:format_tags -of json "$P" \
 | python3 -c "import json,sys; t=json.load(sys.stdin)['format']; print('duration h:', round(float(t['duration'])/3600,2)); [print(f'  {k}: {str(v)[:60]}') for k,v in t['tags'].items() if k in ('title','artist','album','composer','grouping','date','genre')]"
echo
echo "=== chapters ==="
docker exec audiobind-e2e ffprobe -v error -show_chapters -of csv=p=0 "$P" | awk -F, '{n++} END {print "count:", n}'
echo
echo "=== decode sanity (start/middle/end) ==="
for s in 30 18000 37000; do
  docker exec audiobind-e2e sh -c "ffmpeg -hide_banner -ss $s -t 30 -i '$P' -af volumedetect -f null - 2>&1 | grep -E 'mean_volume|Error'"
done

echo
echo "=== daemon + status page ==="
docker rm -f audiobind-web >/dev/null 2>&1
mkdir -p /mnt/user/data/media/.audiobind-test/empty && chmod 777 /mnt/user/data/media/.audiobind-test/empty
docker run -d --name audiobind-web -p 18080:8080 \
  -v /mnt/user/data/media/.audiobind-test/empty:/library:ro \
  -v /mnt/user/data/media/.audiobind-test/out:/output \
  -v /mnt/user/data/media/.audiobind-test/webcfg:/config \
  -e SCAN_INTERVAL_MIN=60 audiobind:dev daemon >/dev/null
sleep 25
echo -n "healthz: "; curl -s -o /dev/null -w "%{http_code} " http://127.0.0.1:18080/healthz; curl -s http://127.0.0.1:18080/healthz; echo
echo -n "api:     "; curl -s http://127.0.0.1:18080/api/state | head -c 300; echo
echo -n "page:    "; curl -s http://127.0.0.1:18080/ | grep -o "<title>[^<]*</title>"
docker logs audiobind-web 2>&1 | tail -5
docker rm -f audiobind-web >/dev/null 2>&1
