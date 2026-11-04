#!/usr/bin/env python3
"""Compare KiCad DRC reports of the atopile board and the original V3.2 board.

The original board is not DRC-clean, so a plain "zero violations" gate would
always fail. This gates on *new* violations instead: any violation in the
atopile board's report that the original board's report does not have.

Violations are matched by type and the positions of the items involved
(rounded to 10 um), not by description: descriptions contain net names and
footprint IDs, which can differ legitimately (e.g. variant footprint names).

    kicad-cli pcb drc --format json --severity-error -o orig.json  <original.kicad_pcb>
    kicad-cli pcb drc --format json --severity-error -o ato.json   <atopile.kicad_pcb>
    python3 tools/compare_drc.py orig.json ato.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path


def key(v: dict) -> tuple:
    pos = sorted(
        (round(float(i["pos"]["x"]), 2), round(float(i["pos"]["y"]), 2))
        for i in v.get("items", [])
        if "pos" in i
    )
    return (v.get("type"), tuple(pos))


def load(path: Path) -> tuple[Counter, dict, int]:
    d = json.loads(path.read_text())
    viol = d.get("violations", [])
    examples = {}
    for v in viol:
        examples.setdefault(key(v), v)
    return Counter(key(v) for v in viol), examples, len(d.get("unconnected_items", []))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("original", type=Path)
    ap.add_argument("atopile", type=Path)
    ap.add_argument(
        "--summary",
        type=Path,
        default=Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None,
    )
    args = ap.parse_args()

    o, _, o_unc = load(args.original)
    n, n_ex, n_unc = load(args.atopile)
    new, fixed = n - o, o - n
    by_type = Counter(k[0] for k in n.elements())

    print(f"original board: {sum(o.values())} violations, {o_unc} unconnected")
    print(f"atopile board : {sum(n.values())} violations, {n_unc} unconnected")
    print("atopile board violations by type: " + ", ".join(f"{t}={c}" for t, c in by_type.most_common()))
    print(f"new in atopile board: {sum(new.values())}, only in original: {sum(fixed.values())}")
    for k in list(new)[:30]:
        v = n_ex[k]
        items = "; ".join(i.get("description", "") for i in v.get("items", []))
        print(f"  NEW {v.get('type')}: {v.get('description')} [{items}]")
    ok = not new and n_unc <= o_unc
    print("\nPASS: no DRC violations beyond those of the original V3.2 board" if ok
          else "\nFAIL: the atopile board has DRC violations the original board does not")

    if args.summary:
        md = [f"### {'✅' if ok else '❌'} DRC vs original V3.2 board", "",
              "| | violations | unconnected |", "|---|---:|---:|",
              f"| original V3.2 | {sum(o.values())} | {o_unc} |",
              f"| atopile | {sum(n.values())} | {n_unc} |",
              f"| **new in atopile** | **{sum(new.values())}** | |", "",
              "By type (atopile): " + ", ".join(f"`{t}` {c}" for t, c in by_type.most_common()), ""]
        for k in list(new)[:30]:
            md.append(f"- new `{n_ex[k].get('type')}`: {n_ex[k].get('description')}")
        with args.summary.open("a") as f:
            f.write("\n".join(md) + "\n\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
