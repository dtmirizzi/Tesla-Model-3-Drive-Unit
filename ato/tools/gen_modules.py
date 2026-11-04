#!/usr/bin/env python3
"""Generate the modular atopile design (src/*.ato + main.ato).

The V3.2 schematic is one flat A0 sheet, but it is drawn as titled boxes
("LV POWER SUPPLY", "Gate Driver, Phase A", ...). This script turns each box
into an atopile module:

* every part goes to the module of the box it is drawn in
* nets that stay inside a box stay inside the module; nets that leave it become
  module ports (signals), and the main supply rails become typed `ElectricPower`
  ports with their voltage declared once at the top level
* the three gate-driver phases are proven structurally identical (graph
  matching on part type + pin-level connectivity) and become ONE reusable
  `GateDrivePhase` module instantiated three times; per-phase BOM differences
  become a derived module that retypes just those parts
* the schematic's engineering notes for each box are kept in the module docstring

Designators and the layout binding are preserved: reference/addresses.json maps
every designator to its new atopile address, and seed_layout.py tags the board
with it. tools/check_netlist.py then proves the result is the same board.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sexp  # noqa: E402
from gen_ato import ident, inst_name, load, natural, write_parts  # noqa: E402
from sexp import child, children, prop  # noqa: E402

ATO = Path(__file__).resolve().parents[1]
REPO = ATO.parent
SCH = REPO / "Design" / "M3DU_BoardV3_2" / "M3DU_BoardV3_2.kicad_sch"

# schematic box title -> (module type, source file, instance name)
SECTIONS: dict[str, tuple[str, str, str]] = {
    "LV POWER SUPPLY": ("LvSupply", "src/power/lv_supply.ato", "lv_supply"),
    "HV REF +5V POWER SUPPLY": ("HvRefSupply", "src/power/hv_ref_supply.ato", "hv_ref_supply"),
    "GATE DRIVER ISOLATED POWER SUPPLY": ("GatePsu", "src/power/gate_psu.ato", "gate_psu"),
    "GATE DRIVER INTERFACE": ("GateInterface", "src/gate_drive/interface.ato", "gate_if"),
    "DC BUS DISCH AND VOLTAGE MEASURE": ("DcBus", "src/sensing/dc_bus.ato", "dc_bus"),
    "CURRENT SENSORS": ("CurrentSensors", "src/sensing/current.ato", "current_sense"),
    "HS TEMP SENSORS": ("HeatsinkTemps", "src/sensing/temps.ato", "hs_temps"),
    "MOTOR TEMP SENSOR": ("MotorTemp", "src/sensing/temps.ato", "motor_temp"),
    "RESOLVER AND EXCITER AMP": ("Resolver", "src/resolver.ato", "resolver"),
    "OVERCURRENT AND IGBT FAULT DETECT": ("OvercurrentDetect", "src/protection/ocp.ato", "ocp"),
    "STM32F103VCT6 MOTOR/SYSTEM CONTROL CPU": ("Mcu", "src/mcu.ato", "mcu"),
    "CAN BUS": ("CanBus", "src/comms.ato", "can"),
    "OIL PUMP LINBUS": ("OilPumpLin", "src/comms.ato", "lin"),
    "Digital Inputs": ("DigitalInputs", "src/io.ato", "inputs"),
    "THROTTLE INPUTS": ("ThrottleInputs", "src/io.ato", "throttle"),
    "CONNECTORS": ("Connectors", "src/connectors.ato", "connectors"),
    "MOUNTING HOLES": ("BoardMechanics", "src/mechanical.ato", "mechanical"),
}
PHASES = {"Gate Driver, Phase A": "gate_a", "Gate Driver, Phase B": "gate_b", "Gate Driver, Phase C": "gate_c"}
PHASE_FILE = "src/gate_drive/phase.ato"

# parts whose box placement is misleading
OVERRIDES = {
    "TP58": "GATE DRIVER INTERFACE",  # test point on /GATE_SD drawn inside the phase C box
    "G***": "MOUNTING HOLES",  # silkscreen logo (not in the schematic)
}

# typed power rails: net -> (port name, reference net, voltage constraint or None)
RAILS = {
    "+3V3": ("power_3v3", "GND", "3.3V +/- 5%"),
    "+3.3VA": ("power_3v3a", "GND", "3.3V +/- 5%"),
    "+5V": ("power_5v", "GND", "5V +/- 5%"),
    "+12V": ("power_12v", "GND", None),  # vehicle 12 V, wide range
    "+12P": ("power_12p", "GND", None),
    "LS_+V": ("power_ls", "LS_GND", None),  # gate-drive primary supply
    "/HV+": ("hv_bus", "HV-", None),  # traction DC bus
}
REF_NETS = {"GND", "LS_GND", "HV-"}


def H(*a) -> str:
    return hashlib.md5(repr(a).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# schematic boxes
# --------------------------------------------------------------------------


def clean_title(t: str) -> str:
    return t.replace("\\n", "\n").strip().split("\n")[0].strip()


def sections_from_schematic(path: Path):
    s = sexp.loads(path.read_text())
    rects = []
    for r in children(s, "rectangle"):
        (x1, y1), (x2, y2) = [tuple(map(float, child(r, k)[1:3])) for k in ("start", "end")]
        rects.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
    titles, notes = [], []
    for t in children(s, "text"):
        at = child(t, "at")
        size = float(child(child(child(t, "effects"), "font"), "size")[1])
        item = (str(t[1]), float(at[1]), float(at[2]))
        (titles if size >= 3.9 else notes).append(item)

    def inside(r, x, y):
        return r[0] <= x <= r[2] and r[1] <= y <= r[3]

    def section(x, y):
        for r in sorted((r for r in rects if inside(r, x, y)), key=lambda r: (r[2] - r[0]) * (r[3] - r[1])):
            ts = [t for t in titles if inside(r, t[1], t[2])]
            if ts:  # one box can hold several titled columns: nearest title wins
                return clean_title(min(ts, key=lambda t: abs(t[1] - x))[0])
        return None

    pos = {}
    for sym in children(s, "symbol"):
        ref = prop(sym, "Reference")
        if ref and not ref.startswith("#"):
            at = child(sym, "at")
            pos.setdefault(ref, (float(at[1]), float(at[2])))
    ref_sec = {r: section(*p) for r, p in pos.items()}
    sec_notes = defaultdict(list)
    for text, x, y in notes:
        if (sec := section(x, y)) is not None:
            sec_notes[sec].append(text.replace("\\n", "\n").strip())
    return ref_sec, sec_notes


# --------------------------------------------------------------------------
# connectivity helpers
# --------------------------------------------------------------------------


@dataclass
class Ctx:
    comps: dict
    nets: dict  # net -> [(ref, pad)]
    inst_type: dict

    def pin(self, ref, pad):
        return self.inst_type[ref]["pins"][pad]


def group_nets(ctx: Ctx, refs: set[str]):
    """net -> (pads inside group, is_external)."""
    out = {}
    for n, pads in ctx.nets.items():
        inside = [(r, p) for r, p in pads if r in refs]
        if inside:
            out[n] = (inside, len(inside) < len(pads))
    return out


def wl_colors(ctx: Ctx, refs: set[str], fixed: dict[str, str]):
    """Weisfeiler-Lehman colour refinement on the part/net graph of a group."""
    gn = group_nets(ctx, refs)
    pad_net = {(r, p): n for n, (pads, _) in gn.items() for r, p in pads}

    def ptype(r):
        c = ctx.comps[r]
        return (c["footprint"], c["symbol"])  # values may differ between phases

    col = {r: H(ptype(r), fixed.get(r)) for r in refs}
    for _ in range(40):
        ncol = {n: H(sorted((col[r], p) for r, p in pads), ext) for n, (pads, ext) in gn.items()}
        new = {
            r: H(col[r], fixed.get(r), sorted((p, ncol.get(pad_net.get((r, p)))) for p in ctx.comps[r]["pads"]))
            for r in refs
        }
        if len(set(new.values())) == len(set(col.values())):
            return new
        col = new
    return col


def match_groups(ctx: Ctx, a: set[str], x: set[str], prefer) -> dict[str, str]:
    """Find a part-to-part isomorphism a -> x (individualise-and-refine)."""
    fa, fx = {}, {}
    k = 0
    while True:
        ca, cx = wl_colors(ctx, a, fa), wl_colors(ctx, x, fx)
        if Counter(ca.values()) != Counter(cx.values()):
            raise SystemExit("phase groups are not structurally identical")
        inv_a, inv_x = defaultdict(list), defaultdict(list)
        for r, c in ca.items():
            inv_a[c].append(r)
        for r, c in cx.items():
            inv_x[c].append(r)
        amb = sorted((c for c in inv_a if len(inv_a[c]) > 1), key=lambda c: sorted(inv_a[c], key=natural)[0])
        if not amb:
            return {inv_a[c][0]: inv_x[c][0] for c in inv_a}
        c = amb[0]
        ra = sorted(inv_a[c], key=natural)[0]
        # among interchangeable candidates prefer the same BOM line, then the
        # designator convention, so that symmetric parts pair up naturally
        same = lambda r: ctx.inst_type[r]["name"] == ctx.inst_type[ra]["name"]  # noqa: E731
        rx = sorted(inv_x[c], key=lambda r: (not same(r), r != prefer(ra), natural(r)))[0]
        k += 1
        fa[ra] = fx[rx] = f"fix{k}"


def phase_letter_free(names: list[str]) -> str | None:
    """'/PWMA_HI','/PWMB_HI','/PWMC_HI' -> '/PWM_HI' (names given in A,B,C order)."""
    a = names[0]
    if len({len(n) for n in names}) != 1:
        return None
    diff = [i for i in range(len(a)) if len({n[i] for n in names}) > 1]
    if len(diff) == 1 and [n[diff[0]] for n in names] == ["A", "B", "C"]:
        return a[: diff[0]] + a[diff[0] + 1 :]
    return None


def net_port_name(net: str) -> str:
    if net.startswith("Net-("):
        return "n_" + ident(net[5:-1], prefix="n")
    return ident(net, prefix="net_")


# --------------------------------------------------------------------------
# module body emission
# --------------------------------------------------------------------------


@dataclass
class ModuleSpec:
    type_name: str
    file: str
    doc: list[str]
    local: dict[str, str]  # ref (of the template instance) -> child name
    refs: set[str]
    ports: dict[str, str] = field(default_factory=dict)  # net -> port name (template namespace)
    power_ports: dict[str, str] = field(default_factory=dict)  # rail net -> port name
    body: list[str] = field(default_factory=list)
    imports: set[str] = field(default_factory=set)
    retypes: dict[str, dict[str, str]] = field(default_factory=dict)  # derived type -> {child: part type}


def build_module(ctx: Ctx, spec: ModuleSpec, net_alias=None):
    """Fill spec.body / ports for a module whose children are spec.refs."""
    net_alias = net_alias or (lambda n: n)
    gn = group_nets(ctx, spec.refs)
    lines = []
    for r in sorted(spec.refs, key=natural):
        t = ctx.inst_type[r]
        spec.imports.add(t["name"])
        lines.append(f"    {spec.local[r]} = new {t['name']}")
    lines.append("")

    used = set(spec.local.values())
    sigs, conns, chains = [], [], []
    for n, (pads, ext) in sorted(gn.items(), key=lambda kv: natural(kv[0])):
        ends = [f"{spec.local[r]}.{ctx.pin(r, p)}" for r, p in sorted(pads, key=lambda rp: natural(rp[0]))]
        named = not n.startswith(("Net-(", "unconnected-"))
        if n.startswith("unconnected-"):
            continue
        if n in RAILS:
            port = RAILS[n][0]
            spec.power_ports[n] = port
            conns.append((f"# {n}", [f"{port}.hv ~ {e}" for e in ends]))
            continue
        if ext or named:
            name = ident(net_alias(n), prefix="net_") if named else net_port_name(n)
            while name in used:
                name += "_net"
            used.add(name)
            if ext:
                spec.ports[n] = name
            sigs.append((name, n, ext))
            conns.append((f"# {n}" + ("" if ext else " (internal)"), [f"{name} ~ {e}" for e in ends]))
        elif len(ends) > 1:
            chains.append((f"# {n}", [f"{ends[0]} ~ {e}" for e in ends[1:]]))

    if spec.power_ports:
        lines.append("    # power ports")
        for n, port in sorted(spec.power_ports.items()):
            lines.append(f"    {port} = new ElectricPower  # {n}")
            ref_net = RAILS[n][1]
            if ref_net in spec.ports:
                lines.append(f"    {port}.lv ~ {spec.ports[ref_net]}")
        lines.append("")
    if sigs:
        lines.append("    # ports (nets that leave this block) and named internal nets")
        for name, n, ext in sigs:
            lines.append(f"    signal {name}" + ("" if ext else "  # internal"))
        lines.append("")
    for header, cl in conns + chains:
        lines.append(f"    {header}")
        lines += [f"    {c}" for c in cl]
    spec.body = lines
    return gn


def write_sources(root: Path, specs: list[ModuleSpec], inst_type: dict):
    import shutil

    if (root / "src").exists():
        shutil.rmtree(root / "src")
    by_file = defaultdict(list)
    for s in specs:
        by_file[s.file].append(s)
    types_by_name = {t["name"]: t for t in inst_type.values()}
    for f, ss in by_file.items():
        imports = sorted(set().union(*(s.imports for s in ss)) | {n for s in ss for d in s.retypes.values() for n in d.values()})
        out = [
            '"""GENERATED by tools/gen_modules.py from the KiCad V3.2 schematic boxes."""',
            "",
            '#pragma experiment("TRAITS")',
            '#pragma experiment("INSTANCE_TRAITS")',
            "import ElectricPower",
            "import has_designator",
        ]
        for name in imports:
            t = types_by_name[name]
            out.append(f'from "parts/{t["lib"]}/{name}.ato" import {name}')
        for s in ss:
            out += ["", "", f"module {s.type_name}:"]
            out.append('    """' + "\n    ".join(s.doc).replace('"""', "'''") + '\n    """')
            out.append("")
            out += s.body
            for derived, rt in s.retypes.items():
                out += ["", "", f"module {derived} from {s.type_name}:"]
                out.append(f'    """{s.type_name} with the parts that differ on this phase in the V3.2 BOM."""')
                for child_name, tname in sorted(rt.items()):
                    out.append(f"    {child_name} -> {tname}")
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(out) + "\n")


# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ATO)
    args = ap.parse_args()
    root = args.root

    ref, comps = load(root)
    inst_type, _ = write_parts(root, comps, ref["symbols"])
    nets = {n: [tuple(p.split(".", 1)) for p in pads if p.split(".", 1)[0] in comps] for n, pads in ref["nets"].items()}
    ctx = Ctx(comps, nets, inst_type)

    ref_sec, sec_notes = sections_from_schematic(SCH)
    sec_of = {r: OVERRIDES.get(r) or ref_sec.get(r) for r in comps}
    missing = [r for r, s in sec_of.items() if s not in SECTIONS and s not in PHASES]
    if missing:
        raise SystemExit(f"parts outside any known schematic box: {missing}")

    addresses: dict[str, str] = {}
    specs: list[ModuleSpec] = []
    top_inst: list[tuple[str, ModuleSpec, dict]] = []  # (instance, spec, template-net -> actual-net)

    # ---- single-instance blocks -----------------------------------------
    for title, (tname, f, inst) in SECTIONS.items():
        refs = {r for r, s in sec_of.items() if s == title}
        notes = sec_notes.get(title, [])
        doc = [f"Schematic block: {title}", f"{len(refs)} parts: {', '.join(sorted(refs, key=natural))}"]
        if notes:
            doc += ["", "Design notes from the schematic:"] + [f"- {n}".replace("\n", " ") for n in notes]
        spec = ModuleSpec(tname, f, doc, {r: inst_name(r) for r in refs}, refs)
        build_module(ctx, spec)
        specs.append(spec)
        top_inst.append((inst, spec, {n: n for n in spec.ports} | {n: n for n in spec.power_ports}))
        for r in refs:
            addresses[r] = f"{inst}.{spec.local[r]}"

    # ---- gate-driver phases: one module, three instances ------------------
    groups = {inst: {r for r, s in sec_of.items() if s == title} for title, inst in PHASES.items()}
    ga = groups["gate_a"]
    digit = {"gate_b": "3", "gate_c": "2"}  # designator convention: last digit 1/3/2 = phase A/B/C
    maps = {"gate_a": {r: r for r in ga}}
    for inst in ("gate_b", "gate_c"):
        maps[inst] = match_groups(ctx, ga, groups[inst], prefer=lambda r, d=digit[inst]: r[:-1] + d)

    # verify the isomorphism at pad level and build net correspondence
    net_map = {inst: {} for inst in maps}
    for inst, m in maps.items():
        for r in ga:
            for p, n in comps[r]["pads"].items():
                nx = comps[m[r]]["pads"].get(p)
                if net_map[inst].setdefault(n, nx) != nx:
                    raise SystemExit(f"{inst}: {r}.{p} maps inconsistently")
        if len(set(net_map[inst].values())) != len(net_map[inst]):
            raise SystemExit(f"{inst}: net mapping is not one-to-one")

    def role(r):
        mm = re.fullmatch(r"([A-Z]+\d+)1", r)
        if mm and maps["gate_b"][r] == mm.group(1) + "3" and maps["gate_c"][r] == mm.group(1) + "2":
            return mm.group(1).lower() + "x"
        return r.lower()

    roles = {r: role(r) for r in ga}
    assert len(set(roles.values())) == len(roles)

    def alias(n):
        """Phase-independent net name: shared nets keep their name, per-phase
        nets lose the phase letter ('_x' when it was a suffix, like child names)."""
        names = [n, net_map["gate_b"][n], net_map["gate_c"][n]]
        if len(set(names)) == 1:
            return n
        if (lf := phase_letter_free(names)) is not None and not n.endswith("_A"):
            return lf
        return re.sub(r"_A$", "_x", n)

    pnotes = []
    for t in PHASES:
        pnotes += [x for x in sec_notes.get(t, []) if x not in pnotes]
    doc = [
        "One phase of the gate driver: high- and low-side STGAP1AS isolated drivers with",
        "their isolated supplies, gate resistors, Miller clamp / DESAT network and the",
        "power-module interface. Instantiated three times (gate_a/b/c).",
        "",
        "Child names follow the phase A designators; an 'x' replaces the phase digit",
        "(e.g. r400x = R4001 / R4003 / R4002 on phases A / B / C).",
    ]
    if pnotes:
        doc += ["", "Design notes from the schematic:"] + [f"- {n}".replace("\n", " ") for n in pnotes]
    phase = ModuleSpec("GateDrivePhase", PHASE_FILE, doc, roles, set(ga))
    build_module(ctx, phase, net_alias=alias)
    specs.append(phase)

    for inst, m in maps.items():
        diffs = {roles[r]: inst_type[m[r]]["name"] for r in ga if inst_type[m[r]]["name"] != inst_type[r]["name"]}
        tname = "GateDrivePhase"
        if diffs:
            tname = f"GateDrivePhase{inst[-1].upper()}"
            phase.retypes[tname] = diffs
        nm = net_map[inst]
        tmpl = {n: nm[n] for n in list(phase.ports) + list(phase.power_ports)}
        top_inst.append((inst, ModuleSpec(tname, PHASE_FILE, [], {}, set(), dict(phase.ports), dict(phase.power_ports)), tmpl))
        for r in ga:
            addresses[m[r]] = f"{inst}.{roles[r]}"

    assert set(addresses) == set(comps), set(comps) ^ set(addresses)
    write_sources(root, specs, inst_type)

    # ---- top level --------------------------------------------------------
    out = [
        '"""Tesla Model 3 drive unit logic board (M3DU V3.2).',
        "",
        "Top level: one module per block of the original schematic, wired together",
        "by the nets that cross between blocks. GENERATED by tools/gen_modules.py;",
        "the reference designators and layout are those of the KiCad V3.2 board.",
        '"""',
        "",
        '#pragma experiment("TRAITS")',
        '#pragma experiment("INSTANCE_TRAITS")',
        "import ElectricPower",
        "import has_designator",
    ]
    imported = defaultdict(set)
    for _, s, _ in top_inst:
        imported[s.file].add(s.type_name)
    for f, names in sorted(imported.items()):
        out += [f'from "{f}" import {n}' for n in sorted(names)]
    out += ["", "", "module M3DU:", '    """M3DU logic board, V3.2 (RDU population)."""', "", "    # ---- blocks ----"]
    for inst, s, _ in top_inst:
        out.append(f"    {inst} = new {s.type_name}")

    # rails
    rails_used = sorted({actual for _, s, tm in top_inst for n in s.power_ports for actual in [tm[n]]})
    top_names = {inst for inst, _, _ in top_inst}
    out += ["", "    # ---- power rails ----"]
    ref_sig = {}
    for rnet in sorted(REF_NETS):
        ref_sig[rnet] = ident(rnet, prefix="net_")
    for rnet in sorted(REF_NETS):
        out.append(f"    signal {ref_sig[rnet]}  # {rnet}")
        top_names.add(ref_sig[rnet])
    for rail in rails_used:
        port, refnet, volt = RAILS[rail]
        out.append(f"    {port} = new ElectricPower  # {rail}")
        out.append(f"    {port}.lv ~ {ref_sig[refnet]}")
        if volt:
            out.append(f"    {port}.voltage = {volt}")
        for inst, s, tm in top_inst:
            for n, pp in s.power_ports.items():
                if tm[n] == rail:
                    out.append(f"    {inst}.{pp} ~ {port}")
        top_names.add(port)

    # nets between blocks
    out += ["", "    # ---- nets between blocks ----"]
    by_net = defaultdict(list)
    for inst, s, tm in top_inst:
        for n, port in s.ports.items():
            by_net[tm[n]].append(f"{inst}.{port}")
    n_named = 0
    for net, ends in sorted(by_net.items(), key=lambda kv: natural(kv[0])):
        named = not net.startswith("Net-(")
        out.append(f"    # {net}")
        if net in REF_NETS:
            out += [f"    {e} ~ {ref_sig[net]}" for e in ends]
        elif named:
            nid = ident(net, prefix="net_")
            while nid in top_names:
                nid += "_net"
            top_names.add(nid)
            out.append(f"    signal {nid}")
            out += [f"    {e} ~ {nid}" for e in ends]
            n_named += 1
        else:
            out += [f"    {ends[0]} ~ {e}" for e in ends[1:]]

    # designators
    out += ["", "    # ---- designators (identical to the KiCad design) ----"]
    for r in sorted(comps, key=natural):
        if addresses[r].split(".")[0] in ("gate_a", "gate_b", "gate_c"):
            out.append(f'    trait {addresses[r]} has_designator<designator="{r}">')
    (root / "main.ato").write_text("\n".join(out) + "\n")

    # designators of single-instance blocks live inside their module
    for spec in specs:
        if spec.type_name == "GateDrivePhase":
            continue
        p = root / spec.file
        txt = p.read_text()
        marker = f"module {spec.type_name}:\n"
        i = txt.index(marker)
        j = txt.find("\n\n\nmodule ", i + len(marker))
        j = len(txt) if j < 0 else j
        des = ["", "    # designators (identical to the KiCad design)"]
        des += [f'    trait {spec.local[r]} has_designator<designator="{r}">' for r in sorted(spec.refs, key=natural)]
        txt = txt[:j].rstrip("\n") + "\n" + "\n".join(des) + "\n" + txt[j:]
        p.write_text(txt)

    (root / "reference" / "addresses.json").write_text(json.dumps(dict(sorted(addresses.items(), key=lambda kv: natural(kv[0]))), indent=1) + "\n")

    print(f"modules    : {len(specs)} types, {len(top_inst)} instances")
    print(f"phases     : 3x GateDrivePhase ({len(ga)} parts each); per-phase retypes: "
          f"{ {k: v for k, v in phase.retypes.items()} }")
    print(f"ports      : phase {len(phase.ports)} signals + {len(phase.power_ports)} power")
    print(f"top nets   : {len(by_net)} between blocks ({n_named} named), rails {rails_used}")


if __name__ == "__main__":
    main()
