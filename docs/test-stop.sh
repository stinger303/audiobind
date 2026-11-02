#!/bin/bash
# Live test of Stop: does an aborted conversion leave anything in the library?
set -e
T=/mnt/user/data/media/.ab-stop
rm -rf "$T"; mkdir -p "$T/out" "$T/cfg"; chmod -R 777 "$T"
docker rm -f ab-stop >/dev/null 2>&1 || true

docker run -d --name ab-stop -p 18083:8080 \
  -v /mnt/user/data/media/audiobooks:/library:ro \
  -v "$T/out:/output" -v "$T/cfg:/config" \
  -e DRY_RUN=false -e AUTO_CONVERT=high -e SCAN_INTERVAL_MIN=60 -e NICE=19 \
  audiobind:0.4.0 daemon >/dev/null

st() { curl -s http://127.0.0.1:18083/api/state | python3 -c "
import sys,json;d=json.load(sys.stdin)
print('   running=%s unit=%r paused=%s counts=%s' % (d['running'], d['current_unit'], d['paused'], d['counts']))"; }

echo "1) starting a pass"; curl -s -X POST -H 'Content-Type: application/json' -d '{"action":"scan"}' http://127.0.0.1:18083/api/control >/dev/null
for i in $(seq 1 30); do
  running=$(curl -s http://127.0.0.1:18083/api/state | python3 -c "import sys,json;print(json.load(sys.stdin)['running'])")
  if [ "$running" = "True" ]; then break; fi
  sleep 2
done
st
echo "   stop button state: $(curl -s http://127.0.0.1:18083/ | grep -o 'Stop now' | head -1)"
sleep 25
echo "2) mid-conversion, partial file in work dir?"
ls -lh "$T/cfg/work"/*/*.m4b 2>/dev/null | awk '{print "   work:", $5, $9}' || echo "   (none yet)"
echo "   output dir so far: $(find "$T/out" -name '*.m4b' | wc -l) m4b file(s)"

echo "3) STOP"
curl -s -X POST -H 'Content-Type: application/json' -d '{"action":"stop"}' http://127.0.0.1:18083/api/control; echo
sleep 6
st
echo "4) aftermath"
echo "   m4b in output:        $(find "$T/out" -name '*.m4b' | wc -l)"
echo "   leftover work dirs:   $(find "$T/cfg/work" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)"
echo "   ffmpeg still running: $(docker exec ab-stop pgrep -c -x ffmpeg || echo 0)"
docker logs ab-stop 2>&1 | grep -iE "stopped by operator|interrupted|published" | tail -3 | sed 's/^/   log: /'

echo "5) resume retries the same book from scratch"
curl -s -X POST -H 'Content-Type: application/json' -d '{"action":"resume"}' http://127.0.0.1:18083/api/control >/dev/null
for i in $(seq 1 40); do
  n=$(find "$T/out" -name "*.m4b" | wc -l)
  if [ "$n" -ge 1 ]; then break; fi
  sleep 10
done
echo "   m4b now: $(find "$T/out" -name '*.m4b' | wc -l)"
find "$T/out" -name "*.m4b" -exec ls -lh {} \; 2>/dev/null | awk '{print "   ", $5, $9}'
st
docker rm -f ab-stop >/dev/null 2>&1 || true
rm -rf "$T"
