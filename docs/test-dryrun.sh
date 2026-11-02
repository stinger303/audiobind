#!/bin/bash
# Verify the dry-run toggle: state, persistence across restart, and the button label.
set -e
T=/mnt/user/data/media/.ab-dry
rm -rf "$T"; mkdir -p "$T/lib" "$T/out" "$T/cfg"; chmod -R 777 "$T"
docker rm -f ab-dry >/dev/null 2>&1 || true

docker run -d --name ab-dry -p 18082:8080 \
  -v "$T/lib:/library:ro" -v "$T/out:/output" -v "$T/cfg:/config" \
  -e DRY_RUN=true -e SCAN_INTERVAL_MIN=60 audiobind:0.3.0 daemon >/dev/null
sleep 12

st() { curl -s http://127.0.0.1:18082/api/state | python3 -c "import sys,json;d=json.load(sys.stdin);print('   dry_run=%s source=%s paused=%s' % (d['dry_run'], d['dry_run_source'], d['paused']))"; }
btn() { curl -s http://127.0.0.1:18082/ | grep -o 'Dry run: ON[^<]*\|Dry run: OFF[^<]*' | head -1 | sed 's/^/   button: /'; }

echo "1) starts from the DRY_RUN env (=true)"; st; btn
echo "2) toggle OFF (writes armed)"
curl -s -X POST -H 'Content-Type: application/json' -d '{"action":"dry_run","value":false}' http://127.0.0.1:18082/api/control >/dev/null
st; btn
echo "3) survives a restart"
docker restart ab-dry >/dev/null; sleep 10; st; btn
echo "4) toggle back ON"
curl -s -X POST -H 'Content-Type: application/json' -d '{"action":"dry_run","value":true}' http://127.0.0.1:18082/api/control >/dev/null
st; btn
echo "5) omitted value flips it"
curl -s -X POST -H 'Content-Type: application/json' -d '{"action":"dry_run"}' http://127.0.0.1:18082/api/control >/dev/null; st
docker rm -f ab-dry >/dev/null 2>&1 || true; rm -rf "$T"
