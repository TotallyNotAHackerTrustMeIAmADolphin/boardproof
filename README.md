# boardproof

An MCP server for PCB design in which nothing counts as finished unless a machine-checked gate says so. The Python package is `kicad_mcp` and the start command is `kicad-mcp`.


An MCP server that lets an AI agent do PCB design work in KiCad: component sourcing, schematic construction, placement, routing (KiCadRoutingTools or Freerouting), verification, SPICE simulation and manufacturing export. It drives KiCad through its files and `kicad-cli`, so no running KiCad is needed.

Who it is for: people who want an MCP client (Claude Code, Claude Desktop or another) to carry out real KiCad 10 work and who care more about a result they can trust than about a result that looks finished. Everything is built around one rule: a board is "done" only when a machine-checked verdict says so. A short tour of the design is in [docs/OVERVIEW.md](docs/OVERVIEW.md).

## Status (2026-10-29)

**No board has passed the acceptance gate as a plain PASS yet.** The tools exist and are tested, but no tool chain has delivered a routed board end to end that the gate accepts without a qualifier. The product promise is therefore tiered by board class:

- **Copilot tier (default):** you own power routing and zone topology in KiCad. The agent handles schematic, placement, signal routing and verification.
- **Autonomous tier:** offered only for board classes that have passed the gate on the acceptance ladder. **Currently none.**

Words used below: a **rung** is one reference board on the acceptance ladder (an ordered set of boards a pipeline must pass, easiest first); **KRT** is KiCadRoutingTools, a third-party router this project runs as a subprocess; the **owner** is the person who decides the scope and rules of this project; a **build ticket N** is an internal work item id and may not be resolvable in a public copy (the fact is always stated in words next to it); a **protocol** is the frozen, numbered plan by which a rung is measured. [docs/CONTEXT.md](docs/CONTEXT.md) has the full glossary.

What the measurements say (five attempts per backend and rung, official batches, 2026-10-28):

| Rung | Board | Protocol | Freerouting 2.5.0 | KiCadRoutingTools 0.23.1 |
|---|---|---|---|---|
| 1 | USB-C ATtiny1616 breakout, 2 layers | 6 | 5 of 5 attempts `PASS (necked)` | 0 of 5 |
| 2 | KiCad's `pic_programmer` demo, tracks removed | 5 (no protocol-6 batch has run) | 0 of 5; cannot pass | 0 of 5; cannot pass |

- **`PASS (necked)`** means the board passed the gate (zero unconnected items, zero error-level DRC violations, all zones filled), but it carries counted necks: tracks narrower than their net class right at a pad entry, which the protocol accepts as "forced" when the pad geometry limits the width (at the 0.2 mm gap between U1's pads, a 0.2 mm track is the widest that keeps the class clearance). The Freerouting boards carry 3 such necks on `+3V3` at U1 pad 4, 2.1476 mm in total. It is a weaker statement than a plain PASS, it is always labelled, and no rung is claimed from it.
- **The 5 of 5 depends on one open decision.** The first segment of each neck is 0.15 mm wide where the pad would allow 0.2 mm. Under the protocol's current reading that still counts as forced; under a stricter reading all five attempts would FAIL. The owner's decision on a neck below the pad limit is still open (build ticket 111, item 3).
- KiCadRoutingTools scores 0 of 5 on rung 1 because its parser reads zero pads of footprints whose pad names are unquoted, so it leaves their pads unconnected (build ticket 112, not fixed).
- Rung 2 cannot pass for any router: the demo's own silkscreen already carries 6 `silk_overlap` errors, which no router can fix (build ticket 106).
- Rung 3, the 20 A class openMPPT board, is frozen until rungs 1 and 2 pass.

Details and the exact meaning of each number: [docs/ROADMAP.md](docs/ROADMAP.md) and [docs/TESTING.md](docs/TESTING.md).

## Quickstart

1. Install (below) and add the server to your MCP client (below).
2. Ask the agent to open a project: `project_open` takes a project directory or a `.kicad_pro` file. `project_create` scaffolds a new one.
3. Load what you need: `list_toolsets` shows the toolsets, `load_toolset` loads one (`schematic`, `placement`, `routing`, ...).
4. Grade the board: `pin_rule_baseline` before you start work, then `verify_board` with the board path, `parity="required"` (or `"not_applicable"` with a reason) and the baseline. Only a `PASS` from `verify_board` may be called routed or done.

## Install

Requirements:

- Python 3.11 or newer.
- KiCad 10 with `kicad-cli` (required). Tested with 10.0.7.
- Tested on Windows only (Windows 11, KiCad 10.0.7): every measurement and every gate run was made there. The code does not knowingly depend on Windows, but nothing has been run on Linux or macOS. Where docs/TESTING.md names Ubuntu paths, they are KiCad's package layout, not something this project has run.

Clone with submodules, so the JLCPCB library comes along (about 103 MB; it is optional, see below):

```
git clone --recurse-submodules <repository-url>
cd <the cloned folder>
```

Already cloned without them? `git submodule update --init --depth 1` fetches them afterwards.

```
python -m venv .venv
.venv/Scripts/pip install -e ".[dev,krt]"
```

Extras: `dev` (pytest, ruff), `krt` (the runtime dependencies of KiCadRoutingTools: numpy, scipy, shapely, Pillow), `assets` (easyeda2kicad, AGPL-3.0, used as a subprocess only), `copilot` (kicad-python, for the live-IPC adapter).

### External tools

Each tool is found the same way: an environment variable first, then `PATH`, then the usual install locations. A missing tool makes the tools that need it return `status: incomplete` with install instructions, never a guess.

| Tool | Used for | Environment variable |
|---|---|---|
| KiCad 10 `kicad-cli` | **Required.** DRC, ERC, netlists, renders, exports, the gate | `KICAD_CLI_PATH` |
| KiCad's bundled Python | Freerouting's Specctra export and import; KiKit | `KICAD_PYTHON_PATH` |
| KiCadRoutingTools v0.23.1 (MIT) | Routing and placement optimisation, run from a checkout (`python build_router.py` builds its router) | `KICAD_ROUTING_TOOLS_PATH`, `KICAD_ROUTING_TOOLS_PYTHON` |
| Freerouting 2.5.0 jar (GPLv3) and Java 25 | Routing, as a subprocess | `FREEROUTING_JAR_PATH`, `JAVA_PATH` |
| ngspice 47 (pinned; `ngspice_con.exe` on Windows) | SPICE simulation | `NGSPICE_PATH` |
| KiKit | Panelisation | `KIKIT_PATH` |
| KiCad's demo projects | Inputs of the second rung and of the oracle tests | `KICAD_DEMOS_DIR` |
| KiCad's symbol libraries | Schematic building and tests | `KICAD_SYMBOL_DIR` |

All third-party tools, their licences and how they are used are listed in [THIRD_PARTY.md](THIRD_PARTY.md).

### Optional: the JLCPCB symbol and footprint library

Whether a part is a JLCPCB Basic, Preferred or Extended part is answered from a small index that ships with the repository, with no download. The ready-made symbols, footprints and 3D models behind that index (CDFER/JLCPCB-Kicad-Library, MIT, about 103 MB) are a git submodule that a plain `git clone` does not fetch. Fetch it when you want them:

```
git submodule update --init --depth 1 third_party/JLCPCB-Kicad-Library
```

Without it, asset lookups fall back to `easyeda2kicad` (the `assets` extra) and say so. The server looks for the library in this order: the `KICAD_MCP_JLCPCB_LIBRARY` environment variable, the submodule above, KiCad's third-party directory (a Plugin and Content Manager install). `jlcpcb_library_status` reports what it found and, for an empty or missing submodule, the command above. A new git worktree starts with the submodule empty; set `KICAD_MCP_JLCPCB_LIBRARY` to another checkout's `third_party/JLCPCB-Kicad-Library` to share it. To rebuild the index after moving the pin: `python scripts/refresh_jlcpcb_index.py`.

## MCP client configuration

```json
{
  "mcpServers": {
    "kicad": {
      "command": "path/to/.venv/Scripts/python",
      "args": ["-m", "kicad_mcp.server"]
    }
  }
}
```

The installed script does the same: `"command": "path/to/.venv/Scripts/kicad-mcp"` with no arguments.

The server starts with 19 tools: the three toolset tools (`list_toolsets`, `load_toolset`, `unload_toolset`), `project_create`, `project_open`, the gate (`verify_board`, `pin_rule_baseline`) and the checkpointed pairs for input preparation, pour step, schematic import, board rules, board outline and silkscreen minimums. Ten more toolsets load on demand with `load_toolset`: `verification` (9 tools), `sourcing` (13), `diagnostics` (2), `library` (3), `schematic` (7), `placement` (12), `manufacturing` (2), `routing` (6), `copilot` (9) and `simulation` (2). That is 65 more tools, 84 in total (counted from a running server on 2026-10-29).

## Principles

- **Evidence-gated:** a check that did not fully run says so and never reports clean.
- **`kicad-cli` is the judge:** structured DRC and ERC output, read in full. Renders are for humans only.
- **Batch tools:** operations take collections, not one item per call.
- **Checkpoints:** autorouting, autoplacement, ambiguous part matches and destructive edits return a proposal and a revision token. Nothing is written until you confirm, and for the newer checkpoint pairs a confirm is refused if the project's files changed in between.
- **Never hand-edit KiCad files:** every mutation goes through this project's tools, or through the project's write layer (`kicad_mcp.write_layer`), which saves through a temporary file, a parse check and `os.replace`.

## Limitations

- **Sourcing:** Nexar and Digi-Key providers are not built (they need credentials). The LCSC provider is blocked by a bot wall and returns an error rather than an empty list. The JLCPCB Basic parts index is vendored.
- **Autoplacement** (`propose_autoplacement`) is a naive row-packer. KiCadRoutingTools' placement optimiser is the stronger option.
- **Zones:** there is a pour step (`propose_pour_step` and `confirm_pour_step`) that adds a gate-graded plane zone from KiCadRoutingTools' plane script. There is no general zone-editing tool and no track or via removal tool.
- **Schematic editing tools** are unsafe on hierarchical (multi-sheet) projects; see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), "Known facts and gotchas".
- **Windows only** so far.

## Tests

Around 4,490 tests (4,486 without the network-marked ones; collected on 2026-10-29). From the repository root:

```
python -m pytest -m "not network"
```

Tests that need an external tool skip with a stated reason when the tool is absent. The full gate, `python scripts/batch_gate.py`, runs the whole suite and the linter in a clean tree and ends with one `GREEN` or `RED` line; it takes about 45 minutes. There is no hosted CI: that local gate is the test gate. See [docs/TESTING.md](docs/TESTING.md).

## Documentation

| File | Contents |
|---|---|
| [docs/OVERVIEW.md](docs/OVERVIEW.md) | A ten-minute tour: how a tool call works, the gate, the ladder |
| [docs/CONTEXT.md](docs/CONTEXT.md) | Glossary |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The decision record and the known facts and gotchas, with evidence |
| [docs/ROADMAP.md](docs/ROADMAP.md) | The acceptance ladder, carried-over defects, non-goals |
| [docs/TESTING.md](docs/TESTING.md) | The acceptance gate and the test suite |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute |
| [THIRD_PARTY.md](THIRD_PARTY.md) | Third-party tools and data, with licences |
| [CLAUDE.md](CLAUDE.md) | Rules for AI agents working in this repository |

## Contributing and licence

Contributions are welcome; read [CONTRIBUTING.md](CONTRIBUTING.md) first. The code is MIT-licensed ([LICENSE](LICENSE)). Copyleft tools (Freerouting, easyeda2kicad) are only ever run as subprocesses. `kiutils` (GPLv3) is not used.
