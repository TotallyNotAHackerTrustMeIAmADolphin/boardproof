# Evaluation suite

`kicad_mcp_eval.xml` holds 15 independent, read-only Q&A pairs. Each tests whether an agent with only this project's tools can answer a realistic question about the fixtures in `tests/fixtures/`. The format follows the `mcp-builder` evaluation guide. The suite tests tool usability, not board quality; the acceptance gate (docs/TESTING.md) handles that.

- **Q1-Q10:** single-tool lookups (DRC/ERC on `simple_led`, footprint counts, symbol values, the parametric footprint generator, datasheet extraction).
- **Q11-Q15:** multi-step chains:
  - Q11: collision check, then placement check.
  - Q12: DRC, then the unrouted-net query ("0 violations" does not mean fab-ready).
  - Q13: ERC, then SPICE export, then reading the netlist.
  - Q14: datasheet, then footprint generation, then minimal KLC.
  - Q15: open the project, run the manufacturing export, read the BOM and CPL.

## Rules for answers

- Every answer must be derived by **running the tool against the real fixture**, never by reasoning it out.
- **Pin answers to facts the fixture file determines,** not to facts the toolchain determines. `lib_footprint_mismatch` counts follow whichever KiCad stock libraries are installed, and they have already drifted once (1 became 3). Values set by `kicad_tools` internals are stated as properties ("at least 7 package types") and not as exact values.
- **No question depends on a live sourcing provider.**
- **No question mutates a fixture.** Exports go to temporary directories.

## Running

Use the `mcp-builder` evaluation script (`~/.claude/skills/mcp-builder/scripts/evaluation.py`) against this server. Or run it by hand: give an agent the tools and the questions, then diff its answers against the `<answer>` values.
