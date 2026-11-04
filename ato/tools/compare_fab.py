#!/usr/bin/env python3
"""Compare fabrication outputs (Gerbers, drill, positions) of two boards.

Both directories must be plotted by the same kicad-cli with the same options.
Files are paired by layer suffix (`<board>-F_Cu.gtl` <-> `<other>-F_Cu.gtl`).
Lines that only carry metadata (creation date, project name/ID, generator
comments) are ignored; every aperture, coordinate and drill hit must match.
Position files are compared on designator, X, Y, rotation and side.

    python3 tools/compare_fab.py out/fab-original out/fab
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

META = re.compile(
    r"^(G04 .*|%TF\.(CreationDate|ProjectId|GenerationSoftware|SameCoordinates).*|"
    r"; (DRILL file|FORMAT|#@! TF\.(CreationDate|ProjectId|GenerationSoftware)).*|"
    r"; Created on .*|; (Board|Generated|Units).*)$"
)


def suffix(p: Path) -> str:
    """Board-name-independent key: 'M3DU-F_Cu.gtl' -> 'F_Cu.gtl', 'M3DU.drl' -> '.drl'."""
    if p.name == "positions.csv":
        return p.name
    return p.name.split("-", 1)[1] if "-" in p.name else p.suffix


def norm_lines(p: Path) -> list[str]:
    return [l.rstrip() for l in p.read_text(errors="replace").splitlines() if l.strip() and not META.match(l.strip())]


def load_pos(p: Path) -> dict[str, tuple]:
    rows = {}
    with p.open(newline="") as f:
        for row in csv.DictReader(f):
            rows[row["Ref"]] = (row["PosX"], row["PosY"], row["Rot"], row["Side"])
    return rows


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

    orig = {suffix(p): p for p in args.original.iterdir() if p.is_file()}
    new = {suffix(p): p for p in args.atopile.iterdir() if p.is_file()}
    errors, same = [], 0
    for key in sorted(set(orig) | set(new)):
        if key not in new or key not in orig:
            errors.append(f"{key}: only in {'original' if key in orig else 'atopile'} output")
            continue
        if key.endswith(".csv"):
            a, b = load_pos(orig[key]), load_pos(new[key])
            diff = sorted(r for r in set(a) | set(b) if a.get(r) != b.get(r))
            if diff:
                errors.append(f"{key}: {len(diff)} placement(s) differ, e.g. {[(r, a.get(r), b.get(r)) for r in diff[:3]]}")
            else:
                same += 1
            continue
        a, b = norm_lines(orig[key]), norm_lines(new[key])
        if a != b:
            first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
            errors.append(f"{key}: differs ({len(a)} vs {len(b)} lines), first at line {first}: "
                          f"{a[first] if first < len(a) else '<eof>'!r} vs {b[first] if first < len(b) else '<eof>'!r}")
        else:
            same += 1

    print(f"compared {len(set(orig) | set(new))} files: {same} identical, {len(errors)} different")
    for e in errors:
        print("  -", e)
    ok = not errors
    print("\nPASS: fabrication outputs are identical to the original V3.2 board's" if ok
          else "\nFAIL: fabrication outputs differ from the original V3.2 board's")
    if args.summary:
        md = [f"### {'✅' if ok else '❌'} Fab outputs vs original V3.2 board", "",
              f"{same} of {len(set(orig) | set(new))} files identical (Gerbers, drill, positions; metadata lines ignored)", ""]
        md += [f"- `{e}`" for e in errors[:40]]
        with args.summary.open("a") as f:
            f.write("\n".join(md) + "\n\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
