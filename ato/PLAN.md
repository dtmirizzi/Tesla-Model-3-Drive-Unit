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
`parts/`). The first port was a single flat `main.ato` (`module M3DU`: 798
instances, 117 named nets, 1,647 connections); it can still be generated with
`FLAT=1 tools/port.sh`.

### Phase 4 - carry the layout over ✅
`tools/seed_layout.py` copies the original PCB into `layouts/default/` and tags
every footprint with its `atopile_address`. `ato build` then binds to it.
`check_netlist.py` passes: same references, same pad geometry, same net
partition (0 nets renamed), every footprint in place, all 6,913
tracks/arcs/vias and 92 zones intact.

### Phase 5 - modules ✅
`tools/gen_modules.py` turns the titled boxes of the A0 schematic into modules
(`src/`). Every part goes to the box it is drawn in. Nets that stay in a box stay
private to it; nets that leave it become ports.

```
src/
  power/lv_supply.ato       LvSupply            12V in, protection, XL1509 5V, LD1086 3V3, TLE4264, NCV8452
  power/hv_ref_supply.ato   HvRefSupply         HV-referenced +5V supply
  power/gate_psu.ato        GatePsu             TPS40211 + VGT isolated gate-drive supplies
  gate_drive/phase.ato      GateDrivePhase x3   HS+LS STGAP1AS, gate network, DESAT/clamp, module pads
  gate_drive/interface.ato  GateInterface       gate SPI / shutdown fan-out
  sensing/dc_bus.ato        DcBus               ACPL-C87A, HV divider, active discharge
  sensing/current.ato       CurrentSensors      MLX91209 x2
  sensing/temps.ato         HeatsinkTemps, MotorTemp
  resolver.ato              Resolver            exciter amp + SIN/COS conditioning
  protection/ocp.ato        OvercurrentDetect   LM2901 comparators + fault logic
  mcu.ato                   Mcu                 STM32F103VCT6, crystal, SWD
  comms.ato                 CanBus, OilPumpLin
  io.ato                    DigitalInputs, ThrottleInputs
  connectors.ato            Connectors          OEM 30-pin, HVIL, board detect
  mechanical.ato            BoardMechanics      mounting holes, logo
```

* **One gate-driver phase, three instances.** Graph matching on part type and
  pin-level connectivity proves phases A, B and C are the same circuit (126 parts
  each), so `GateDrivePhase` is written once. Child names follow phase A, with
  `x` replacing the phase digit (`r400x` = R4001/R4003/R4002 on phases A/B/C).
  The only per-phase BOM difference is TR6's value text, handled by a derived
  `GateDrivePhaseC` that retypes that one part.
* **Typed power rails.** +3V3, +3.3VA, +5V, +12V and +12P (to GND), LS_+V (to
  LS_GND) and /HV+ (to HV-) are `ElectricPower` ports, and the known rail
  voltages are declared once at the top level.
* Each module's docstring carries the design notes drawn in its schematic box.
* Designators are unchanged. `reference/addresses.json` maps every designator to
  its new atopile address, and `seed_layout.py` tags the layout with it.
  `check_netlist.py` still passes, and the atopile BOM matches the original JLC
  BOM line for line (703 parts, same LCSC numbers).

### Phase 6 - CI ✅ (written, not yet run on GitHub)
`.github/workflows/atopile.yml`:
* regenerates everything from the KiCad sources, builds, and runs `check_netlist.py`
* fails if the committed generated files are stale (generation is deterministic)
* in a `kicad/kicad:9.0` container: DRC (report only for now) plus Gerbers, drill
  and position files as build artifacts

## What's next
* **Make the modules the source.** Today `src/` is regenerated from KiCad. The
  next step is to edit it by hand and stop regenerating, leaving
  `check_netlist.py` as the guard. To switch: drop `gen_modules.py` from
  `port.sh`, and change CI to `ato build` + `check_netlist.py`.
* Typed buses: replace the gate-driver SPI, CAN and LIN signals with `SPI`,
  `CAN_TTL` and `UART` interfaces.
* Design intent as constraints: divider ratios for the HV measurement, current
  sensor scaling, regulator feedback, and the power rating R339 needs.
* RDU/FDU variant. The FDU ("960 board") population is only described in
  schematic notes (for example "10R RESISTORS ARE 6R2 ON 960 BOARD"), not as
  data, so it needs a reviewed list of affected parts first. Once that exists it
  becomes a derived module per block, like `GateDrivePhaseC`.
* Gate the DRC in CI once the original board's DRC report has been reviewed.

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
4. TR6 (phase C power-module pads) has the value "CH1234" where TR1-TR5 say
   "M3DU_MOSFETS". It is cosmetic: same footprint, not in the BOM.
5. Phases A/B/C are identical, but the designators of two parallel capacitors
   are swapped in phase C (C2582 is the 2.2 uF part there, C2581/C2583 are 1 uF
   on A/B). This is harmless, but it is easy to misread when comparing phases.
