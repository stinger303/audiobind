#!/bin/bash
# Exercise the new page controls against a live container.
set -e
T=/mnt/user/data/media/.audiobind-ctl
rm -rf "$T"; mkdir -p "$T/lib" "$T/out" "$T/cfg"; chmod -R 777 "$T"
docker rm -f ab-ctl >/dev/null 2>&1 || true

docker run -d --name ab-ctl -p 18081:8080 \
  -v "$T/lib:/library:ro" -v "$T/out:/output" -v "$T/cfg:/config" \
  -e SCAN_INTERVAL_MIN=60 audiobind:0.2.0 daemon >/dev/null
sleep 12

state() { curl -s http://127.0.0.1:18081/api/state | python3 -c "import sys,json;d=json.load(sys.stdin);print('  paused=%s scan_pending=%s counts=%s' % (d['paused'], d['scan_pending'], d['counts']))"; }
post() { curl -s -X POST -H 'Content-Type: application/json' -d "{\"action\":\"$1\"}" http://127.0.0.1:18081/api/control; echo; }

echo "1) initial";            state
echo "2) page has buttons:";  curl -s http://127.0.0.1:18081/ | grep -o 'Pause after this book\|Scan now\|class="pill [a-z]*"' | sort -u | sed 's/^/   /'
echo "3) POST pause";         post pause; state
echo "4) page after pause:";  curl -s http://127.0.0.1:18081/ | grep -o 'pill paused\|>Resume<' | sort -u | sed 's/^/   /'
echo "5) POST resume";        post resume; state
echo "6) POST scan";          post scan; state
echo "7) bad action";         curl -s -o /dev/null -w "   HTTP %{http_code}\n" -X POST -H 'Content-Type: application/json' -d '{"action":"nope"}' http://127.0.0.1:18081/api/control
echo "8) state survives restart:"
post pause >/dev/null; docker restart ab-ctl >/dev/null; sleep 10; state
echo "9) logs:"; docker logs ab-ctl 2>&1 | tail -5
docker rm -f ab-ctl >/dev/null 2>&1 || true
rm -rf "$T"
