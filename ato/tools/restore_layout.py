#!/usr/bin/env python3
"""Put back footprint attributes that atopile 0.15.9 drops when it writes the board.

atopile's KiCad data model has no fields for several footprint attributes, so
every `ato build` silently removes them from the layout:

  footprint level : locked, clearance, zone_connect, solder mask/paste margins,
                    groups, Fab/user text (fp_text), 3D model options (hide)
  pad level       : thermal_bridge_angle
  board level     : `locked` on any item (zones, images, tracks, graphics),
                    and the setup block, where atopile also rewrites the
                    stackup (soldermask colour -> Black, copper finish -> ENIG)
                    and drops the grid origin

Some of these are electrically meaningful (zone_connect and clearance change
how copper pours connect and what DRC allows). extract_kicad.py already pushes
the footprint-level clearance/zone_connect onto every pad, which atopile does
keep. This script restores the rest from a reference board, but only on
footprints atopile left as the *same* footprint (same footprint ID as in
reference/netlist.json, same position, rotation and side). A part whose
footprint was really changed in the atopile source is never overwritten.

Run after `ato build`; `tools/port.sh` does this automatically.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sexp  # noqa: E402
from sexp import child, children, head, prop, value  # noqa: E402

ATO = Path(__file__).resolve().parents[1]
REPO = ATO.parent
REF = REPO / "Design" / "M3DU_BoardV3_2" / "M3DU_BoardV3_2.kicad_pcb"
BOARD = ATO / "layouts" / "default" / "default.kicad_pcb"

FP_SCALARS = ("locked", "clearance", "zone_connect", "solder_mask_margin",
              "solder_paste_margin", "solder_paste_margin_ratio")
FP_LISTS = ("group", "fp_text", "model")
PAD_SCALARS = ("thermal_bridge_angle",)


LOCKED = re.compile(r"^\t\t\(locked yes\)$|\(locked yes\)", re.M)
UUID = re.compile(r'^\t\t\(uuid "([^"]+)"\)', re.M)


def top_level_blocks(text: str, spans: bool = False):
    """Yield (kind, block[, start, end]) for every top-level '(kind ...)' item."""
    i = text.find("\n\t(")
    while i != -1:
        start = i + 1
        depth, j = 0, start
        while True:
            c = text[j]
            if c == '"':
                j += 1
                while text[j] != '"':
                    j += 2 if text[j] == "\\" else 1
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        block = text[start:j + 1]
        kind = block.lstrip()[1:].split(None, 1)[0].rstrip(")")
        yield (kind, block, start, j + 1) if spans else (kind, block)
        i = text.find("\n\t(", j + 1)


def item_uuid(block: str) -> str | None:
    m = UUID.search(block)
    return m.group(1) if m else None


def block_head(block: str) -> str:
    """The item's own direct children lines (depth 2), not nested ones."""
    return "\n".join(l for l in block.split("\n") if l.startswith("\t\t(") and not l.startswith("\t\t\t"))


def placement(fp):
    at = child(fp, "at")
    return (value(fp, "layer"), tuple(round(float(x), 4) for x in at[1:3]),
            round(float(at[3]) % 360, 3) if len(at) > 3 else 0.0)


def pad_key(p):
    at = child(p, "at")
    return (str(p[1]), round(float(at[1]), 3), round(float(at[2]), 3))


def restore(new_fp, ref_fp) -> int:
    changed = 0
    # footprint-level scalars: insert right after (layer ...), like KiCad writes them
    for key in FP_SCALARS:
        r = child(ref_fp, key)
        n = child(new_fp, key)
        if r is not None and n is None:
            idx = next(i for i, c in enumerate(new_fp) if isinstance(c, list) and head(c) == "layer") + 1
            new_fp.insert(idx, copy.deepcopy(r))
            changed += 1
    # whole lists that atopile rewrites lossily
    for key in FP_LISTS:
        r_items = children(ref_fp, key)
        if [sexp.dumps(c) for c in children(new_fp, key)] == [sexp.dumps(c) for c in r_items]:
            continue
        first = next((i for i, c in enumerate(new_fp) if isinstance(c, list) and head(c) == key), None)
        new_fp[:] = [c for c in new_fp if not (isinstance(c, list) and head(c) == key)]
        if first is None:
            first = len(new_fp) if key == "model" else next(
                (i for i, c in enumerate(new_fp) if isinstance(c, list) and head(c) in ("pad", "model")), len(new_fp))
        new_fp[first:first] = copy.deepcopy(r_items)
        changed += 1
    # pad-level scalars
    ref_pads = {pad_key(p): p for p in children(ref_fp, "pad")}
    for p in children(new_fp, "pad"):
        rp = ref_pads.get(pad_key(p))
        if rp is None:
            continue
        for key in PAD_SCALARS:
            if child(rp, key) is not None and child(p, key) is None:
                p.append(copy.deepcopy(child(rp, key)))
                changed += 1
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", type=Path, default=REF, help="board to restore attributes from")
    ap.add_argument("--board", type=Path, default=BOARD, help="atopile-built board (edited in place)")
    args = ap.parse_args()

    expected = {r: c["footprint"] for r, c in json.loads((ATO / "reference" / "netlist.json").read_text())["components"].items()}
    ref = {prop(f, "Reference"): f for f in children(sexp.loads(args.ref.read_text()), "footprint")}

    ref_text = args.ref.read_text()
    ref_blocks = {}
    for kind, block in top_level_blocks(ref_text):
        if kind == "setup":
            ref_blocks["setup"] = block
    locked_uuids = {u for _, block in top_level_blocks(ref_text)
                    if (u := item_uuid(block)) and LOCKED.search(block_head(block))}

    text = args.board.read_text()
    out, pos = [], 0
    n_fp = n_changes = n_skipped = n_locked = 0
    setup_restored = False
    # edit top-level blocks in place; everything else stays byte-identical
    for kind, block, start, end in top_level_blocks(text, spans=True):
        new = block
        if kind == "footprint":
            fp = sexp.loads(block)
            r = prop(fp, "Reference")
            rf = ref.get(r)
            if rf is not None and str(fp[1]) == expected.get(r) and placement(fp) == placement(rf):
                k = restore(fp, rf)
                if k:
                    new = sexp.dumps(fp, indent=1)
                    n_fp += 1
                    n_changes += k
            elif rf is not None:
                n_skipped += 1
        elif kind == "setup" and "setup" in ref_blocks and block != ref_blocks["setup"]:
            new = ref_blocks["setup"]
            setup_restored = True
        elif (u := item_uuid(block)) in locked_uuids and not LOCKED.search(block_head(block)):
            nl = block.index("\n")
            new = block[:nl] + "\n\t\t(locked yes)" + block[nl:]
            n_locked += 1
        out.append(text[pos:start])
        out.append(new)
        pos = end
    out.append(text[pos:])
    args.board.write_text("".join(out))
    print(f"restored {n_changes} dropped attribute(s) on {n_fp} footprint(s); "
          f"{n_skipped} footprint(s) changed in the atopile source were left alone")
    print(f"restored locked flag on {n_locked} board item(s); setup/stackup "
          + ("restored" if setup_restored else "unchanged"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
