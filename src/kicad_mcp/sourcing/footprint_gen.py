"""Parametric footprint generation fallback, for parts with no ready
asset from the P1-05 easyeda2kicad path.

Deviation from the original archived backlog P1-06 spec: the ticket named
`kicad-footprint-generator`/`footag` as candidates. Investigated both
while building this:

- `footag` (endofexclusive/footag) turned out to be a C library, not
  Python -- not embeddable as a Python import at all.
- `kicad-footprint-generator` (pip name `kicad-library-generators`,
  installed via `pip install git+https://gitlab.com/kicad/libraries/
  kicad-footprint-generator.git`) is GPLv3 (as is its underlying
  `KicadModTree` drawing library), so per this project's established
  GPL-isolation policy (see docs/ARCHITECTURE.md's kiutils/easyeda2kicad
  precedents) it could only be used as an arm's-length subprocess, never
  imported. On top of that, the pip-installed package throws a real
  `IndexError` in `SMD_2terminal_chip_molded/footprint.py`'s
  `create_footprints` (`get_spec_file_names(generator_name)` returns an
  empty list) when run standalone rather than from within a full
  monorepo checkout -- reproduced directly, not assumed. Not pursued
  further; worth a bug report upstream if this project ends up needing
  that specific generator family later.

Instead: `kicad_tools.library.generators` (MIT, our existing core
dependency -- discovered it has its own parametric footprint generators
while investigating the above) provides `create_chip`/`create_soic`/
`create_qfp`/`create_qfn`/`create_sot`/`create_dip`/`create_pin_header`,
each returning a `kicad_tools.library.footprint.Footprint` with
`.save(path)`. This is a strictly better fit than either originally-named
candidate: no new dependency, no licensing complication, and it actually
works. This module is a thin adapter over it, following this project's
evidence-gated-verdict and tool-naming conventions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from kicad_tools.library import generators as kt_generators
from kicad_tools.library.footprint import Footprint

# package_type -> underlying kicad_tools generator function. Each function's
# real keyword-argument shape varies (chip takes a size string; soic/qfp/qfn
# take pins/pitch/body dimensions) -- callers pass whatever kwargs the chosen
# generator accepts, see kicad_tools.library.generators for each signature.
_GENERATORS: dict[str, Callable[..., Footprint]] = {
    "chip": kt_generators.create_chip,
    "soic": kt_generators.create_soic,
    "qfp": kt_generators.create_qfp,
    "qfn": kt_generators.create_qfn,
    "sot": kt_generators.create_sot,
    "dip": kt_generators.create_dip,
    "pin_header": kt_generators.create_pin_header,
}


def list_package_types() -> list[str]:
    """Package types this generator can produce -- see kicad_tools.library.generators
    for each one's exact keyword-argument shape."""
    return sorted(_GENERATORS)


def generate_footprint(package_type: str, output_path: str | Path, **kwargs: Any) -> dict:
    """Generate an IPC-7351-class footprint and write it to output_path.

    Evidence-gated per docs/ARCHITECTURE.md: an unknown package_type or a
    generation failure returns an explicit status/reason, never raises
    for an expected "can't do this one" case.
    """
    generator = _GENERATORS.get(package_type)
    if generator is None:
        return {
            "status": "unsupported_package_type",
            "reason": f"no generator for '{package_type}'",
            "available": list_package_types(),
        }

    # kicad_tools' Footprint.to_sexp escapes nothing: a LF or CR in a name reached the
    # .kicad_mod raw (build ticket 25). Every text argument here is one line.
    line_breaks = sorted(k for k, v in kwargs.items() if isinstance(v, str) and ("\n" in v or "\r" in v))
    if line_breaks:
        return {
            "status": "incomplete",
            "reason": f"line break in {', '.join(line_breaks)}: footprint text is one line; nothing was written",
        }
    # The same writer leaves '"' and '\' raw too: a '"' ends the quoted string early and KiCad
    # cannot load the library; KiCad reads '\' as an escape (build ticket 69, review round 1).
    unescaped = sorted(k for k, v in kwargs.items() if isinstance(v, str) and ('"' in v or "\\" in v))
    if unescaped:
        return {
            "status": "incomplete",
            "reason": f"'\"' or '\\' in {', '.join(unescaped)}: kicad_tools writes footprint text unescaped, "
            "so KiCad could not read it back; nothing was written",
        }

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        footprint = generator(**kwargs)
        footprint.save(output_path)
    except Exception as exc:  # noqa: BLE001 -- report, don't crash the tool call
        return {"status": "incomplete", "reason": str(exc)}

    return {
        "status": "ok",
        "package_type": package_type,
        "footprint_name": footprint.name,
        "path": str(output_path.resolve()),
        "pad_count": len(footprint.pads),
    }
