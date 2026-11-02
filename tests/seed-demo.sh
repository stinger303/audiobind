#!/bin/bash
# Seed a demo database and serve the status page so it can be screenshotted.
set -e
TEST=/mnt/user/data/media/.audiobind-test
docker rm -f audiobind-web >/dev/null 2>&1 || true
rm -rf "$TEST/webcfg"; mkdir -p "$TEST/webcfg" "$TEST/empty2"; chmod -R 777 "$TEST"

docker run --rm --entrypoint python3 \
  -v "$TEST/webcfg:/config" \
  audiobind:dev -c '
from audiobind.state import State, UnitState
import time
s = State(__import__("pathlib").Path("/config"))
demo = [
    ("Michael Lewis- Moneyball", "converted", "HIGH",   "B005EHN61O", "Moneyball", "Michael Lewis", 10.5, 195),
    ("David Baldacci/David Baldacci - Stone Cold (Unabridged)", "converted", "HIGH", "B002VA3QV2", "Stone Cold", "David Baldacci", 10.67, 33),
    ("Pierce Brown - Red Rising", "converted", "HIGH", "B00K8X0RQM", "Red Rising", "Pierce Brown", 16.2, 127),
    ("Mikey Reilly - The Martian", "review", "LOW", "", "The Martian", "", 10.9, 42),
    ("George Orwell - 1984", "held", "MEDIUM", "B004V3M2ME", "1984", "George Orwell", 9.74, 168),
    ("Vince Flynn Novels 14 books Complete Collection", "review", "COLLECTION", "", "Vince Flynn Novels 14 books Complete Collection", "", 171.95, 33),
]
for i, (p, st, cf, asin, title, author, hours, files) in enumerate(demo):
    s.save(UnitState(path=p, status=st, confidence=cf, asin=asin, title=title,
                     author=author, hours=hours, files=files, updated_at=time.time() - i * 900))
run = s.start_run()
s.finish_run(run, 6, 3, 1, 0)
s.heartbeat()
print("seeded", len(demo))
'

docker run -d --name audiobind-web -p 18080:8080 \
  -v "$TEST/empty2:/library:ro" -v "$TEST/out:/output" -v "$TEST/webcfg:/config" \
  -e SCAN_INTERVAL_MIN=30 audiobind:dev daemon >/dev/null
sleep 20
curl -s -o /dev/null -w "status page: %{http_code}\n" http://127.0.0.1:18080/
