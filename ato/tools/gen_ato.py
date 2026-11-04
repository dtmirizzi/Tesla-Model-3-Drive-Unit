#!/usr/bin/env python3
"""Generate a flat, netlist-exact atopile port from reference/netlist.json.

Writes:
  parts/<FootprintLib>/<Part>.ato        one atopile component per BOM line
  parts/<FootprintLib>/*.kicad_mod       footprints (copied from lib/footprints)
  parts/<FootprintLib>/*.kicad_sym       symbols   (copied from lib/symbols)
  main.ato                               module M3DU: every instance + every net

This is Phase 3 of the port ("exact flat port"). It is intentionally mechanical;
the hand-written, modular design replaces main.ato block by block later, with
tools/check_netlist.py proving equivalence at every step.
"""

from __future__ import annotations

import argparse
import json
import keyword
import re
import shutil
from collections import defaultdict
from pathlib import Path

ATO = Path(__file__).resolve().parents[1]

ATO_KEYWORDS = {
    "module", "component", "interface", "signal", "pin", "new", "import", "from",
    "assert", "within", "is", "to", "trait", "pass", "for", "in", "True", "False",
}

# References that are left out of the port entirely (none at the moment; the
# silkscreen logo G*** is kept as a pad-less part so atopile doesn't delete it)
SKIP_REFS: set[str] = set()

STUB_SYMBOL = """(kicad_symbol_lib
\t(version 20241209)
\t(generator "kicad_symbol_editor")
\t(symbol "Graphic"
\t\t(exclude_from_sim yes)
\t\t(in_bom no)
\t\t(on_board yes)
\t\t(property "Reference" "G" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "Graphic" (at 0 -2.54 0) (effects (font (size 1.27 1.27))))
\t\t(symbol "Graphic_0_1"
\t\t\t(rectangle (start -2.54 -1.27) (end 2.54 1.27) (stroke (width 0) (type default)) (fill (type none)))
\t\t)
\t)
)
"""


def ident(raw: str, *, prefix: str = "n", signs: bool = True) -> str:
    """Turn a KiCad name into an ato identifier.

    signs=True keeps polarity readable for pins/nets (IN- -> IN_neg, +3V3 -> plus_3V3);
    signs=False is for part type names, where '-' is just a separator.
    """
    s = raw.strip()
    if not signs:
        s = s.replace("-", "_").replace("+", "p")
    s = s.replace("~{", "n").replace("}", "")
    s = re.sub(r"^\+", "plus_", s)
    s = re.sub(r"^-", "minus_", s)
    s = s.replace("+", "_pos").replace("-", "_neg").replace("/", "_").replace(".", "_")
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        return ""
    if s[0].isdigit():
        s = f"{prefix}{s}"
    if s in ATO_KEYWORDS or keyword.iskeyword(s):
        s += "_"
    return s


def inst_name(ref: str) -> str:
    """main.ato instance name for a reference designator (G*** -> logo)."""
    return "logo" if ref == "G***" else ident(ref.lower(), prefix="x")


def fp_file_name(variant: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", variant) + ".kicad_mod"


def designator_prefix(ref: str) -> str:
    m = re.match(r"[A-Za-z]+", ref)
    return m.group(0) if m else "X"


def pin_map(pads: list[str], sym_pins: dict[str, str]) -> dict[str, str]:
    """pad number -> unique signal identifier."""
    names: dict[str, str] = {}
    for num in pads:
        nm = ident(sym_pins.get(num, ""))
        if not nm or nm.isdigit() or re.fullmatch(r"n\d+", nm):
            nm = f"p{num}"
        names[num] = nm
    counts = defaultdict(int)
    for nm in names.values():
        counts[nm] += 1
    # same-named pins (e.g. several VSS) stay separate signals; nets join them
    return {num: (nm if counts[nm] == 1 else f"{nm}_{num}") for num, nm in names.items()}


def natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ATO)
    args = ap.parse_args()
    root: Path = args.root

    ref = json.loads((root / "reference" / "netlist.json").read_text())
    comps = {r: c for r, c in ref["components"].items() if r not in SKIP_REFS}
    symbols = ref["symbols"]

    parts_dir = root / "parts"
    if parts_dir.exists():
        shutil.rmtree(parts_dir)
    stub = root / "lib" / "symbols" / "Local" / "Graphic.kicad_sym"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(STUB_SYMBOL)

    # ---- part types (one per distinct BOM line) ---------------------------
    types: dict[tuple, dict] = {}
    type_names: set[str] = set()
    inst_type: dict[str, dict] = {}
    for r in sorted(comps, key=natural):
        c = comps[r]
        key = (c["footprint"], c["symbol"], c["value"], c["lcsc"], c["mpn"])
        if key not in types:
            lib, variant = c["footprint"].split(":", 1)
            base = ident(f"{c['value']}_{variant}", prefix="v", signs=False) or ident(variant, prefix="v", signs=False)
            name, i = base, 2
            while name in type_names:
                name, i = f"{base}_{i}", i + 1
            type_names.add(name)
            sym = symbols.get(c["symbol"]) or {"pins": {}, "file": "lib/symbols/Local/Graphic.kicad_sym"}
            pads = sorted(c["pads"], key=natural)
            types[key] = {
                "name": name,
                "lib": lib,
                "variant": variant,
                "symbol": c["symbol"],
                "sym_file": sym["file"],
                "pins": pin_map(pads, sym["pins"]),
                "value": c["value"],
                "lcsc": c["lcsc"],
                "mpn": c["mpn"],
                "prefix": designator_prefix(r),
                "datasheet": c.get("datasheet"),
                "refs": [],
            }
        types[key]["refs"].append(r)
        inst_type[r] = types[key]

    for t in types.values():
        d = parts_dir / t["lib"]
        d.mkdir(parents=True, exist_ok=True)
        fp_src = root / "lib" / "footprints" / f"{t['lib']}.pretty" / fp_file_name(t["variant"])
        shutil.copy(fp_src, d / fp_src.name)
        sym_name = Path(t["sym_file"]).parent.name + "__" + Path(t["sym_file"]).name
        shutil.copy(root / t["sym_file"], d / sym_name)
        partno = t["mpn"] or t["value"]
        dnp_refs = [r for r in t["refs"] if comps[r]["dnp"]]
        lines = [
            '#pragma experiment("TRAITS")',
            "import is_atomic_part",
            "import has_designator_prefix",
        ]
        if t["lcsc"]:
            lines.append("import has_part_picked")
        lines += ["", f"component {t['name']}:"]
        doc = f"{t['value']} ({t['symbol']}, {t['lib']}:{t['variant']}) -- used by {', '.join(t['refs'])}"
        lines.append(f'    """{doc}"""')
        lines.append("    # generated by tools/gen_ato.py from the KiCad V3.2 design")
        lines.append(
            f'    trait is_atomic_part<manufacturer="{"unknown"}", '
            f'partnumber="{_q(partno)}", footprint="{fp_src.name}", symbol="{sym_name}">'
        )
        if t["lcsc"] and not dnp_refs:
            lines.append(
                f'    trait has_part_picked::by_supplier<supplier_id="lcsc", supplier_partno="{t["lcsc"]}", '
                f'manufacturer="{"unknown"}", partno="{_q(partno)}">'
            )
        lines.append(f'    trait has_designator_prefix<prefix="{t["prefix"]}">')
        lines.append("")
        for num, nm in sorted(t["pins"].items(), key=lambda kv: natural(kv[0])):
            lines.append(f"    signal {nm} ~ pin {num}")
        (d / f"{t['name']}.ato").write_text("\n".join(lines) + "\n")

    # ---- main.ato -----------------------------------------------------------
    inst_names = {r: inst_name(r) for r in comps}
    assert len(set(inst_names.values())) == len(inst_names), "instance name collision"
    used = set(inst_names.values())

    out = [
        '"""Tesla Model 3 drive unit logic board (M3DU V3.2) -- flat atopile port.',
        "",
        "GENERATED by tools/gen_ato.py from Design/M3DU_BoardV3_2. Do not hand-edit:",
        "regenerate, or move blocks into hand-written modules (see PLAN.md).",
        '"""',
        "",
        '#pragma experiment("TRAITS")',
        '#pragma experiment("INSTANCE_TRAITS")',
        "import has_designator",
    ]
    for t in sorted(types.values(), key=lambda t: (t["lib"], t["name"])):
        out.append(f'from "parts/{t["lib"]}/{t["name"]}.ato" import {t["name"]}')
    out += ["", "module M3DU:", '    """M3DU logic board, V3.2 (RDU population)."""', ""]

    out.append("    # ---- parts ----")
    for r in sorted(comps, key=natural):
        out.append(f"    {inst_names[r]} = new {inst_type[r]['name']}")
    out += ["", "    # ---- designators (kept identical to the KiCad design) ----"]
    for r in sorted(comps, key=natural):
        out.append(f'    trait {inst_names[r]} has_designator<designator="{r}">')

    out += ["", "    # ---- nets ----"]
    net_idents: dict[str, str] = {}
    n_conn = 0
    for net, pins in sorted(ref["nets"].items(), key=lambda kv: natural(kv[0])):
        pins = [p for p in pins if p.split(".", 1)[0] in comps]
        endpoints = []
        for p in pins:
            r, num = p.split(".", 1)
            endpoints.append(f"{inst_names[r]}.{inst_type[r]['pins'][num]}")
        if not endpoints:
            continue
        named = not net.startswith(("Net-(", "unconnected-"))
        if not named and len(endpoints) < 2:
            continue
        out.append(f"    # {net}")
        if named:
            nid = ident(net, prefix="net_")
            while nid in used:
                nid += "_net"
            used.add(nid)
            net_idents[net] = nid
            out.append(f"    signal {nid}")
            for e in endpoints:
                out.append(f"    {nid} ~ {e}")
                n_conn += 1
        else:
            for e in endpoints[1:]:
                out.append(f"    {endpoints[0]} ~ {e}")
                n_conn += 1
    (root / "main.ato").write_text("\n".join(out) + "\n")
    (root / "reference" / "net_idents.json").write_text(json.dumps(net_idents, indent=1) + "\n")

    print(f"part types : {len(types)}")
    print(f"instances  : {len(comps)}")
    print(f"named nets : {len(net_idents)}")
    print(f"connections: {n_conn}")


def _q(s: str) -> str:
    return str(s).replace('"', "'")


if __name__ == "__main__":
    main()
