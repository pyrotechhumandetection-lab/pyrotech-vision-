#!/usr/bin/env bash
# Download every pinned package into backend/wheels/linux, so ./aivision.sh setup can
# install on a Linux PC WITHOUT internet. Linux version of download-wheels.cmd.
# Run it on a Linux PC with internet that has the SAME CPU type (x86_64 / aarch64) and the same
# Python version as the offline PC - easiest: run ./aivision.sh setup there first, then this.
# Then copy the whole folder (including backend/wheels/linux) to the offline PC and run ./aivision.sh install.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
# shellcheck source=lib.sh
source backend/tools/lib.sh

PY="$VENV/bin/python"
[[ -x "$PY" ]] || die "Run ./aivision.sh setup first (the wheels must match backend/.venv's Python)."
DIR="backend/wheels/linux"
mkdir -p "$DIR"
PIP=("$PY" -m pip --disable-pip-version-check download --only-binary=:all: -d "$DIR")

say "Downloading wheels for $(uname -m), $("$PY" -V) into $DIR"
# CPU PyTorch first (much smaller); the lock file download below then reuses it
"${PIP[@]}" torch==2.14.0 torchvision==0.29.0 --index-url https://download.pytorch.org/whl/cpu \
  || warn "CPU PyTorch not available - the standard PyTorch build is downloaded instead."
grep -viE '^openvino(-telemetry)?==' "$LOCK" > "$DIR/.core.txt"
"${PIP[@]}" --find-links "$DIR" -r "$DIR/.core.txt" || die "Download failed."
grep -iE '^openvino(-telemetry)?==' "$LOCK" > "$DIR/.opt.txt" || true
"${PIP[@]}" -r "$DIR/.opt.txt" || warn "OpenVINO is not available for this PC - skipped (optional)."
rm -f "$DIR/.core.txt" "$DIR/.opt.txt"
echo
echo "Wheels saved in $DIR ($(du -sh "$DIR" | cut -f1)). Copy the folder to the offline PC and run ./aivision.sh install there."
