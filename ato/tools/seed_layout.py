#!/usr/bin/env python3
"""Seed the atopile layout with the original, hand-routed V3.2 PCB.

The board is a drop-in replacement for the OEM logic board, so placement,
routing, zones, HV creepage and the outline must be preserved exactly. atopile
matches existing footprints by their `atopile_address` property and updates
them in place, so we copy the original board and tag every footprint with the
address of the matching instance in main.ato. Everything else (tracks, vias,
zones, graphics) is carried over byte-for-byte.

Run once (or after re-generating main.ato), then `ato build`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen_ato import SKIP_REFS, inst_name  # noqa: E402

ATO = Path(__file__).resolve().parents[1]
REPO = ATO.parent
SRC_PCB = REPO / "Design" / "M3DU_BoardV3_2" / "M3DU_BoardV3_2.kicad_pcb"

PROP = """\t\t(property "atopile_address" "{addr}"
\t\t\t(at 0 0 0)
\t\t\t(layer "User.9")
\t\t\t(hide yes)
\t\t\t(uuid "{uuid}")
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t)
\t\t\t)
\t\t)
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=SRC_PCB)
    ap.add_argument("--out", type=Path, default=ATO / "layouts" / "default" / "default.kicad_pcb")
    args = ap.parse_args()

    ref = json.loads((ATO / "reference" / "netlist.json").read_text())
    comps = ref["components"]
    # atopile address of every designator; written by gen_modules.py (modular
    # design). Without it, fall back to the flat main.ato naming.
    addr_file = ATO / "reference" / "addresses.json"
    addresses = json.loads(addr_file.read_text()) if addr_file.exists() else {}

    lines = args.src.read_text().splitlines(keepends=True)
    out: list[str] = []
    fp_line_idx = None
    tagged = 0
    renamed = 0
    dropped = 0
    for line in lines:
        # schematic-only hint without a position; atopile's transformer chokes on it
        if line.startswith("\t\t(property ki_fp_filters "):
            dropped += 1
            continue
        if line.startswith("\t(footprint "):
            fp_line_idx = len(out)
        elif fp_line_idx is not None and (m := re.match(r'\t\t\(property "Reference" "([^"]*)"', line)):
            r = m.group(1)
            if r in comps and r not in SKIP_REFS:
                out.append(PROP.format(addr=addresses.get(r) or inst_name(r), uuid=uuid.uuid5(uuid.NAMESPACE_URL, f"m3du/{r}")))
                tagged += 1
                want = comps[r]["footprint"]
                cur = re.match(r'\t\(footprint "([^"]*)"', out[fp_line_idx]).group(1)
                if cur != want:
                    out[fp_line_idx] = out[fp_line_idx].replace(f'"{cur}"', f'"{want}"', 1)
                    renamed += 1
            fp_line_idx = None
        out.append(line)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(out))
    print(f"tagged {tagged} footprints, renamed {renamed} footprint ids, dropped {dropped} ki_fp_filters -> {args.out}")


if __name__ == "__main__":
    main()
