#!/usr/bin/env bash
# ======================================================================================
#  OPTIONAL - Object Monitoring Layer 3 (Grounding DINO name suggestions).
#  Linux version of install-grounding-dino.cmd.
#  The model (IDEA-Research/grounding-dino-tiny, Apache-2.0, ~690 MB) is NOT shipped with this
#  repository. To enable Layer 3, give this script a folder that contains the Hugging Face cache
#  folder "models--IDEA-Research--grounding-dino-tiny" (for example the "hf" folder of a previous
#  install). It is copied INTO backend/models/object_monitor/hf, and "transformers" is installed.
#  After that the backend uses the copy only - the source folder is not needed any more.
#
#    backend/tools/install-grounding-dino.sh /path/to/folder/hf
# ======================================================================================
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
# shellcheck source=lib.sh
source backend/tools/lib.sh

[[ $# -eq 1 ]] || die "Usage: backend/tools/install-grounding-dino.sh <folder containing models--IDEA-Research--grounding-dino-tiny>"
SRC="${1%/}"
NAME="models--IDEA-Research--grounding-dino-tiny"
[[ -d "$SRC/$NAME/snapshots" ]] || die "'$SRC' does not contain $NAME/snapshots"
PY="$(backend_python)"
[[ -x "$PY" ]] || command -v "$PY" >/dev/null 2>&1 || die "No backend Python environment yet - run ./start.sh --setup-only first."

DST="backend/models/object_monitor/hf"
say "Copying $NAME into $DST"
mkdir -p "$DST"
cp -a "$SRC/$NAME" "$DST/"
say "Installing transformers"
"$PY" -m pip install --disable-pip-version-check transformers==5.17.0 || die "Installing transformers failed."
echo
echo "Grounding DINO installed. Restart the backend; Object Monitoring shows \"Layer 3: On\"."
