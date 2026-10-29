# Third-party tools and data

boardproof's own code is MIT-licensed ([LICENSE](LICENSE)). This file lists every third-party tool and data set it uses, under which licence, and how. Licences were read from each project's own licence file when the integration was decided (docs/ARCHITECTURE.md decision 6); check them again before you rely on this list.

The rule behind the table: a copyleft tool is only ever run as a separate process (an arm's-length subprocess, with an explicit argument list). It is never imported, linked or copied into this repository.

| Component | Licence | How it is used |
|---|---|---|
| [KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools) (KRT), tested with v0.23.1 | MIT | Router and placer. Found at run time through `KICAD_ROUTING_TOOLS_PATH` or KiCad's plugin directory and run as a subprocess from its own checkout. **Not copied** into this repository. Its runtime dependencies (numpy, scipy, shapely, Pillow) are the `krt` extra. |
| [Freerouting](https://github.com/freerouting/freerouting) 2.5.0 | GPLv3 | Router. A subprocess; the jar is not in this repository and is never downloaded automatically (`FREEROUTING_JAR_PATH`). It needs Java 25 (`JAVA_PATH`). |
| [easyeda2kicad](https://github.com/uPesy/easyeda2kicad.py) | AGPL-3.0 | Fetches symbols and footprints by LCSC part number. Optional (`[assets]` extra), invoked only as `python -m easyeda2kicad` in a subprocess, never imported. Fetched files are third-party data whose terms are those of their source (EasyEDA and LCSC); they are written into the user's project. |
| [kicad-python](https://gitlab.com/kicad/code/kicad-python) (`kipy`) | MIT | Imported lazily for the optional live-IPC `copilot` toolset (`[copilot]` extra). |
| [kicad-tools](https://github.com/rjwalters/kicad-tools) | MIT | Core dependency. Parses and serialises KiCad files through its low-level S-expression API and supplies footprint generators. Mutations go through this project's own write layer, not through its high-level models. |
| [ngspice](https://ngspice.sourceforge.io/) 47 (pinned) | Modified BSD | SPICE simulator. A subprocess (`ngspice_con.exe` on Windows), found through `NGSPICE_PATH`. Not included. |
| [KiKit](https://github.com/yaqwsx/KiKit) | MIT | Panelisation. Optional; runs in KiCad's bundled Python as a subprocess (`KIKIT_PATH`). |
| KiCad 10 and `kicad-cli` | GPLv3 or later (the application); libraries under CC BY-SA 4.0 with a linking exception | The required judge: DRC, ERC, netlists, exports. A subprocess. Its bundled Python runs the Specctra export and import for Freerouting. Not included. |
| [CDFER/JLCPCB-Kicad-Library](https://github.com/CDFER/JLCPCB-Kicad-Library), a git submodule at `third_party/JLCPCB-Kicad-Library` | MIT | Symbols, footprints and 3D models of JLCPCB's Basic and Preferred parts. **Fetched on demand, not part of this repository's own content**: the repository holds only the gitlink (pinned to commit `e37fb96f4a4b54013293b80963efb1208d573d43`, 2026-08-08) and `.gitmodules`; `git submodule update --init --depth 1 third_party/JLCPCB-Kicad-Library` downloads it. Read at run time, never modified. Full licence text below. |
| `jlcpcb_basic_index.json` in `src/kicad_mcp/sourcing/data/` | MIT | Index of JLCPCB Basic and Preferred parts, derived from the library above at the same commit `e37fb96f4a4b54013293b80963efb1208d573d43` (`scripts/refresh_jlcpcb_index.py` regenerates it). Copyright (c) 2023 Chris. The file carries its source, licence and copyright line in its header. |
| `pic_programmer` demo project from KiCad's demos | CC BY-SA 4.0 (KiCad demos are licensed CC BY-SA 4.0; see the KiCad project's documentation for demos) | The input of ladder rung 2. It is read from the user's KiCad install and staged into a scratch copy with its tracks, vias and zones removed. This repository does not contain a copy. **Any modified copy you publish stays CC BY-SA 4.0 and needs the attribution below.** |
| Python packages `mcp`, `pydantic`, `httpx`, `pdfplumber` | MIT, MIT, BSD-3-Clause, MIT | Core dependencies, imported normally. |
| `pytest`, `pytest-asyncio`, `ruff` | MIT | Development dependencies (`dev` extra). |

## Not used

- `kiutils` (GPLv3) is not used and must not be imported.
- LTspice is not used by this repository: no code or file here depends on it. Its licence is not a redistribution licence, and it restricts publishing benchmark or comparison results, so keep such results out of contributions.
- Konnect (AGPL plus commercial) and the other MCP servers and design-automation projects surveyed (KiCAD-MCP-Server, kicad-mcp-pro, circuit-synth, atopile, tscircuit and others) were read for ideas only. No code was taken from them.

## Test fixtures

`tests/fixtures/README.md` records the origin of every fixture. `simple_led.*` is copied from the kicad-tools repository (MIT). Manufacturers' datasheets are not project content and are not covered by this project's licence.

## Licence text: CDFER/JLCPCB-Kicad-Library

The library is not copied into this repository; it is linked as a git submodule and its licence is reproduced here as MIT requires of copies and derived data (the index above). Upstream commit `e37fb96f4a4b54013293b80963efb1208d573d43`, file `LICENSE`:

```
MIT License

Copyright (c) 2023 Chris

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Attribution: `pic_programmer`

Boards derived from KiCad's `pic_programmer` demo (rung 2) are modified copies of KiCad's demo project `demos/pic_programmer`, shipped with KiCad 10.0.x (source: gitlab.com/kicad/code/kicad, `demos/pic_programmer`).

- **Authors:** the KiCad project and its contributors.
- **Licence:** Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0), KiCad's licence for its demo projects: https://creativecommons.org/licenses/by-sa/4.0/
- **Changes made by this project:** all tracks and vias removed (placement kept), the project's rules prepared for routing, and in measurement runs routed by automated routers.
- **Licence of the modified files:** CC BY-SA 4.0, the same licence.

No endorsement by the KiCad project is implied.
