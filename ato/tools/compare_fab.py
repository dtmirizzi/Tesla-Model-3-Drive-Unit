#!/usr/bin/env python3
"""Compare fabrication outputs (Gerbers, drill, positions) of two boards.

Both directories must be plotted by the same kicad-cli with the same options.
Files are paired by layer suffix (`<board>-F_Cu.gtl` <-> `<other>-F_Cu.gtl`).

Gerbers are compared as geometry, not text: every draw, arc, flash and filled
region (with its aperture definition) must match, as a multiset per polarity
block. Drawing order, aperture numbering and X2 metadata attributes (net names,
creation date, project name) are ignored: KiCad plots items in file order,
which atopile rewrites. Drill files are compared as hits per tool diameter.
Position files are compared on designator, X, Y, rotation and side.

    python3 tools/compare_fab.py out/fab-original out/fab
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter
from pathlib import Path

_COORD = re.compile(r"(?:X(-?\d+))?(?:Y(-?\d+))?(?:I(-?\d+))?(?:J(-?\d+))?D0?([123])\*$")
_METADATA = ("G04", "TF.", "TA.", "TO.", "TD")


def suffix(p: Path) -> str:
    """Board-name-independent key: 'M3DU-F_Cu.gtl' -> 'F_Cu.gtl', 'M3DU.drl' -> '.drl'."""
    if p.name == "positions.csv":
        return p.name
    return p.name.split("-", 1)[1] if "-" in p.name else p.suffix


def _canon_region(pts: list) -> tuple:
    if len(pts) > 1 and pts[0][1:3] == pts[-1][1:3]:
        pts = pts[:-1]
    if not pts:
        return ()
    k = min(range(len(pts)), key=lambda i: pts[i][1:3])
    fwd = tuple(p[1:3] for p in pts[k:] + pts[:k])
    rev = (fwd[0],) + tuple(reversed(fwd[1:]))
    return min(fwd, rev)


def gerber_geometry(p: Path):
    """(sorted header, [(polarity, Counter(primitives)), ...]) of a Gerber file."""
    text = p.read_text(errors="replace")
    header, apertures, blocks = [], {}, []
    polarity, prims = "D", Counter()
    ap, interp, x, y = None, "G01", 0, 0
    region = None
    for raw in re.split(r"\*", text):
        cmd = raw.replace("%", "").strip()
        if not cmd:
            continue
        cmd += "*"
        if cmd.startswith(_METADATA):
            continue
        if cmd.startswith(("FS", "MO", "AM")):
            header.append(cmd)
            continue
        if m := re.match(r"ADD(\d+)(.*)\*$", cmd):
            apertures[m.group(1)] = m.group(2)
            continue
        if cmd.startswith("LP"):
            blocks.append((polarity, prims))
            polarity, prims = cmd[2], Counter()
            continue
        if cmd.startswith("G36"):
            region = []
            continue
        if cmd.startswith("G37"):
            if region:
                prims[("region", _canon_region(region))] += 1
            region = None
            continue
        if m := re.match(r"(G0[123])(.*)$", cmd):
            interp, cmd = m.group(1), m.group(2)
            if cmd in ("", "*"):
                continue
        if m := re.match(r"D(\d{2,})\*$", cmd):
            ap = apertures.get(m.group(1), m.group(1))
            continue
        if cmd.startswith(("G75", "G74", "M02")):
            continue
        if m := _COORD.match(cmd):
            nx = int(m.group(1)) if m.group(1) is not None else x
            ny = int(m.group(2)) if m.group(2) is not None else y
            op = m.group(5)
            if op == "2":
                if region is not None:
                    region.append(("M", nx, ny))
            elif op == "1":
                if region is not None:
                    region.append((interp, nx, ny))
                elif interp == "G01":
                    prims[("line", ap, min((x, y), (nx, ny)), max((x, y), (nx, ny)))] += 1
                else:
                    prims[("arc", ap, interp, (x, y), (nx, ny), m.group(3), m.group(4))] += 1
            elif op == "3":
                prims[("flash", ap, (nx, ny))] += 1
            x, y = nx, ny
            continue
        header.append(cmd)  # anything unrecognised must match literally
    blocks.append((polarity, prims))
    return sorted(header), [(pol, pr) for pol, pr in blocks if pr]


def drill_geometry(p: Path) -> Counter:
    tools, hits, tool = {}, Counter(), None
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        if m := re.match(r"T(\d+)C([\d.]+)", line):
            tools[m.group(1)] = m.group(2)
            continue
        if m := re.match(r"T(\d+)$", line):
            tool = tools.get(m.group(1), m.group(1))
            continue
        hits[(tool, line)] += 1
    return hits


def load_pos(p: Path) -> dict[str, tuple]:
    rows = {}
    with p.open(newline="") as f:
        for row in csv.DictReader(f):
            rows[row["Ref"]] = (row["PosX"], row["PosY"], row["Rot"], row["Side"])
    return rows


def compare(a_path: Path, b_path: Path, key: str) -> str | None:
    """None if equivalent, else a description of the difference."""
    if key == "positions.csv":
        a, b = load_pos(a_path), load_pos(b_path)
        diff = sorted(r for r in set(a) | set(b) if a.get(r) != b.get(r))
        return f"{len(diff)} placement(s) differ, e.g. {[(r, a.get(r), b.get(r)) for r in diff[:3]]}" if diff else None
    if key.endswith(".gbrjob"):
        return None  # job file: metadata only (dates, file list)
    if key.endswith(".drl"):
        a, b = drill_geometry(a_path), drill_geometry(b_path)
        if a != b:
            return f"{sum((a - b).values())} hit(s) only in original, {sum((b - a).values())} only in atopile"
        return None
    (ha, a), (hb, b) = gerber_geometry(a_path), gerber_geometry(b_path)
    if ha != hb:
        return f"format/aperture-macro header differs: {sorted(set(ha) ^ set(hb))[:3]}"
    if [p for p, _ in a] != [p for p, _ in b] or any(x != y for (_, x), (_, y) in zip(a, b)):
        lost = sum(sum((x - y).values()) for (_, x), (_, y) in zip(a, b))
        gained = sum(sum((y - x).values()) for (_, x), (_, y) in zip(a, b))
        ex = next(((list(x - y) or list(y - x))[:1] for (_, x), (_, y) in zip(a, b) if x != y), [])
        return (f"{lost} primitive(s) only in original, {gained} only in atopile; "
                f"polarity blocks {len(a)} vs {len(b)}; e.g. {ex}")
    return None


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
        elif (d := compare(orig[key], new[key], key)) is not None:
            errors.append(f"{key}: {d}")
        else:
            same += 1

    total = len(set(orig) | set(new))
    print(f"compared {total} files: {same} equivalent, {len(errors)} different")
    for e in errors:
        print("  -", e)
    ok = not errors
    print("\nPASS: fabrication outputs are equivalent to the original V3.2 board's" if ok
          else "\nFAIL: fabrication outputs differ from the original V3.2 board's")
    if args.summary:
        md = [f"### {'✅' if ok else '❌'} Fab outputs vs original V3.2 board", "",
              f"{same} of {total} files equivalent (Gerber geometry, drill hits, placements)", ""]
        md += [f"- `{e}`" for e in errors[:40]]
        with args.summary.open("a") as f:
            f.write("\n".join(md) + "\n\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
