#!/usr/bin/env bash
# Re-run the whole KiCad -> atopile port and verify it.
#
#   tools/port.sh            extract, generate, seed layout, build, check
#   tools/port.sh --no-seed  keep the current layouts/ (e.g. after layout edits)
#
# Needs: atopile 0.15.9 (Python 3.14) on PATH as `ato`, plus pandas + xlrd
# for reading the JLC BOM.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}
SEED=1
[[ "${1:-}" == "--no-seed" ]] && SEED=0

$PY -I tools/extract_kicad.py
$PY -I tools/gen_ato.py
if [[ $SEED == 1 ]]; then
  $PY -I tools/seed_layout.py
fi
ato --non-interactive build --keep-net-names --no-standardize-designators
$PY -I tools/check_netlist.py
