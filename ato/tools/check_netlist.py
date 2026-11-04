#!/usr/bin/env python3
"""Prove the atopile build is the same board as the original KiCad V3.2 design.

Compares the original PCB against the atopile-built layout and checks:
  1. the same set of references, with the same footprint geometry
  2. connectivity: the pad partition into nets is identical (net names may differ)
  3. layout: every footprint keeps its position/rotation/side, and every track,
     arc, via and zone survives with a net that maps to the original net

Exit status is non-zero on any difference, so this can gate CI.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sexp  # noqa: E402
from sexp import child, children, prop, value  # noqa: E402

ATO = Path(__file__).resolve().parents[1]
REPO = ATO.parent
ORIG = REPO / "Design" / "M3DU_BoardV3_2" / "M3DU_BoardV3_2.kicad_pcb"
NEW = ATO / "layouts" / "default" / "default.kicad_pcb"


def net_of(node) -> str | None:
    n = child(node, "net")
    if n is None:
        return None
    return str(n[-1])


def load(path: Path):
    pcb = sexp.loads(path.read_text())
    fps = {}
    pads: dict[tuple[str, str], str | None] = {}
    for fp in children(pcb, "footprint"):
        r = prop(fp, "Reference")
        at = child(fp, "at")
        fps[r] = {
            "id": str(fp[1]),
            "layer": value(fp, "layer"),
            "at": (round(float(at[1]), 4), round(float(at[2]), 4), round(float(at[3]) % 360, 3) if len(at) > 3 else 0.0),
            "pads": sorted(
                (str(p[1]), str(p[2]), str(p[3]),
                 round(float(child(p, "at")[1]), 3), round(float(child(p, "at")[2]), 3))
                for p in children(fp, "pad")
            ),
        }
        for p in children(fp, "pad"):
            if str(p[1]):
                pads[(r, str(p[1]))] = net_of(p)
    copper = []
    for kind in ("segment", "arc", "via"):
        for s in children(pcb, kind):
            geo = tuple(
                tuple(round(float(v), 4) for v in c[1:3])
                for c in s if isinstance(c, list) and c and c[0] in ("start", "mid", "end", "at")
            )
            copper.append((kind, geo, str(value(s, "layer") or "")), )
            copper[-1] = copper[-1] + (net_of(s),)
    zones = [(net_of(z) if child(z, "net") is not None else value(z, "net_name"),
              str(value(z, "layer") or value(z, "layers") or "")) for z in children(pcb, "zone")]
    return fps, pads, copper, zones


def partition(pads: dict) -> set[frozenset]:
    groups = defaultdict(set)
    for k, n in pads.items():
        if n and not n.startswith("unconnected-"):
            groups[n].add(k)
    return {frozenset(g) for g in groups.values() if len(g) > 1}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--orig", type=Path, default=ORIG)
    ap.add_argument("--new", type=Path, default=NEW)
    ap.add_argument("--no-layout", action="store_true", help="only check connectivity")
    args = ap.parse_args()

    o_fps, o_pads, o_cu, o_zones = load(args.orig)
    n_fps, n_pads, n_cu, n_zones = load(args.new)
    errors: list[str] = []

    # 1. references + footprint geometry
    missing = sorted(set(o_fps) - set(n_fps))
    extra = sorted(set(n_fps) - set(o_fps))
    if missing:
        errors.append(f"missing references: {missing}")
    if extra:
        errors.append(f"extra references: {extra}")
    for r in sorted(set(o_fps) & set(n_fps)):
        if o_fps[r]["pads"] != n_fps[r]["pads"]:
            errors.append(f"{r}: pad geometry differs ({o_fps[r]['id']} -> {n_fps[r]['id']})")

    # 2. connectivity
    common = {k for k in o_pads if k in n_pads}
    po = partition({k: o_pads[k] for k in common})
    pn = partition({k: n_pads[k] for k in common})
    only_o, only_n = po - pn, pn - po
    for g in sorted(only_o, key=lambda g: sorted(g))[:20]:
        errors.append(f"net in original not in atopile build: {o_pads[next(iter(g))]} {sorted(g)[:8]}")
    for g in sorted(only_n, key=lambda g: sorted(g))[:20]:
        errors.append(f"net in atopile build not in original: {n_pads[next(iter(g))]} {sorted(g)[:8]}")

    # net name mapping orig -> new (via pads)
    mapping: dict[str, Counter] = defaultdict(Counter)
    for k in common:
        if o_pads[k]:
            mapping[o_pads[k]][n_pads[k]] += 1
    name_map = {o: c.most_common(1)[0][0] for o, c in mapping.items()}
    renamed = {o: n for o, n in name_map.items() if o != n and not o.startswith("unconnected-")}

    # 3. layout
    moved = []
    if not args.no_layout:
        for r in sorted(set(o_fps) & set(n_fps)):
            if (o_fps[r]["at"], o_fps[r]["layer"]) != (n_fps[r]["at"], n_fps[r]["layer"]):
                moved.append(f"{r}: {o_fps[r]['layer']} {o_fps[r]['at']} -> {n_fps[r]['layer']} {n_fps[r]['at']}")
        errors += [f"footprint moved: {m}" for m in moved[:20]]
        oc = Counter((k, g, l, name_map.get(n, n)) for k, g, l, n in o_cu)
        nc = Counter(n_cu)
        lost, added = oc - nc, nc - oc
        if lost:
            errors.append(f"{sum(lost.values())} copper items lost or re-netted, e.g. {list(lost)[:3]}")
        if added:
            errors.append(f"{sum(added.values())} copper items added or re-netted, e.g. {list(added)[:3]}")
        oz = Counter((name_map.get(n, n), l) for n, l in o_zones)
        nz = Counter(n_zones)
        if oz != nz:
            errors.append(f"zones differ: lost {dict(oz - nz)} added {dict(nz - oz)}")

    print(f"references : {len(o_fps)} original / {len(n_fps)} atopile")
    print(f"nets       : {len(po)} multi-pad nets original / {len(pn)} atopile")
    print(f"copper     : {len(o_cu)} original / {len(n_cu)} atopile (tracks+arcs+vias)")
    print(f"zones      : {len(o_zones)} original / {len(n_zones)} atopile")
    print(f"net names  : {len(renamed)} renamed by atopile")
    for o, n in sorted(renamed.items())[:15]:
        print(f"    {o!r} -> {n!r}")
    if errors:
        print(f"\nFAIL: {len(errors)} difference(s)")
        for e in errors:
            print("  -", e)
        return 1
    print("\nPASS: atopile build is netlist- and layout-identical to the KiCad V3.2 board")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
