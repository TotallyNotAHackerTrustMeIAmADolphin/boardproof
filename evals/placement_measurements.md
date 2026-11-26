# place_simple measurements (issue 259)

Measurements of `propose_placement_optimization` then `confirm_placement_optimization` (KiCadRoutingTools `place_simple`, pinned version, via the tools' own code path) on fresh copies of three fixture boards, seeds 0 to 5, one run per (board, seed), 2026-11-26. Reproduce with `python evals/measure_placement.py <work_dir> [board ...]` (KICAD_ROUTING_TOOLS_PATH set; the script exits with the reason when place_simple is unavailable). DRC counts are `kicad-cli pcb drc` violations / errors / unconnected items, taken by the confirm before and after writing the poses. Times are wall seconds for the placer run and for the whole propose call. Per-run kill timeout 180 s; none was reached.

Boards: `tests/fixtures/golden_attiny1616_board`, `tests/fixtures/simple_led.kicad_pcb`, `tests/fixtures/krt_stress_congestion.kicad_pcb`. The frozen openMPPT board was not touched.

| board | seed | status | footprints moved | DRC before (violations/errors/unconnected) | DRC after | placer s | propose s |
|---|---|---|---|---|---|---|---|
| golden_attiny1616_board | 0 | confirmed | 6 | 0/0/11 | 10/0/11 | 3.1 | 3.2 |
| golden_attiny1616_board | 1 | confirmed | 6 | 0/0/11 | 5/0/11 | 3.1 | 3.2 |
| golden_attiny1616_board | 2 | confirmed | 6 | 0/0/11 | 6/0/11 | 3.0 | 3.2 |
| golden_attiny1616_board | 3 | confirmed | 6 | 0/0/11 | 6/0/11 | 3.1 | 3.2 |
| golden_attiny1616_board | 4 | confirmed | 6 | 0/0/11 | 5/0/11 | 3.7 | 3.8 |
| golden_attiny1616_board | 5 | confirmed | 6 | 0/0/11 | 16/0/11 | 3.5 | 3.7 |
| simple_led | 0 | confirmed | 3 | 3/0/3 | 3/0/3 | 4.3 | 4.4 |
| simple_led | 1 | confirmed | 3 | 3/0/3 | 3/0/3 | 4.7 | 4.8 |
| simple_led | 2 | confirmed | 3 | 3/0/3 | 3/0/3 | 4.7 | 4.8 |
| simple_led | 3 | confirmed | 3 | 3/0/3 | 3/0/3 | 3.7 | 3.8 |
| simple_led | 4 | confirmed | 3 | 3/0/3 | 3/0/3 | 3.8 | 3.9 |
| simple_led | 5 | confirmed | 3 | 3/0/3 | 3/0/3 | 4.3 | 4.4 |
| krt_stress_congestion | 0 | confirmed | 5 | 3/0/15 | 9/0/15 | 5.3 | 5.5 |
| krt_stress_congestion | 1 | confirmed | 5 | 3/0/15 | 0/0/15 | 4.4 | 4.6 |
| krt_stress_congestion | 2 | confirmed | 5 | 3/0/15 | 4/0/15 | 4.3 | 4.5 |
| krt_stress_congestion | 3 | confirmed | 5 | 3/0/15 | 8/0/15 | 4.0 | 4.1 |
| krt_stress_congestion | 4 | confirmed | 5 | 3/0/15 | 7/0/15 | 4.0 | 4.2 |
| krt_stress_congestion | 5 | confirmed | 5 | 3/0/15 | 4/0/15 | 4.0 | 4.2 |

## What was and was not measured

Measured: that all 18 runs proposed, passed the tool's own courtyard check and confirmed (no refusal, failure or timeout); the number of footprints moved; kicad-cli violation, error and unconnected counts of the copy before and after the pose write; wall times on one machine, one run each, run once with no re-run (seeds 0 to 3 on the attiny board agree with the review of issue 257).

Not measured, and not claimed: routing (the boards were unrouted or fixture-routed; the unconnected counts did not change, as no tracks were touched); any `verify_board` verdict (nothing here is routed or done); placement quality (violation counts are DRC counts only, not a quality score); seeds beyond 5; repeatability of the times; other machines. Violation counts rise or fall with the seed (attiny 5 to 16, stress 0 to 9 after, simple_led unchanged); no error-level violation occurred in any run. The counts are measurements of these boards under these seeds, not a score of the tool.
