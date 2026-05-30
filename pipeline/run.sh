#!/usr/bin/env bash
# Process all CCTV clips in a directory into data/generated_events.jsonl.
#
# Usage:
#   bash pipeline/run.sh [CLIPS_DIR] [STORE_ID] [API_URL]
#
#   CLIPS_DIR  directory of .mp4/.avi/.mov/.mkv clips   (default: data/clips)
#   STORE_ID   store id to tag events with             (default: STORE_BLR_002)
#   API_URL    if set, also POST events to this API     (default: none)
#
# Camera id is inferred from the filename:
#   *entry*   -> CAM_ENTRY_01
#   *bill*    -> CAM_BILLING_01
#   otherwise -> CAM_FLOOR_01
#
# Layout is auto-resolved: data/store_layout.json (official) else
# data/fallback_store_layout.json. No official files are required to run.
set -euo pipefail

CLIPS_DIR="${1:-data/clips}"
STORE_ID="${2:-STORE_BLR_002}"
API_URL="${3:-}"
OUT="data/generated_events.jsonl"

if [ ! -d "$CLIPS_DIR" ]; then
  echo "[run.sh] No clips directory at '$CLIPS_DIR'."
  echo "[run.sh] Place CCTV clips there, or generate synthetic events instead:"
  echo "         python -m pipeline.simulate_events --store $STORE_ID --post http://localhost:8000"
  exit 0
fi

# Resolve layout (prefer official).
LAYOUT="data/fallback_store_layout.json"
if [ -f "data/store_layout.json" ]; then
  LAYOUT="data/store_layout.json"
fi

# Fresh output file (detect.py truncates per clip, so accumulate via append copy).
: > "$OUT"
TMP_OUT="$(mktemp)"

shopt -s nullglob nocaseglob
clips=( "$CLIPS_DIR"/*.mp4 "$CLIPS_DIR"/*.avi "$CLIPS_DIR"/*.mov "$CLIPS_DIR"/*.mkv )
shopt -u nocaseglob

if [ ${#clips[@]} -eq 0 ]; then
  echo "[run.sh] No video clips found in '$CLIPS_DIR'."
  exit 0
fi

for clip in "${clips[@]}"; do
  fname="$(basename "$clip" | tr '[:upper:]' '[:lower:]')"
  case "$fname" in
    *entry*) CAM="CAM_ENTRY_01" ;;
    *bill*)  CAM="CAM_BILLING_01" ;;
    *)       CAM="CAM_FLOOR_01" ;;
  esac
  echo "[run.sh] Processing $clip (store=$STORE_ID camera=$CAM)"
  POST_ARG=()
  [ -n "$API_URL" ] && POST_ARG=(--post "$API_URL")
  python -m pipeline.detect \
    --video "$clip" \
    --store-id "$STORE_ID" \
    --camera-id "$CAM" \
    --layout "$LAYOUT" \
    --output "$TMP_OUT" \
    "${POST_ARG[@]}"
  cat "$TMP_OUT" >> "$OUT"
done

rm -f "$TMP_OUT"
echo "[run.sh] Done. Combined events written to $OUT"
