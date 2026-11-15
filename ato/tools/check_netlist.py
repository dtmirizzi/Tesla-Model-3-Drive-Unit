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
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sexp  # noqa: E402
from sexp import Sym, child, children, prop, value  # noqa: E402

ATO = Path(__file__).resolve().parents[1]
REPO = ATO.parent
ORIG = REPO / "Design" / "M3DU_BoardV3_2" / "M3DU_BoardV3_2.kicad_pcb"
NEW = ATO / "layouts" / "default" / "default.kicad_pcb"


_FP_DROP = {"uuid", "tstamp", "net", "pinfunction", "pintype", "path", "sheetname", "sheetfile"}


def _canon(node, nd: int = 4) -> str:
    """Canonical text of an S-expression; numbers rounded to 1e-4."""
    if not isinstance(node, list):
        if isinstance(node, Sym):
            try:
                return f"{round(float(node), nd) + 0.0:g}"
            except ValueError:
                return str(node)
        return repr(str(node))
    if node and node[0] == "at" and len(node) == 4 and isinstance(node[3], Sym):
        try:
            if float(node[3]) % 360 == 0:
                node = node[:3]  # (at x y 0) == (at x y)
        except ValueError:
            pass
    return "(" + " ".join(_canon(c, nd) for c in node if not (isinstance(c, list) and c and c[0] in _FP_DROP)) + ")"


def fp_body(fp) -> Counter:
    """Everything a footprint is made of except properties (text fields) and
    net assignments: pads incl. per-pad overrides, graphics, courtyard,
    attributes, zone/thermal/clearance overrides, lock state, 3D models.

    Footprint-level clearance/zone_connect/mask margins are compared as the
    effective per-pad values (KiCad semantics), since that is how they are
    carried through atopile."""
    import copy

    from extract_kicad import push_overrides_to_pads

    fp = copy.deepcopy(fp)
    push_overrides_to_pads(fp)
    return Counter(_canon(c) for c in fp[2:] if isinstance(c, list) and c and c[0] not in ("property", "at", "layer") and c[0] not in _FP_DROP)


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
            "body": fp_body(fp),
            "props": {p[1]: _canon([c for c in p if not (isinstance(c, list) and c and c[0] == "uuid")])
                      for p in children(fp, "property") if p[1] != "ki_fp_filters"},
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
            locked = value(s, "locked") == "yes" or "locked" in [str(a) for a in s if not isinstance(a, list)]
            copper.append((kind, geo, str(value(s, "layer") or ""), locked, net_of(s)))
    zones = [(net_of(z) if child(z, "net") is not None else value(z, "net_name"),
              str(value(z, "layer") or value(z, "layers") or "")) for z in children(pcb, "zone")]
    # everything else that ends up in the fab data: outline + graphics,
    # board setup / stackup / layer table, and the full zone definitions
    # (outline, priority, clearances, thermal settings, filled polygons)
    board = {}
    for kind in BOARD_KINDS:
        items = children(pcb, kind)
        # child order inside an item is not significant to KiCad
        def unordered(item, drop=()):
            atoms = [_canon(c) for c in item if not isinstance(c, list)]
            kids = sorted(_canon(c) for c in item if isinstance(c, list) and c and c[0] not in drop and c[0] not in _FP_DROP)
            return " ".join(atoms + kids)

        board[kind] = Counter(unordered(c, ("net", "net_name") if kind == "zone" else ()) for c in items)
    return fps, pads, copper, zones, board


BOARD_KINDS = ("layers", "setup", "gr_line", "gr_arc", "gr_circle", "gr_rect", "gr_poly",
               "gr_curve", "gr_text", "gr_text_box", "dimension", "image", "zone")


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
    ap.add_argument("--title", default="Netlist + layout equivalence", help="heading for the markdown summary")
    ap.add_argument(
        "--summary",
        type=Path,
        default=Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None,
        help="append a markdown report here (defaults to $GITHUB_STEP_SUMMARY in CI)",
    )
    args = ap.parse_args()

    o_fps, o_pads, o_cu, o_zones, o_board = load(args.orig)
    n_fps, n_pads, n_cu, n_zones, n_board = load(args.new)
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

    # 1b. full footprint content (graphics, courtyard, attributes, overrides)
    body_diff = Counter()
    for r in sorted(set(o_fps) & set(n_fps)):
        lost, added = o_fps[r]["body"] - n_fps[r]["body"], n_fps[r]["body"] - o_fps[r]["body"]
        if lost or added:
            kinds = sorted({x[1:].split(" ", 1)[0] for x in list(lost) + list(added)})
            body_diff[tuple(kinds)] += 1
            if sum(body_diff.values()) <= 15:
                ex = (list(lost) or list(added))[0][:160]
                errors.append(f"{r}: footprint content differs ({', '.join(kinds)}), e.g. {ex}")
    if sum(body_diff.values()) > 15:
        errors.append(f"... {sum(body_diff.values())} footprints differ in total: "
                      + ", ".join(f"{'/'.join(k)} x{v}" for k, v in body_diff.most_common()))

    # 1c. text fields: every original field (Reference/Value text, position,
    # layer, visibility, hidden metadata) must survive unchanged; atopile may
    # only add its own hidden bookkeeping fields
    ato_owned = {"atopile_address", "atopile_subaddresses", "LCSC", "lcsc_id", "Partnumber", "Manufacturer"}
    prop_diff = []
    for r in sorted(set(o_fps) & set(n_fps)):
        op, np_ = o_fps[r]["props"], n_fps[r]["props"]
        bad = [k for k in op if op[k] != np_.get(k)] + [k for k in np_ if k not in op and k not in ato_owned]
        if bad:
            prop_diff.append((r, bad))
    for r, bad in prop_diff[:10]:
        errors.append(f"{r}: text fields differ: {bad[:6]}")
    if len(prop_diff) > 10:
        errors.append(f"... text fields differ on {len(prop_diff)} footprints in total")

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
        oc = Counter((k, g, l, lk, name_map.get(n, n)) for k, g, l, lk, n in o_cu)
        nc = Counter(n_cu)
        lost, added = oc - nc, nc - oc
        if lost:
            errors.append(f"{sum(lost.values())} copper items lost or re-netted, e.g. {list(lost)[:3]}")
        if added:
            errors.append(f"{sum(added.values())} copper items added or re-netted, e.g. {list(added)[:3]}")
        for kind in BOARD_KINDS:
            lost, added = o_board[kind] - n_board[kind], n_board[kind] - o_board[kind]
            if lost or added:
                ex = (list(lost) or list(added))[0][:200]
                errors.append(f"board {kind}: {sum(lost.values())} item(s) lost, {sum(added.values())} added, e.g. {ex}")
        oz = Counter((name_map.get(n, n), l) for n, l in o_zones)
        nz = Counter(n_zones)
        if oz != nz:
            errors.append(f"zones differ: lost {dict(oz - nz)} added {dict(nz - oz)}")

    rows = [
        ("references", len(o_fps), len(n_fps)),
        ("multi-pad nets", len(po), len(pn)),
        ("tracks + arcs + vias", len(o_cu), len(n_cu)),
        ("zones", len(o_zones), len(n_zones)),
    ]
    for name, a, b in rows:
        print(f"{name:21}: {a} original / {b} atopile")
    print(f"{'net names renamed':21}: {len(renamed)}")
    for o, n in sorted(renamed.items())[:15]:
        print(f"    {o!r} -> {n!r}")
    ok = not errors
    if ok:
        print("\nPASS: atopile build is netlist- and layout-identical to the KiCad V3.2 board")
    else:
        print(f"\nFAIL: {len(errors)} difference(s)")
        for e in errors:
            print("  -", e)

    if args.summary:
        def rel(p: Path) -> str:
            try:
                return str(p.resolve().relative_to(REPO))
            except ValueError:
                return str(p)

        md = [f"### {'✅' if ok else '❌'} {args.title}", "", f"`{rel(args.new)}` vs `{rel(args.orig)}`", "",
              "| | original | atopile |", "|---|---:|---:|"]
        md += [f"| {name} | {a} | {b} |" for name, a, b in rows]
        md += [f"| net names renamed | | {len(renamed)} |", ""]
        if not ok:
            md += [f"**{len(errors)} difference(s):**", ""] + [f"- `{e}`" for e in errors[:50]] + [""]
        with args.summary.open("a") as f:
            f.write("\n".join(md) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
