#!/bin/bash
# Move every successfully-fixed source out of audiobooks/, then move the finished
# library from audiobooks-fixed/ into audiobookss/.
#
# Safety:
#  * only books the database says were CONVERTED are touched
#  * the published m4b must exist and be a plausible size before its source is retired
#  * sources go to a dated trash folder, not to oblivion (rm it yourself when happy)
#  * the daemon is paused for the duration (and resumed on any exit, even an error)
#  * scratch files live in a private mktemp -d directory, removed on exit
#
# If the container has AUTH_TOKEN set, export it as AUDIOBIND_TOKEN before running.
# ALLOWED_HOSTS must still include 127.0.0.1 (the default does).
set -euo pipefail
DB=/mnt/user/appdata/audiobind/audiobind.db
SRC=/mnt/user/data/media/audiobooks
FIXED=/mnt/user/data/media/audiobooks-fixed
TARGET=/mnt/user/data/media/audiobookss
STAMP=$(date +%Y%m%d-%H%M%S)
TRASH=/mnt/user/data/media/_fixed_originals_$STAMP
API=http://127.0.0.1:8686/api/control

APPLY=${1:-}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/ab-retire.XXXXXXXX")
PLAN="$WORK/plan.tsv"
GOOD="$WORK/good.tsv"
BAD="$WORK/bad.txt"
AUTH=()
if [ -n "${AUDIOBIND_TOKEN:-}" ]; then AUTH=(-H "Authorization: Bearer $AUDIOBIND_TOKEN"); fi

api() { curl -s ${AUTH[@]+"${AUTH[@]}"} "$@"; }
control() {
  api -X POST -H 'Content-Type: application/json' -d "{\"action\":\"$1\"}" "$API" >/dev/null || true
}
cleanup() {
  rm -rf -- "$WORK"
  control resume
}
trap cleanup EXIT

echo "=== pausing the daemon ==="
control pause
# pause only stops NEW books: wait for the one in flight so nothing is writing into these trees
for i in $(seq 1 60); do
  running=$(api -m 5 http://127.0.0.1:8686/api/state | python3 -c "import sys,json;print(json.load(sys.stdin)['running'])" 2>/dev/null || echo False)
  if [ "$running" = "False" ]; then echo "  idle after ${i} check(s)"; break; fi
  sleep 10
done

echo "=== reading the manifest ==="
python3 - "$DB" > "$PLAN" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
for r in c.execute("SELECT path, output, title FROM units WHERE status='converted' ORDER BY path"):
    print(f"{r['path']}\t{r['output']}\t{r['title'] or ''}")
PY

ok=0; bad=0; missing=0
: > "$GOOD"
: > "$BAD"
while IFS=$'\t' read -r rel out title; do
  [ -z "$rel" ] && continue
  # the database records container paths (/output, /library); map them to the host
  out_host="${out/#\/output/$FIXED}"
  if [ ! -e "$SRC/$rel" ]; then missing=$((missing+1)); echo "  no longer on disk: $rel" >> "$BAD"; continue; fi
  if [ ! -f "$out_host" ]; then bad=$((bad+1)); echo "  OUTPUT MISSING: $rel -> $out_host" >> "$BAD"; continue; fi
  size=$(stat -c %s "$out_host")
  if [ "$size" -lt 5000000 ]; then bad=$((bad+1)); echo "  OUTPUT TOO SMALL (${size}B): $rel" >> "$BAD"; continue; fi
  ok=$((ok+1)); printf '%s\t%s\n' "$rel" "$out_host" >> "$GOOD"
done < "$PLAN"

echo "  converted books with a verified output : $ok"
echo "  skipped (output missing/short)          : $bad"
echo "  already gone from audiobooks/           : $missing"
if [ -s "$BAD" ]; then echo "--- details ---"; cat "$BAD"; fi

if [ "$APPLY" != "--apply" ]; then
  echo
  echo "[dry run] nothing changed. Re-run with --apply to proceed."
  echo "  sample of what would be retired:"
  head -5 "$GOOD" | cut -f1 | sed 's/^/    /'
  exit 0  # the EXIT trap resumes the daemon
fi

echo
echo "=== 1. retiring verified sources to $TRASH ==="
moved=0
while IFS=$'\t' read -r rel out; do
  mkdir -p "$TRASH/$(dirname "$rel")"
  if mv "$SRC/$rel" "$TRASH/$rel" 2>/dev/null; then moved=$((moved+1)); else
    echo "  COULD NOT MOVE: $rel"; fi
done < "$GOOD"
echo "  moved $moved source folder(s) to the recovery folder"

echo
echo "=== 2. moving the fixed library into audiobookss ==="
if [ ! -d "$FIXED" ]; then echo "  $FIXED does not exist"; else
  cd "$FIXED" || exit 1
  find . -type f -print0 | while IFS= read -r -d '' f; do
    dest="$TARGET/${f#./}"
    if [ -e "$dest" ]; then
      echo "  kept existing: ${f#./}"
    else
      mkdir -p "$(dirname "$dest")"
      mv "$f" "$dest" || echo "  COULD NOT MOVE: ${f#./}"
    fi
  done
  find "$FIXED" -mindepth 1 -depth -type d -empty -delete 2>/dev/null || true
  echo "  remaining files in audiobooks-fixed: $(find "$FIXED" -type f 2>/dev/null | wc -l)"
fi

echo
echo "=== result ==="
echo "  audiobooks/      : $(find "$SRC" -mindepth 1 -maxdepth 1 -type d | wc -l) top-level entries left"
echo "  audiobookss/     : $(find "$TARGET" -name '*.m4b' | wc -l) m4b files"
echo "  recovery folder  : $TRASH ($(du -sh "$TRASH" 2>/dev/null | cut -f1))"
echo
echo "=== resuming the daemon (on exit) ==="
