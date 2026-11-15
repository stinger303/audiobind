#!/bin/bash
# Deploy audiobind 0.20.0 on Tower, preserving the live container's exact shape.
# Rollback trap: if any verification fails, the previous container is restored.
set -uo pipefail

NAME=Audiobind
PREV=${NAME}-prev
NEW=ghcr.io/stinger303/audiobind:0.20.0
ENVF=/root/ab-deploy-0200.env
TPL=/boot/config/plugins/dockerMan/templates-user/my-Audiobind.xml

echo "== 0. preflight =="
docker image inspect "$NEW" --format '  image: {{.RepoTags}} built {{.Created}}' || { echo "FAIL: image missing"; exit 1; }
docker ps --filter name=^/"$NAME"$ --format '  live: {{.Names}} {{.Status}} {{.Image}}'
OLD=$(docker inspect "$NAME" --format '{{.Config.Image}}')
echo "  from: $OLD   ->   to: $NEW"
[ -n "$(docker inspect "$NAME" --format '{{.State.Running}}')" ] || { echo "FAIL: live container not running"; exit 1; }

echo "== 1. env file from the live container (AUDIO_CHANNELS pinned to 2) =="
docker inspect "$NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -v '^AUDIO_CHANNELS=' | grep -v '^$' > "$ENVF"
echo 'AUDIO_CHANNELS=2' >> "$ENVF"
chmod 600 "$ENVF"
echo "  $(wc -l < "$ENVF") vars, AUDIO_CHANNELS=2"

echo "== 2. back up the Unraid template, then point it at 0.20.0 =="
cp "$TPL" "$TPL.pre-0200-$(date +%Y%m%d-%H%M%S)"
python3 - "$TPL" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
s2 = re.sub(r'(<Repository>)[^<]*(</Repository>)', r'\g<1>ghcr.io/stinger303/audiobind:0.20.0\g<2>', s)
open(p, 'w').write(s2)
print('  template repository ->', re.search(r'<Repository>[^<]*</Repository>', s2).group(0))
PY

echo "== 3. swap =="
docker stop "$NAME" >/dev/null
docker rename "$NAME" "$PREV"
if ! docker run -d --name "$NAME" \
  --network container:GluetunVPN \
  --restart unless-stopped \
  --cpus=1.5 \
  --tmpfs /tmp:size=256m \
  --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID \
  --security-opt no-new-privileges:true \
  -v /mnt/user/data/media/audiobooks:/library:rw \
  -v /mnt/user/data/media/audiobooks-fixed:/output:rw \
  -v /mnt/user/appdata/audiobind:/config \
  -v /mnt/cache_plex/torrents/audiobind-ingest:/ingest:ro \
  --env-file "$ENVF" \
  --label "net.unraid.docker.icon=https://raw.githubusercontent.com/stinger303/audiobind/main/images/audiobind-icon.png" \
  --label "net.unraid.docker.webui=http://[IP]:[PORT:8080]/" \
  --label "net.unraid.docker.managed=dockerman" \
  "$NEW" daemon >/tmp/ab-new-cid.txt 2>&1; then
    echo "FAIL: run failed:"; cat /tmp/ab-new-cid.txt
    docker rm -f "$NAME" >/dev/null 2>&1
    docker rename "$PREV" "$NAME"; docker start "$NAME" >/dev/null
    echo "ROLLED BACK to the previous container"; exit 1
fi
sleep 18

echo "== 4. verify =="
fail=0
v=$(docker exec "$NAME" audiobind version 2>&1 | grep -o '0\.20\.0' | head -1)
echo "  version       : ${v:-<none>}"; [ "$v" = "0.20.0" ] || fail=1
st=$(docker inspect "$NAME" --format '{{.State.Status}}')
echo "  state         : $st"; [ "$st" = "running" ] || fail=1
h=$(curl -s --max-time 10 http://127.0.0.1:8686/healthz)
echo "  healthz       : ${h:-<none>}"; [ "$h" = "ok" ] || fail=1
echo "  binds         : $(docker inspect "$NAME" --format '{{range .HostConfig.Binds}}{{.}} {{end}}')"
echo "  network       : $(docker inspect "$NAME" --format '{{.HostConfig.NetworkMode}}')"
echo "  channels      : $(docker exec "$NAME" sh -c 'echo $AUDIO_CHANNELS')"
counts=$(docker exec "$NAME" python3 -c "
import sqlite3
c=sqlite3.connect('/config/audiobind.db')
print(', '.join('%s=%d'%(s,n) for s,n in c.execute('select status,count(*) from units group by status order by status')))
" 2>/dev/null)
echo "  counts        : ${counts:-<none>}"

echo "== 5. egress must still be the VPN =="
docker exec "$NAME" python3 -c "
import json, urllib.request
d = json.load(urllib.request.urlopen('https://ipinfo.io/json', timeout=25))
ip = str(d.get('ip',''))
print('  egress        :', ip, d.get('city'), d.get('country'))
if ip.startswith('67.176.'):
    raise SystemExit('LEAK: home WAN address')
" || fail=1

if [ "$fail" -ne 0 ]; then
  echo "== VERIFY FAILED: rolling back =="
  docker rm -f "$NAME" >/dev/null 2>&1
  docker rename "$PREV" "$NAME"; docker start "$NAME" >/dev/null
  echo "ROLLED BACK. previous container running."
  exit 1
fi

rm -f "$ENVF"
echo "== OK: 0.20.0 live. keeping the previous container as $PREV for rollback =="
docker ps --filter name=udiobind --format '  {{.Names}} | {{.Status}} | {{.Image}}'
