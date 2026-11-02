#!/bin/bash
# Does the progress percentage actually climb during a real encode?
T=/mnt/user/data/media/.ab-prog
rm -rf "$T"; mkdir -p "$T/out" "$T/cfg"; chmod -R 777 "$T"
docker rm -f ab-prog >/dev/null 2>&1 || true
docker run -d --name ab-prog -p 18085:8080 \
  -v /mnt/user/data/media/audiobooks:/library:ro \
  -v "$T/out:/output" -v "$T/cfg:/config" \
  -e DRY_RUN=false -e AUTO_CONVERT=high -e SCAN_INTERVAL_MIN=60 -e NICE=19 \
  audiobind:0.5.0 daemon >/dev/null

show() { curl -s -m 5 http://127.0.0.1:18085/api/state | python3 -c "
import sys,json
d=json.load(sys.stdin); p=d.get('progress') or {}
print('   running=%-5s pct=%-6s speed=%-5s eta=%-6s unit=%s' % (
    d['running'], p.get('percent','-'), p.get('speed','-'), p.get('eta','-'), (d.get('current_unit') or '')[:38]))"; }

sleep 25
echo "watch the percentage climb:"
for i in $(seq 1 8); do show; sleep 6; done

echo "page shows a bar?"
curl -s http://127.0.0.1:18085/ | grep -o 'id="prog"[^>]*\|id="pct"[^>]*>[^<]*' | sed 's/^/   /'
docker rm -f ab-prog >/dev/null 2>&1 || true
rm -rf "$T"
