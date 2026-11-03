#!/usr/bin/env bash
# Pack the two-wheeler dataset frames the backend captured (module setting captureDataset / data-collection mode) into one
# zip for labelling / training, with a short yield summary.
#
#   bash backend/tools/twhelmet_train/pack_dataset.sh                 # every date folder
#   bash backend/tools/twhelmet_train/pack_dataset.sh 2026-11-02      # one date
#   bash backend/tools/twhelmet_train/pack_dataset.sh --since 2026-11-03   # that date and later
#   bash backend/tools/twhelmet_train/pack_dataset.sh --new           # only frames not in an earlier zip (per-frame list kept)
#
# The zip lands in ~/twhelmet_dataset_<today>.zip (or $OUT_DIR). The folder is the one the backend reports
# (backend.config.SNAPSHOT_DIR/twhelmet_dataset), so it works whatever AIV_SNAPSHOT_DIR is set to.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
SNAP="$($PY -c 'from backend import config; print(config.SNAPSHOT_DIR)' 2>/dev/null | tail -1)"
DS="$SNAP/twhelmet_dataset"
[ -d "$DS" ] || { echo "no dataset folder at $DS (nothing captured yet?)"; exit 1; }
OUT_DIR="${OUT_DIR:-$HOME}"
STAMP="$(date +%F_%H%M%S)"
SEEN="$DS/.packed_frames"                 # names already shipped in an earlier zip (for --new)
touch "$SEEN"

mode="all"; arg=""
case "${1:-}" in
  --new) mode="new" ;;
  --since) mode="since"; arg="${2:?date}" ;;
  "") ;;
  *) mode="date"; arg="$1" ;;
esac

LIST="$(mktemp)"
for d in "$DS"/????-??-??; do
  [ -d "$d" ] || continue
  day="$(basename "$d")"
  case "$mode" in
    date)  [ "$day" = "$arg" ] || continue ;;
    since) [[ "$day" < "$arg" ]] && continue ;;
  esac
  find "$d" -maxdepth 1 -name '*.jpg' | sort
done | while read -r jpg; do
  if [ "$mode" = "new" ] && grep -qxF "$(basename "$jpg")" "$SEEN"; then continue; fi
  echo "$jpg"
done > "$LIST"

N="$(wc -l < "$LIST")"
[ "$N" -gt 0 ] || { echo "nothing to pack ($mode)"; rm -f "$LIST"; exit 0; }

# summary: frames, two-wheelers, frames with a linked rider, size
$PY - "$LIST" <<'EOF'
import json, os, sys
frames = [l.strip() for l in open(sys.argv[1]) if l.strip()]
riders = tw = 0; size = 0
for f in frames:
    size += os.path.getsize(f)
    j = f[:-4] + ".json"
    if os.path.exists(j):
        d = json.load(open(j)); tw += len(d.get("twoWheelers", []))
        riders += any(t.get("riders") for t in d.get("twoWheelers", []))
print(f"frames {len(frames)} | two-wheeler boxes {tw} | frames with a linked rider {riders} | {size/1e6:.0f} MB of JPEG")
EOF

ZIP="$OUT_DIR/twhelmet_dataset_${STAMP}.zip"
# each frame with its .txt (YOLO bike pre-label) and .json (riders, time, camera), paths relative to the dataset folder
( cd "$DS" && sed "s#^$DS/##" "$LIST" | while read -r rel; do
    echo "$rel"; [ -f "${rel%.jpg}.txt" ] && echo "${rel%.jpg}.txt"; [ -f "${rel%.jpg}.json" ] && echo "${rel%.jpg}.json"; done \
  | zip -q -@ "$ZIP" )
sed "s#^.*/##" "$LIST" >> "$SEEN"
rm -f "$LIST"
echo "written $ZIP ($(du -h "$ZIP" | cut -f1))"
echo "copy to the PC:  scp box:$ZIP ."
