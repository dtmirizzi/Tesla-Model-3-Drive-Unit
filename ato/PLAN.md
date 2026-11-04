# Porting the M3DU logic board to atopile

Goal: describe the Tesla Model 3 drive-unit logic board (`Design/M3DU_BoardV3_2`)
as code in [atopile](https://atopile.io), without losing the proven, hand-routed
layout. The board is a drop-in replacement for the OEM board, so the outline,
mounting holes, the OEM 30-pin connector, the HV pads, the gate pads and the HV
creepage and clearance all have to stay exactly where they are. atopile does not
place or route, so **the circuit moves into code and the layout is carried over.**

Every step is checked by `tools/check_netlist.py`. It compares the original
KiCad PCB with the atopile-built PCB pad by pad, and checks that every
footprint, track, via and zone is still in place.

## Source design (V3.2)

| | |
|---|---|
| Schematic | one flat A0 sheet, ~960 symbols, 117 named nets |
| PCB | 4 layers, 798 footprints, 453 nets, ~5.4k segments, 1.5k vias, 92 zones |
| Key parts | STM32F103VCT6, 6x STGAP1AS, TPS40211 + VGT isolated gate supply, 2x MLX91209, ACPL-C87A, TDA2822/OPA320 resolver amp, LM2901 OCP, SN65HVD230 CAN, TJA1021 LIN, XL1509/LD1086/TLE4264/NCV8452 LV supply |
| Variants | RDU and FDU ("960 board") in one schematic, as text notes |

## Toolchain notes (learned in Phase 0)

* atopile **0.12 is retired** (the CLI refuses to run). **0.15.9** is the last
  PyPI release and needs **Python 3.14**. 0.16+ is app-only (app.atopile.io).
  This port pins `requires-atopile: ^0.15.9`.
* Builds work fully offline when every part carries `is_atomic_part` (and
  `has_part_picked::by_supplier` for the LCSC number). The generic picker
  (`Resistor`, `Capacitor`, ...) needs the atopile parts API.
* An atomic part's footprint and symbol must sit in the **same directory** as
  its `.ato` file. The directory name becomes the KiCad library nickname, which
  is why `parts/` keeps the original library names (`Tesla`, `Resistor_SMD`, ...).
* Designators are only assigned automatically to "picked" parts. Exact
  references (R339, U16, ...) are pinned with
  `trait <inst> has_designator<designator="...">` (`INSTANCE_TRAITS` pragma).
* atopile matches existing PCB footprints by the `atopile_address` property and
  updates them in place, keeping position, rotation, tracks and zones. That
  property is what makes it possible to carry the layout over.
* atopile 0.15.9 crashes on footprint properties that have no `(at ...)`, such
  as `ki_fp_filters`. `seed_layout.py` strips them; they are only schematic hints.
* Footprints with no atopile counterpart are deleted from the layout, so even
  the silkscreen logo is modelled as a pad-less part.

## Phases

### Phase 0 - toolchain trial ✅
Pinned atopile 0.15.9 and proved custom footprints, fixed designators, LCSC
BOM output and reuse of an existing layout on a small project.

### Phase 1 - reference netlist + libraries ✅
`tools/extract_kicad.py`:
* `reference/netlist.json`: pad-to-net map for all 798 footprints, plus value,
  symbol, MPN, LCSC (from the JLC BOM) and DNP flag for each part.
* `lib/footprints/*.pretty`: 85 footprints recovered from the board, including
  the `Tesla` library that was never committed. Rotation and side are undone,
  and pad-geometry variants are kept apart (`CP_EIA-3528-15_AVX-H__v2`).
* `lib/symbols/*`: 68 symbols recovered from the schematic's embedded library.

### Phase 2 + 3 - exact flat port ✅
`tools/gen_ato.py` writes one atopile component per BOM line (183 types under
`parts/`) and `main.ato` (`module M3DU`: 798 instances with their original
designators, 117 named nets and 1,647 connections).

### Phase 4 - carry the layout over ✅
`tools/seed_layout.py` copies the original PCB into `layouts/default/` and tags
every footprint with its `atopile_address`. `ato build` then binds to it.
`check_netlist.py` passes: same references, same pad geometry, same net
partition (0 nets renamed), every footprint in place, all 6,913
tracks/arcs/vias and 92 zones intact.

### Phase 5 - refactor into modules (next)
Replace the flat `main.ato` one block at a time with hand-written modules,
re-running `check_netlist.py` after each:

```
src/
  power/lv_supply.ato   # 12V in, protection, XL1509 5V, LD1086 3V3, TLE4264, NCV8452
  power/gate_psu.ato    # TPS40211 + VGT isolated supplies
  gate_drive/phase.ato  # HS+LS STGAP1AS + MOSFET/clamp network, instantiated 3x
  sensing/current.ato   # MLX91209 x2
  sensing/dc_bus.ato    # ACPL-C87A, HV divider, discharge, HV ref 5V
  sensing/temps.ato     # heatsink / motor NTCs
  resolver.ato          # exciter amp + SIN/COS conditioning
  protection/ocp.ato    # LM2901 comparators, 74LVC3G17 / 74LVC1G10 fault logic
  mcu.ato               # STM32F103, crystal, SWD, decoupling
  comms.ato             # CAN + ESD, LIN oil pump
  io.ato                # throttle, digital inputs, outputs
  connectors.ato        # OEM 30-pin, HVIL, board detect
```
* Replace global labels with typed interfaces (`ElectricPower`, `SPI`, `CAN_TTL`,
  `SWD`, `ElectricLogic`), so a mis-wired rail fails to compile.
* Express design intent as constraints and assertions: divider ratios, current
  sensor scaling, regulator feedback, voltage ratings in the HV divider, and the
  power rating R339 needs.
* Make RDU/FDU a variant parameter: population and the 10R -> 6R2 gate resistors
  instead of text notes.
* Keep the original designators (`has_designator`) and `atopile_address`
  values, or re-tag the layout with a small mapping, so the layout stays bound.
  The gate-drive phase is the first candidate: three identical copies, and it is
  where a sub-layout pays off most.

### Phase 6 - CI + fab outputs
GitHub Action: install atopile 0.15.9 on Python 3.14, `ato build --frozen`,
`check_netlist.py`, KiCad 9 DRC/ERC, and JLC BOM/CPL/Gerber export as artifacts.

## Findings in the original design (kept unchanged by the exact port)

1. **BOM package mismatches.** The JLC BOM assigns some LCSC parts to footprints
   of a different size:
   * C17414 (10K, BOM says 0805) on **0603** pads: R1, R133, R136, R235, R237
   * C26010 (3K3, BOM says 0805) on **0603** pad: R2
   * C17798 (680R, 0603) on **0805** pad: R19
   * C22967 (27K, 0603) on **0805** pad: R9
2. **`HV DISCH EN`** is a global label on MCU pin 43 (U16.43) that appears only
   once, so the pin drives nothing. This is probably deliberate, since active
   discharge is now disabled through connector pin 23, but it is worth confirming.
3. Two footprint libraries are named `Samacsys` and `Samascys` (typo). They are
   kept as-is so the footprint IDs match.
