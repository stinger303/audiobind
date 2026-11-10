#!/bin/bash
# Roll Audiobind's container to (or re-deploy) a given local image tag, using
# the STORED args snapshot (docs/live-args.json) - never the live container,
# because a broken container would faithfully re-create brokenness.
# Usage: bash docs/rollback.sh <image:tag>    e.g. bash docs/rollback.sh audiobind:0.14.4
# Refuses to run while a book is encoding (but always proceeds if the app is
# unreachable - nothing can be encoding then). Never pushes, never builds.
set -euo pipefail
TAG="${1:?usage: rollback.sh <image:tag>}"
SNAPSHOT="$(dirname "$0")/live-args.json"
SSH="ssh -i /opt/data/ssh/id_ed25519 -o IdentitiesOnly=yes root@192.168.5.2"

echo "== 0. pre-flight: library idle =="
IDLE=$((curl -s --max-time 10 http://192.168.5.2:8686/api/state || true) | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print('ok' if not d.get('running') and not d.get('current_unit') and not d.get('queue',{}).get('count') else 'BUSY')
except Exception:
    print('down')")
case "$IDLE" in
  ok) ;;
  down) echo "  app is not answering - safe to proceed (nothing can be encoding)";;
  *) echo "library is BUSY - aborting"; exit 1;;
esac

echo "== 1. build the docker run command from the stored snapshot =="
RUNARGS=$(python3 - "$SNAPSHOT" <<'PYEOF'
import json, sys, shlex
s = json.load(open(sys.argv[1]))
parts = ["--restart " + s["restart"]]
parts += ["-v " + shlex.quote(b) for b in s["binds"]]
parts += ["--label " + shlex.quote(k + "=" + v) for k, v in s.get("labels", {}).items()]
parts += ["-e " + shlex.quote(e) for e in s["env"]]
for k, v in s["ports"].items():
    parts += ["-p " + shlex.quote(f'{v[0]["HostPort"]}:{k.split("/")[0]}')]
parts.append(shlex.quote(s["image"]) if s.get("image") else "")
print(" ".join(p for p in parts if p))
PYEOF
)
RUNCMD=$(python3 -c "
import json, sys, shlex
print(' '.join(shlex.quote(a) for a in json.load(open(sys.argv[1]))['cmd']))" "$SNAPSHOT")
echo "  image args ready ($(echo "$RUNARGS" | wc -w) words), cmd: $RUNCMD"

echo "== 2. recreate the container on $TAG =="
$SSH "docker rm -f Audiobind >/dev/null && docker run -d --name Audiobind $RUNARGS $TAG $RUNCMD >/dev/null && echo recreated"
sleep 6

echo "== 3. verify =="
$SSH "docker inspect Audiobind --format '{{range .HostConfig.Binds}}{{.}} {{end}}'"
curl -s --max-time 15 http://192.168.5.2:8686/api/state | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('version:', d['version'])
print('counts :', d['counts'])
assert d['counts']['converted'] > 0, 'library looks wrong'
"
echo "rollback complete (running ${TAG})"
