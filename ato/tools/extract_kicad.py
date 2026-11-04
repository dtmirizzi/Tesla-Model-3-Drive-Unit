#!/usr/bin/env python3
"""Extract a reference netlist + footprint/symbol library from the KiCad V3.2 design.

Inputs : Design/M3DU_BoardV3_2/M3DU_BoardV3_2.kicad_{pcb,sch}
         Design/M3DU_BoardV3_2/Production/M3DUBoard_V3_2_BOM_JLC.xls (LCSC part numbers)
Outputs: ato/reference/netlist.json          -- golden netlist (PCB pad -> net), per-ref metadata
         ato/lib/footprints/<Lib>.pretty/     -- footprints recovered from the board
         ato/lib/symbols/<Lib>/<Name>.kicad_sym -- symbols recovered from the schematic

The PCB is used as the source of truth for connectivity because it is what was
fabricated and tested; the schematic only contributes symbols, pin names and
part metadata.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sexp  # noqa: E402
from sexp import Sym, child, children, head, prop, value  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "Design" / "M3DU_BoardV3_2"
OUT = REPO / "ato"

STRIP_KEYS = {"uuid", "tstamp", "path", "sheetname", "sheetfile", "net", "pinfunction", "pintype"}


def fnum(x) -> float:
    return float(x)


def fmt(x: float) -> Sym:
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    return Sym("0" if s in ("-0", "") else s)


# --------------------------------------------------------------------------
# footprint normalisation (board instance -> library footprint)
# --------------------------------------------------------------------------


def _strip(node):
    if not isinstance(node, list):
        return node
    return [
        _strip(c)
        for c in node
        if not (isinstance(c, list) and (head(c) in STRIP_KEYS or (head(c) == "property" and c[1] == "ki_fp_filters")))
    ]


def _unrotate(node, rot: float):
    """KiCad stores pad/text orientation on the board as absolute; make it local."""
    if not isinstance(node, list):
        return
    for c in node:
        if not isinstance(c, list):
            continue
        if head(c) in ("pad", "property", "fp_text") and (at := child(c, "at")) is not None:
            ang = fnum(at[3]) if len(at) > 3 and isinstance(at[3], Sym) and at[3] != "unlocked" else 0.0
            new = (ang - rot) % 360
            rest = [a for a in at[3:] if a == "unlocked"]
            at[:] = [at[0], at[1], at[2]] + ([fmt(new)] if abs(new) > 1e-9 else []) + rest
        _unrotate(c, rot)


_COORD_HEADS = {"at", "start", "end", "mid", "center", "xy", "offset"}


def _flip(node):
    """Undo a KiCad 'flip to back': mirror Y, swap F./B. layers, negate angles."""
    if not isinstance(node, list):
        return
    h = head(node)
    if h in _COORD_HEADS and len(node) >= 3:
        try:
            node[2] = fmt(-fnum(node[2]))
        except ValueError:
            pass
        if h == "at" and len(node) > 3:
            try:
                node[3] = fmt((-fnum(node[3])) % 360)
            except ValueError:
                pass
    if h in ("layer", "layers"):
        node[1:] = [_swap_layer(a) if isinstance(a, str) else a for a in node[1:]]
    if h == "justify":
        node[1:] = [a for a in node[1:] if a != "mirror"]
    for c in node:
        _flip(c)


def _swap_layer(name: str) -> str:
    if name.startswith("F."):
        return "B." + name[2:]
    if name.startswith("B."):
        return "F." + name[2:]
    return name


def normalise_footprint(fp: list) -> list:
    fp = copy.deepcopy(fp)
    at = child(fp, "at")
    rot = fnum(at[3]) if at is not None and len(at) > 3 else 0.0
    on_back = value(fp, "layer") == "B.Cu"
    _unrotate(fp, rot)
    if on_back:
        _flip(fp)
    fp = _strip(fp)
    fp = [c for c in fp if not (isinstance(c, list) and head(c) in ("at", "locked"))]
    for p in children(fp, "property"):
        if p[1] == "Reference":
            p[2] = "REF**"
    push_overrides_to_pads(fp)
    return fp


# footprint-level defaults that atopile 0.15.9 cannot represent on a footprint
# but can on a pad; KiCad applies them to every pad without its own value
PAD_INHERITED = ("clearance", "zone_connect", "solder_mask_margin", "solder_paste_margin", "solder_paste_margin_ratio")


def push_overrides_to_pads(fp: list) -> None:
    for key in PAD_INHERITED:
        if (fp_level := child(fp, key)) is None:
            continue
        for pad in children(fp, "pad"):
            if child(pad, key) is None:
                pad.append(copy.deepcopy(fp_level))


def canon(node, nd: int = 4) -> str:
    """Order-preserving canonical text of an S-expression; numbers rounded."""
    if not isinstance(node, list):
        if isinstance(node, Sym):
            try:
                return f"{round(float(node), nd) + 0.0:g}"
            except ValueError:
                return str(node)
        return repr(str(node))
    return "(" + " ".join(canon(c, nd) for c in node) + ")"


def footprint_signature(fp_norm: list) -> tuple:
    """Everything that makes up the footprint except its properties.

    atopile rewrites board footprints from the library copy, so two instances
    may only share a library footprint if they are identical: same pads (incl.
    per-pad zone/thermal overrides), graphics, courtyard, attributes, models.
    """
    return tuple(sorted(canon(c) for c in fp_norm[2:] if isinstance(c, list) and head(c) != "property"))


def to_library_fp(fp_norm: list, name: str) -> list:
    out = [Sym("footprint"), name, [Sym("version"), Sym("20241229")], [Sym("generator"), "pcbnew"],
           [Sym("generator_version"), "9.0"]]
    out += [c for c in fp_norm[2:] if not (isinstance(c, list) and head(c) in ("version", "generator", "generator_version"))]
    for c in out:
        if isinstance(c, list) and head(c) == "layer":
            c[1] = "F.Cu"
    return out


# --------------------------------------------------------------------------
# schematic
# --------------------------------------------------------------------------


def load_schematic(path: Path):
    sch = sexp.loads(path.read_text())
    libsyms = {s[1]: s for s in children(child(sch, "lib_symbols"), "symbol")}
    insts: dict[str, dict] = {}
    for s in children(sch, "symbol"):
        ref = prop(s, "Reference")
        if not ref or ref.startswith("#"):
            continue
        lib_name = value(s, "lib_name") or value(s, "lib_id")
        d = insts.setdefault(ref, {
            "lib_id": value(s, "lib_id"),
            "lib_name": lib_name,
            "value": prop(s, "Value"),
            "footprint": prop(s, "Footprint"),
            "mpn": prop(s, "Manufacturer_Part_Number") or prop(s, "MPN"),
            "lcsc": prop(s, "LCSC") or prop(s, "LCSC PN"),
            "datasheet": prop(s, "Datasheet"),
            "dnp": value(s, "dnp") == "yes",
            "in_bom": value(s, "in_bom") != "no",
            "units": [],
        })
        d["units"].append(int(value(s, "unit", 1)))
    return libsyms, insts


def symbol_pins(sym: list) -> dict[str, str]:
    """pin number -> pin name over all units."""
    pins: dict[str, str] = {}
    for unit in children(sym, "symbol"):
        for p in children(unit, "pin"):
            num = value(child(p, "number"), "number") if False else child(p, "number")[1]
            name = child(p, "name")[1]
            pins.setdefault(str(num), str(name))
    return pins


def to_library_symbol(sym: list, bare: str) -> list:
    sym = copy.deepcopy(sym)
    sym[1] = bare
    for unit in children(sym, "symbol"):
        # sub-unit names are "<name>_<unit>_<style>"; keep the suffix, fix the stem
        m = re.match(r".*_(\d+_\d+)$", unit[1])
        if m:
            unit[1] = f"{bare}_{m.group(1)}"
    return [Sym("kicad_symbol_lib"), [Sym("version"), Sym("20241209")],
            [Sym("generator"), "kicad_symbol_editor"], [Sym("generator_version"), "9.0"], sym]


# --------------------------------------------------------------------------


def load_lcsc(bom_xls: Path) -> dict[str, str]:
    try:
        import pandas as pd
    except ImportError:
        print("pandas/xlrd not installed: LCSC numbers skipped", file=sys.stderr)
        return {}
    df = pd.read_excel(bom_xls)
    out = {}
    for _, row in df.iterrows():
        for ref in str(row["Designator"]).split(","):
            ref = ref.strip()
            if ref and isinstance(row["JLCPCB Part#"], str):
                out[ref] = row["JLCPCB Part#"].strip()
    return out


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", name)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=SRC)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    pcb = sexp.loads((args.src / "M3DU_BoardV3_2.kicad_pcb").read_text())
    libsyms, sch = load_schematic(args.src / "M3DU_BoardV3_2.kicad_sch")
    lcsc = load_lcsc(args.src / "Production" / "M3DUBoard_V3_2_BOM_JLC.xls")

    fp_dir = args.out / "lib" / "footprints"
    sym_dir = args.out / "lib" / "symbols"

    # ---- footprints -----------------------------------------------------
    # group instances by lib id, and split into variants if pad geometry differs
    variants: dict[str, dict[tuple, str]] = defaultdict(dict)  # fpid -> sig -> variant name
    chosen: dict[str, tuple[int, list]] = {}  # variant name -> (score, normalised fp)
    components = {}
    warnings = []
    variant_notes = []

    for fp in children(pcb, "footprint"):
        fpid = fp[1]
        ref = prop(fp, "Reference")
        attr = child(fp, "attr") or []
        norm = normalise_footprint(fp)
        sig = footprint_signature(norm)
        lib, name = fpid.split(":", 1) if ":" in fpid else ("Board", fpid)
        vmap = variants[fpid]
        if sig not in vmap:
            vmap[sig] = name if not vmap else f"{name}__v{len(vmap) + 1}"
            if len(vmap) > 1:
                variant_notes.append(f"{fpid}: variant {vmap[sig]} (first seen on {ref})")
        vname = vmap[sig]
        at = child(fp, "at")
        rot = fnum(at[3]) if len(at) > 3 else 0.0
        score = (value(fp, "layer") == "F.Cu") * 2 + (abs(rot) < 1e-9)
        key = f"{lib}:{vname}"
        if key not in chosen or score > chosen[key][0]:
            chosen[key] = (score, norm)

        pads = {}
        for p in children(fp, "pad"):
            num = str(p[1])
            net = child(p, "net")
            netname = str(net[-1]) if net is not None else None
            if num == "":
                continue
            prev = pads.get(num)
            if prev is not None and prev != netname:
                warnings.append(f"{ref}: pad {num} has multiple nets {prev!r} / {netname!r}")
            pads[num] = netname if netname is not None else prev
        if ref in components:
            warnings.append(f"duplicate reference {ref} on PCB")
        s = sch.get(ref, {})
        components[ref] = {
            "footprint": key,
            "value": prop(fp, "Value"),
            "layer": value(fp, "layer"),
            "attrs": [str(a) for a in attr[1:]],
            "pads": pads,
            "symbol": s.get("lib_name"),
            "lib_id": s.get("lib_id"),
            "mpn": s.get("mpn"),
            "lcsc": lcsc.get(ref) or s.get("lcsc"),
            "datasheet": s.get("datasheet") if s.get("datasheet") not in (None, "", "~") else None,
            "dnp": s.get("dnp", False) or "dnp" in [str(a) for a in attr],
            "in_schematic": ref in sch,
        }

    for key, (_, norm) in chosen.items():
        lib, vname = key.split(":", 1)
        d = fp_dir / f"{safe(lib)}.pretty"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{safe(vname)}.kicad_mod").write_text(sexp.dumps(to_library_fp(norm, vname)) + "\n")

    # ---- symbols --------------------------------------------------------
    used_syms = {c["symbol"] for c in components.values() if c["symbol"]}
    symbols = {}
    for sname in sorted(used_syms):
        sym = libsyms.get(sname)
        if sym is None:
            warnings.append(f"symbol {sname} not embedded in schematic")
            continue
        lib, bare = sname.split(":", 1) if ":" in sname else ("Local", sname)
        d = sym_dir / safe(lib)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{safe(bare)}.kicad_sym").write_text(sexp.dumps(to_library_symbol(sym, bare)) + "\n")
        symbols[sname] = {"file": f"lib/symbols/{safe(lib)}/{safe(bare)}.kicad_sym",
                          "pins": symbol_pins(sym)}

    only_sch = sorted(set(sch) - set(components))
    nets = defaultdict(list)
    for ref, c in components.items():
        for num, n in c["pads"].items():
            if n:
                nets[n].append(f"{ref}.{num}")

    ref_out = args.out / "reference"
    ref_out.mkdir(parents=True, exist_ok=True)
    data = {
        "source": "Design/M3DU_BoardV3_2/M3DU_BoardV3_2.kicad_pcb",
        "components": dict(sorted(components.items())),
        "nets": {k: sorted(v) for k, v in sorted(nets.items())},
        "symbols": symbols,
        "schematic_only_refs": only_sch,
        "warnings": warnings,
    }
    (ref_out / "netlist.json").write_text(json.dumps(data, indent=1) + "\n")

    print(f"components on PCB : {len(components)}")
    print(f"nets              : {len(nets)}")
    print(f"footprints written: {len(chosen)} ({len(variant_notes)} per-instance variants)")
    print(f"symbols written   : {len(symbols)}")
    print(f"LCSC numbers      : {sum(1 for c in components.values() if c['lcsc'])}")
    print(f"schematic-only    : {len(only_sch)} {only_sch[:20]}")
    print(f"warnings          : {len(warnings)}")
    for w in warnings[:40]:
        print("  ", w)


if __name__ == "__main__":
    main()
