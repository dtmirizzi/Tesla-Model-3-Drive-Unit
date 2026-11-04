#!/usr/bin/env python3
"""Check the atopile BOM against the JLC BOM of the KiCad V3.2 design.

Every designator must have the same LCSC part number in both, and no
designator may appear in only one of them. Exit status is non-zero on any
difference, so this can gate CI.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

ATO = Path(__file__).resolve().parents[1]
REPO = ATO.parent
ORIG = REPO / "Design" / "M3DU_BoardV3_2" / "Production" / "M3DUBoard_V3_2_BOM_JLC.xls"
NEW = ATO / "build" / "builds" / "default" / "default.bom.csv"


def load_jlc(path: Path) -> dict[str, str]:
    import pandas as pd

    out = {}
    for _, row in pd.read_excel(path).iterrows():
        part = row["JLCPCB Part#"]
        if not isinstance(part, str):
            continue
        for ref in str(row["Designator"]).split(","):
            if ref.strip():
                out[ref.strip()] = part.strip()
    return out


def load_ato(path: Path) -> dict[str, str]:
    out = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            for ref in row["Designator"].split(","):
                if ref.strip():
                    out[ref.strip()] = row["LCSC Part #"].strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--orig", type=Path, default=ORIG)
    ap.add_argument("--new", type=Path, default=NEW)
    ap.add_argument(
        "--summary",
        type=Path,
        default=Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None,
    )
    args = ap.parse_args()

    if not args.new.exists():
        print(f"FAIL: {args.new} not found (run `ato build` first)")
        return 1
    orig, new = load_jlc(args.orig), load_ato(args.new)
    missing = sorted(set(orig) - set(new))
    extra = sorted(set(new) - set(orig))
    changed = sorted((r, orig[r], new[r]) for r in set(orig) & set(new) if orig[r] != new[r])

    errors = []
    if missing:
        errors.append(f"{len(missing)} designator(s) missing from the atopile BOM: {missing[:30]}")
    if extra:
        errors.append(f"{len(extra)} designator(s) only in the atopile BOM: {extra[:30]}")
    for r, a, b in changed[:30]:
        errors.append(f"{r}: LCSC {a} (JLC BOM) -> {b} (atopile)")
    if len(changed) > 30:
        errors.append(f"... and {len(changed) - 30} more LCSC changes")

    print(f"JLC BOM    : {len(orig)} placed parts, {len(set(orig.values()))} LCSC numbers")
    print(f"atopile BOM: {len(new)} placed parts, {len(set(new.values()))} LCSC numbers")
    ok = not errors
    print("\nPASS: atopile BOM matches the JLC BOM" if ok else f"\nFAIL: {len(errors)} difference(s)")
    for e in errors:
        print("  -", e)

    if args.summary:
        md = [f"### {'✅' if ok else '❌'} BOM vs JLC BOM", "",
              f"{len(new)} parts in the atopile BOM, {len(orig)} in the JLC BOM", ""]
        md += [f"- `{e}`" for e in errors]
        with args.summary.open("a") as f:
            f.write("\n".join(md) + "\n\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
