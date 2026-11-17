#!/usr/bin/env bash
# One-shot end-to-end demo:
#   1. starts the scheduler + panel
#   2. starts local worker processes, one of which uploads a half image
#   3. submits the sample moving scene as a frame range
#   4. waits for completion, then writes a time-ordered preview sheet
set -euo pipefail

cd "$(dirname "$0")"
PORT="${PORT:-8765}"
DATA_DIR="${DATA_DIR:-./farm_data_demo}"
BASE="http://127.0.0.1:${PORT}"

rm -rf "$DATA_DIR"
mkdir -p "$DATA_DIR"

python3 -m renderfarm.web --port "$PORT" --data-dir "$DATA_DIR" &
SCHED_PID=$!
trap 'kill ${SCHED_PID} ${WORKER_PIDS:-} 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  curl -sf "$BASE/health" >/dev/null && break
  sleep 0.25
done

# node A injects a half-image upload on its very first task; the scheduler
# must reject it and another attempt must finish the frame.
RENDERFARM_FAULT="halfupload:-1" \
  python3 -m renderfarm.worker --server "$BASE" --name node-A-half --lease 8 &
WORKER_PIDS="$!"
python3 -m renderfarm.worker --server "$BASE" --name node-B --lease 8 &
WORKER_PIDS="$WORKER_PIDS $!"
python3 -m renderfarm.worker --server "$BASE" --name node-C --lease 8 &
WORKER_PIDS="$WORKER_PIDS $!"

SUB_JSON=$(curl -sf -X POST "$BASE/api/submissions" -H 'Content-Type: application/json' \
  -d "$(python3 -c '
import json
doc = json.load(open("examples/scene_bounce.json"))
print(json.dumps({"scene_doc": doc, "frame_start": 0,
                  "frame_end": doc["total_frames"] - 1,
                  "settings": {"width": 320, "height": 180, "ssaa": 2, "format": "png"}}))
')")
SID=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['submission_id'])" "$SUB_JSON")
echo "[demo] submission $SID -> open the panel at $BASE/"

for _ in $(seq 1 90); do
  SUMMARY=$(curl -sf "$BASE/api/submissions/$SID" | python3 -c "import sys,json; print(json.load(sys.stdin)['summary'])")
  echo "  $SUMMARY"
  echo "$SUMMARY" | grep -q "'succeeded': 12" && break
  sleep 1
done

python3 -m renderfarm.preview --server "$BASE" --submission "$SID" \
  --out ./preview_demo.png

echo
echo "[demo] panel:  $BASE/"
echo "[demo] preview: $(pwd)/preview_demo.png"
echo "[demo] press Ctrl-C to stop the scheduler"
wait "$SCHED_PID"
