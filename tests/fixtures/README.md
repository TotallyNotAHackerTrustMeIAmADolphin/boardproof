# Test fixtures

Every purpose-built fixture is generated through `kicad_tools`' own object model (`PCB.create`, footprints, nets, `SExp`) and saved through the write layer (`write_layer.save_pcb`, which parse-checks it with `kicad-cli`; build ticket 42), or filled by `kicad-cli`. None is hand-typed (CLAUDE.md rule 1).

| Fixture | Origin | Purpose |
|---|---|---|
| `simple_led.*` | Copied from rjwalters/kicad-tools `00-simple-led` (MIT) | Small, valid real KiCad files for `kicad-cli` and `kicad_tools` calls. KiCad 7-style refs, which is why it missed the KiCad 8+ DSN reference bug. |
| `golden_attiny1616_board/` | Built by the pipeline (v1, 2026-09-02) | Pipeline regression fixture. **Unrouted. It has a generic power header instead of USB-C, and its 0.2/0.2 rules cannot be routed at the VQFN's 0.4 mm pitch.** Not a rung-1 candidate (TESTING.md). |
| `graphics_removal_board.kicad_pcb` | Purpose-built | `propose_remove_graphic_items` tests: six graphics across `gr_rect`, `gr_poly`, `gr_line`, `gr_circle` and `gr_arc`, net-tagged copper plus silkscreen. |
| `zone_obstacle/` | `simple_led` plus a real GND pour filled by `kicad-cli --refill-zones` | Reproduces "filled zone is not an obstacle for a foreign net": KRT routes LED_ANODE at 0.0 mm clearance to the GND zone. |
| `krt_stress_congestion.*` | Purpose-built, 30x36 mm, 2-layer, with a real sibling `.kicad_pro` | KRT placement and blocking-analysis tests. An LQFP-48 with 5 signal nets crosses an 8-pin THT header wall, modelled on openMPPT's GPIO1 bottleneck. KRT routes 4 of 5 nets in about 4 s. `SIG5` fails as `boxed_in_static`, but `check_reachability.py` says the net is PASSABLE, so this is a search miss, not real congestion. |
| `krt_pad_geometry_isolated.*` | Purpose-built | Isolates whether pad size alone blocks a route. With 2.2 mm header pads (0.34 mm gap), `SIG1` fails after more than 227k A\* iterations. Shrunk to the JLCPCB floor of 1.36 mm (1.0 drill + 2 x 0.18 annular), it routes as one direct trace. The shrink must mutate the `(size)` node, because `pad.size = ...` is not saved. |
| `openmppt_v1_3.kicad_dru` | Copied from the user's openMPPT board | A real custom-rules file for `board_rules` matching (`hole_to_hole` vs `hole_clearance`). |
| `datasheets/` | TI LM358, onsemi NCV7691, Nexperia PMEG2005EJ | `inspect_datasheet` tests. |
| `uw/r2krt`, `uw/r1krt`, `uw/r1fr` | Taken byte-for-byte from the committed run directories of `paradigm-review-2` ticket 04 (2026-10-19, KRT rung 2, KRT rung 1, Freerouting rung 1): `in/` holds the batch's frozen `.kicad_pcb` and `.kicad_pro`, `out/` the board attempt 00 delivered; `attempt0.json` holds that attempt's recorded router facts with machine paths replaced by `<path>` (by `scripts/export_ladder_results.py`' sanitiser) | `tests/test_krt_under_width_delivery.py`, `tests/test_rung_backends_under_width.py`: the under-width replays. These were read from `.scratch` before the run directories left the repository. |
