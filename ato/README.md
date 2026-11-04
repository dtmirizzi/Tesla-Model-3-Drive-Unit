# M3DU logic board in atopile

An [atopile](https://atopile.io) port of the V3.2 logic board in
`../Design/M3DU_BoardV3_2`. Right now it is an **exact, flat port**: the same
798 parts, designators, nets and hand-routed layout as the KiCad design, checked
pad by pad. See [PLAN.md](PLAN.md) for the roadmap to a modular design.

## Layout

```
ato.yaml                 atopile project (requires atopile ^0.15.9)
main.ato                 module M3DU: every part + every net (generated)
parts/<KiCadLib>/        one atopile component per BOM line + its footprint/symbol (generated)
layouts/default/         the KiCad board atopile builds into (seeded from the original PCB)
lib/                     footprint + symbol libraries recovered from the KiCad design
reference/netlist.json   reference netlist extracted from the original PCB
tools/                   extraction, code generation, layout seeding and the equivalence check
```

## Build

```bash
uv venv -p 3.14 .venv && . .venv/bin/activate
uv pip install atopile==0.15.9 pandas xlrd

tools/port.sh            # extract -> generate -> seed layout -> ato build -> check
# or, once seeded:
ato build --keep-net-names --no-standardize-designators
python tools/check_netlist.py
```

`check_netlist.py` exits non-zero unless the built board matches the original:
the same references and pad geometry, the same pad-to-net partition, and every
footprint, track, via and zone in place.

Open `layouts/default/default.kicad_pcb` in KiCad 9 to view or edit the board.
After editing the layout, run `tools/port.sh --no-seed` so your edits are kept.
