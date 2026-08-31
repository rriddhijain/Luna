#!/usr/bin/env bash
# scripts/setup.sh — one command to a working, reproducible environment (seat 5).
#
#   ./scripts/setup.sh
#
# Creates .venv, installs the pinned lockfile, installs SAMANVAY editable, and
# verifies MAGSAC++ is present. Idempotent: safe to re-run.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"

if [ ! -d .venv ]; then
  echo ">> creating virtualenv (.venv)"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate

echo ">> upgrading pip"
python -m pip install --upgrade pip >/dev/null

echo ">> installing pinned dependencies (requirements.lock)"
pip install -r requirements.lock

echo ">> installing samanvay (editable, no deps)"
pip install --no-deps -e .

echo ">> verifying MAGSAC++ (opencv-contrib) is available"
python -c "import cv2; assert hasattr(cv2, 'USAC_MAGSAC'), 'need opencv-contrib build'; print('   MAGSAC++ OK, OpenCV', cv2.__version__)"

cat <<'EOF'

Environment ready. Try:

  . .venv/bin/activate
  python -m synth.render_pair
  samanvay register --source data/source.tif --ref data/reference.tif --out runs/demo_01
  python -m bench.harness            # full benchmark
  pytest                             # test suite

EOF
