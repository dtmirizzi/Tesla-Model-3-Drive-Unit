#!/usr/bin/env bash
# Re-run the whole KiCad -> atopile port and verify it.
#
#   tools/port.sh            extract, generate, seed layout, build, check
#   tools/port.sh --no-seed  keep the current layouts/ (e.g. after layout edits)
#   tools/port.sh --no-check build only (CI runs the checkers as separate steps)
#   FLAT=1 tools/port.sh     generate the flat, single-module port instead
#
# Needs: atopile 0.15.9 (Python 3.14) on PATH as `ato`, plus pandas + xlrd
# for reading the JLC BOM.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}
SEED=1
CHECK=1
for arg in "$@"; do
  case "$arg" in
    --no-seed) SEED=0 ;;
    --no-check) CHECK=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

$PY -I tools/extract_kicad.py
if [[ "${FLAT:-0}" == 1 ]]; then
  rm -f reference/addresses.json
  $PY -I tools/gen_ato.py               # flat main.ato (one module, every part)
else
  $PY -I tools/gen_modules.py           # modular main.ato + src/
fi
if [[ $SEED == 1 ]]; then
  $PY -I tools/seed_layout.py
fi
ato --non-interactive build --no-standardize-designators
if [[ $CHECK == 1 ]]; then
  $PY -I tools/check_netlist.py
  $PY -I tools/check_bom.py
fi
