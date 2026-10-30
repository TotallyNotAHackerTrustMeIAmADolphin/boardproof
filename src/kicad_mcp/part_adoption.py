"""Adopt parts into a project-local library: symbol, footprint and 3D model, with gate checks.

Contract: build ticket 38. ``fetch_part_assets`` (``sourcing/asset_fetch.py``) and
``resolve_jlcpcb_assets`` give the assets of a part somewhere on disk; nothing put them into the
project's own library and fixed the references. This module does, as a checkpointed pair
(docs/ARCHITECTURE.md decision 4; the shared helper in ``kicad_mcp.checkpoint``):

``propose_adopt_parts`` writes nothing. For a LIST of parts it reads every asset, lists what each
part would add and every problem it has, and returns a ``revision_token``. One rejected part
rejects the whole proposal (no token): a half-adopted batch is not a state anyone asked for.
``confirm_adopt_parts`` builds a NEW project revision beside the source (the source is never
touched): the symbols go into ``<library_dir>/<library_name>.kicad_sym``, the footprints into
``<library_name>.pretty``, the model files into ``<library_name>.3dshapes``, both library tables
gain a ``${KIPRJMOD}``-relative row (``toolsets.library._register_one``), every footprint's
``(model ...)`` path is rewritten to ``${KIPRJMOD}/...`` and every symbol gets the properties
Footprint, Datasheet, LCSC, Source (name and version) and License. The revision is verified from
a fresh reload and moved into place only when every check passes.

What is machine-checked (each per part, fail closed; ``fail`` and ``incomplete`` both keep the
revision out of place):

- ``model_path_project_relative``: the saved footprint's model path starts with ``${KIPRJMOD}/``,
  has no ``..`` and no drive, and lies in the library's ``.3dshapes`` folder;
- ``model_file_present``: that path resolves, in the revision, to a non-empty file;
- ``mandatory_properties``: Footprint (naming a footprint that exists in a library the project's
  fp-lib-table lists), Datasheet, LCSC (equal to the part's id), Source (with a version) and
  License are set and not empty on the saved symbol;
- ``side_view_render``: ``kicad-cli pcb render --side front`` of a scratch board that carries the
  adopted footprint, once with and once without its model. The model must add a silhouette above
  the board's top face (at least 0.1 mm, or 2 px where the image cannot resolve that) and none
  below the bottom face. A render that cannot run or be read is ``incomplete``, never clean.

Named limits: the render check sees the HEIGHT profile of the body in the side view only. A model
shifted along the board's plane, rotated about the vertical axis, or mirrored in X or Y still
passes it; the saved ``top`` render and the ``front`` renders in ``adoption_evidence/`` are for a
human to look at, and no verdict rests on them. The model's own geometry (a STEP or WRL file
that is valid but the wrong part) is not judged. The licence text is the caller's claim, not
looked up. A failed or incomplete confirm leaves no evidence images behind (the revision is
removed with them); the result carries the measured figures.

Every position or size read here comes from the raw S-expression (``(at ...)``, ``(size ...)``),
never from ``kicad_tools`` convenience properties (CLAUDE.md hard rule 2); every file is saved
through the write layer (rule 1).
"""

from __future__ import annotations

import math
import re
import shutil
import struct
import subprocess
import time
import zlib
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kicad_tools.schema import PCB
from kicad_tools.sexp import SExp

from kicad_mcp import checkpoint, write_layer
from kicad_mcp import verify_board as gate
from kicad_mcp.toolsets import library as lib

DEFAULT_LIBRARY = "project_parts"
DEFAULT_LIBRARY_DIR = "lib"
MANDATORY = ("Footprint", "Datasheet", "LCSC", "Source", "License")
MODEL_SUFFIXES = (".step", ".stp", ".wrl")
EVIDENCE_DIR = "adoption_evidence"
URI_PREFIX = "${KIPRJMOD}/"
MIN_RISE_MM = 0.1
MAX_SINK_MM = 0.1
MIN_RISE_PX = 2
BOARD_THICKNESS_DEFAULT_MM = 1.6
RENDER_TIMEOUT_S = 120
RENDER_WIDTH, RENDER_HEIGHT = 640, 240

_LCSC = re.compile(r"C\d+")
_LIB_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_BAD_FILE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_VERSION = re.compile(r"\d")
_NOT_SET = ("", "~")

_clock = time.time  # replaced in tests
_PENDING = checkpoint.Pending(clock=lambda: _clock())

_DRIFT = (
    "the project (or one of the part files the proposal read) changed since the proposal was made: "
    "re-run propose_adopt_parts"
)

# ---------------------------------------------------------------------------- raw readers


def _children(node: SExp, name: str) -> list[SExp]:
    return [c for c in node.children if not c.is_atom and c.name == name]


def _floats(node: SExp | None) -> list[float] | None:
    if node is None:
        return None
    try:
        return [float(v) for v in node.values]
    except (TypeError, ValueError):
        return None


def _properties(symbol: SExp) -> dict[str, str]:
    """``{key: value}`` of the symbol's own ``(property "Key" "Value" ...)`` nodes, last one
    wins as in KiCad."""
    out: dict[str, str] = {}
    for prop in _children(symbol, "property"):
        key, value = prop.get_string(0), prop.get_string(1)
        if key is not None:
            out[key] = value if value is not None else ""
    return out


def _pins(symbol: SExp) -> list[tuple[str, str]]:
    """``(number, name)`` of every pin of the symbol and its unit sub-symbols, sorted."""
    pins: list[tuple[str, str]] = []

    def walk(node: SExp) -> None:
        for child in node.children:
            if child.is_atom:
                continue
            if child.name == "pin":
                number, name = child.find("number"), child.find("name")
                pins.append(
                    (
                        number.get_string(0) if number is not None else "",
                        name.get_string(0) if name is not None else "",
                    )
                )
            elif child.name == "symbol":
                walk(child)

    walk(symbol)
    return sorted((str(n), str(m)) for n, m in pins)


def _pad_numbers(footprint: SExp) -> list[str]:
    return sorted(str(p.get_string(0)) for p in _children(footprint, "pad"))


def _model_nodes(footprint: SExp) -> list[SExp]:
    return _children(footprint, "model")


def _model_uri(footprint: SExp) -> str | None:
    nodes = _model_nodes(footprint)
    return nodes[0].get_string(0) if len(nodes) == 1 else None


def _pad_extent(footprint: SExp) -> tuple[float, float]:
    """Largest ``|x|`` and ``|y|`` reach of the pads from the footprint origin, from the raw
    ``(at x y)`` and ``(size w h)`` of each pad (a pad's rotation is ignored: the larger of its
    two sizes is used as its radius, which only over-estimates)."""
    reach_x = reach_y = 0.0
    for pad in _children(footprint, "pad"):
        at, size = _floats(pad.find("at")), _floats(pad.find("size"))
        if not at or len(at) < 2 or not size:
            continue
        radius = max(size) / 2
        reach_x = max(reach_x, abs(at[0]) + radius)
        reach_y = max(reach_y, abs(at[1]) + radius)
    return reach_x, reach_y


# ---------------------------------------------------------------------------- planning


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _one_line_problem(label: str, value: str) -> str | None:
    try:
        write_layer.string(value)
    except write_layer.InvalidStringError as exc:
        return f"{label}: {exc}"
    if "\n" in value or "\r" in value:
        return f"{label} holds a line break: it must be one line"
    return None


def _library_names_problem(library_name: Any, library_dir: Any) -> str | None:
    if not isinstance(library_name, str) or not _LIB_NAME.fullmatch(library_name) or library_name in (".", ".."):
        return f"library_name {library_name!r} must be letters, digits, '_', '-' or '.'"
    if not isinstance(library_dir, str) or not library_dir or library_dir.startswith(("/", "\\")):
        return f"library_dir {library_dir!r} must be a folder path relative to the project"
    parts = library_dir.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") or not _LIB_NAME.fullmatch(p) for p in parts):
        return f"library_dir {library_dir!r} must be relative, without '..', and use letters, digits, '_', '-' or '.'"
    return None


def _library_paths(project_dir: Path, library_name: str, library_dir: str) -> dict[str, Path]:
    base = project_dir / Path(*library_dir.replace("\\", "/").split("/"))
    return {
        "symbols": base / f"{library_name}.kicad_sym",
        "footprints": base / f"{library_name}.pretty",
        "models": base / f"{library_name}.3dshapes",
    }


def _one_symbol(path: Path) -> tuple[SExp, SExp] | str:
    try:
        root = write_layer.load(path)
    except Exception as exc:  # noqa: BLE001
        return f"symbol file {path.name} cannot be read: {exc}"
    if root.name != "kicad_symbol_lib":
        return f"{path.name} is not a symbol library (root is ({root.name} ...))"
    symbols = _children(root, "symbol")
    if len(symbols) != 1:
        return f"{path.name} holds {len(symbols)} symbols: pass a file with exactly one"
    return root, symbols[0]


def _one_footprint(path: Path) -> tuple[SExp, str] | str:
    try:
        root = write_layer.load(path)
    except Exception as exc:  # noqa: BLE001
        return f"footprint file {path.name} cannot be read: {exc}"
    if root.name not in ("footprint", "module"):
        return f"{path.name} is not a footprint (root is ({root.name} ...))"
    name = root.get_string(0)
    if not name or _BAD_FILE_CHARS.search(name) or name in (".", ".."):
        return f"footprint name {name!r} cannot be a file name"
    return root, name


def _pick_model(directory: Path, footprint: SExp) -> Path | str:
    candidates = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in MODEL_SUFFIXES)
    if not candidates:
        return f"{directory.name} holds no 3D model ({', '.join(MODEL_SUFFIXES)})"
    named = _model_uri(footprint)
    if named:
        base = re.split(r"[\\/]", named)[-1]
        match = [p for p in candidates if p.name == base]
        if match:
            return match[0]
    if len(candidates) == 1:
        return candidates[0]
    return f"{directory.name} holds {len(candidates)} models and the footprint names none of them: pass model_path"


def _existing_symbols(sym_file: Path) -> dict[str, dict[str, str]] | str:
    """``{symbol name: properties}`` of the library file already in the project, or the reason it
    cannot be read; ``{}`` when there is none."""
    if not sym_file.exists():
        return {}
    try:
        root = write_layer.load(sym_file)
    except Exception as exc:  # noqa: BLE001
        return f"{sym_file.name} in the project cannot be read: {exc}"
    if root.name != "kicad_symbol_lib":
        return f"{sym_file.name} in the project is not a symbol library"
    return {str(s.get_string(0)): _properties(s) for s in _children(root, "symbol")}


def _default_source() -> tuple[str, str] | None:
    """Name and installed version of easyeda2kicad (read from package metadata: the AGPL tool is
    never imported), the source of what ``fetch_part_assets`` produces."""
    try:
        from importlib import metadata

        return "easyeda2kicad", metadata.version("easyeda2kicad")
    except Exception:  # noqa: BLE001
        return None


def _plan_part(
    spec: Any,
    index: int,
    paths: dict[str, Path],
    existing: dict[str, dict[str, str]],
    defaults: dict[str, str | None],
) -> dict:
    """The plan of one part: ``status`` ``ready``, ``already_adopted`` or ``rejected`` with the
    ``problems``; everything read from the files."""
    problems: list[str] = []
    entry: dict = {"index": index, "lcsc_id": None, "status": "rejected", "problems": problems}
    if not isinstance(spec, dict):
        problems.append("a part is an object with lcsc_id, symbol_path, footprint_path or footprint_dir, and a model")
        return entry
    lcsc = _clean_text(spec.get("lcsc_id"))
    entry["lcsc_id"] = lcsc
    if lcsc is None or not _LCSC.fullmatch(lcsc):
        problems.append(f"lcsc_id {spec.get('lcsc_id')!r} is not an LCSC part number ('C' and digits)")

    def path_of(*keys: str, kind: str) -> Path | None:
        for key in keys:
            value = spec.get(key)
            if value is None:
                continue
            path = Path(str(value))
            ok = path.is_file() if kind == "file" else path.is_dir()
            if not ok:
                problems.append(f"{key} {value!r} is not an existing {kind}")
                return None
            return path
        return None

    symbol_path = path_of("symbol_path", "symbol_library_path", kind="file")
    if symbol_path is None and not any("symbol_" in p and "path" in p for p in problems):
        problems.append("symbol_path is required")
    fp_path = path_of("footprint_path", kind="file")
    if fp_path is None and spec.get("footprint_path") is None:
        fp_dir = path_of("footprint_dir", kind="dir")
        if fp_dir is not None:
            found = sorted(fp_dir.glob("*.kicad_mod"))
            if len(found) == 1:
                fp_path = found[0]
            else:
                problems.append(f"footprint_dir holds {len(found)} .kicad_mod files: pass footprint_path")
        elif spec.get("footprint_dir") is None:
            problems.append("footprint_path or footprint_dir is required")

    sym_found = _one_symbol(symbol_path) if symbol_path is not None else None
    fp_found = _one_footprint(fp_path) if fp_path is not None else None
    if isinstance(sym_found, str):
        problems.append(sym_found)
        sym_found = None
    if isinstance(fp_found, str):
        problems.append(fp_found)
        fp_found = None

    model_path = path_of("model_path", "model_3d_path", kind="file")
    if model_path is None and spec.get("model_path") is None and spec.get("model_3d_path") is None:
        model_dir = path_of("model_3d_dir", kind="dir")
        if model_dir is not None and fp_found is not None:
            picked = _pick_model(model_dir, fp_found[0])
            if isinstance(picked, str):
                problems.append(picked)
            else:
                model_path = picked
        elif spec.get("model_3d_dir") is None:
            problems.append("a 3D model is required: model_path or model_3d_dir (a part without one is not adopted)")
    if model_path is not None and model_path.suffix.lower() not in MODEL_SUFFIXES:
        problems.append(f"model {model_path.name} is not one of {', '.join(MODEL_SUFFIXES)}")
        model_path = None
    if model_path is not None and model_path.stat().st_size == 0:
        problems.append(f"model {model_path.name} is empty")
        model_path = None
    if model_path is not None and _BAD_FILE_CHARS.search(model_path.name):
        problems.append(f"model file name {model_path.name!r} cannot be used")
        model_path = None

    # Mandatory properties: Footprint is derived at confirm; the others must be given or read.
    props: dict[str, str] = {}
    source_props = _properties(sym_found[1]) if sym_found else {}
    datasheet = _clean_text(spec.get("datasheet")) or _clean_text(source_props.get("Datasheet"))
    if datasheet in _NOT_SET:
        datasheet = None
    source = _clean_text(spec.get("source")) or defaults["source"]
    version = _clean_text(spec.get("source_version")) or defaults["source_version"]
    license_ = _clean_text(spec.get("license")) or defaults["license"]
    if datasheet is None:
        problems.append("Datasheet is missing: pass datasheet (a URL), the symbol has none")
    if source is None:
        problems.append("Source is missing: pass source")
    if version is None or not _VERSION.search(version):
        problems.append("Source needs a version (source_version, containing a digit or a commit id with one)")
    if license_ is None:
        problems.append("License is missing: pass license (the part's library licence, as the user states it)")
    if lcsc and datasheet and source and version and license_:
        props = {"Datasheet": datasheet, "LCSC": lcsc, "Source": f"{source} {version}", "License": license_}
        for key, value in props.items():
            if (bad := _one_line_problem(key, value)) is not None:
                problems.append(bad)

    if sym_found and fp_found:
        symbol_name = str(sym_found[1].get_string(0))
        footprint_name = fp_found[1]
        if (bad := _one_line_problem("symbol name", symbol_name)) is not None:
            problems.append(bad)
        entry.update(symbol=symbol_name, footprint=footprint_name)
        fp_file = paths["footprints"] / f"{footprint_name}.kicad_mod"
        if symbol_name in existing:
            if lcsc and existing[symbol_name].get("LCSC") == lcsc:
                entry["status"] = "already_adopted"
                return entry
            problems.append(f"the library already has a symbol {symbol_name!r} for another part: nothing is overwritten")
        if fp_file.exists():
            problems.append(f"{fp_file.name} already exists in {paths['footprints'].name}: nothing is overwritten")
        if not _pins(sym_found[1]):
            problems.append("the symbol has no pins")
        symbol_pads = {p[0] for p in _pins(sym_found[1])}
        pads = set(_pad_numbers(fp_found[0]))
        if pads and symbol_pads and not symbol_pads <= pads:
            problems.append(
                f"symbol pins {sorted(symbol_pads - pads)[:5]} have no pad in the footprint {footprint_name!r}"
            )
        if len(_model_nodes(fp_found[0])) > 1:
            problems.append("the footprint carries more than one (model ...): pass a footprint with one")
    if model_path is not None:
        target = paths["models"] / model_path.name
        if target.exists() and target.read_bytes() != model_path.read_bytes():
            problems.append(f"{target.name} already exists in {paths['models'].name} with other content")
        entry["model"] = model_path.name
    if problems:
        return entry
    entry.update(
        status="ready",
        properties=props,
        sources={"symbol": str(symbol_path), "footprint": str(fp_path), "model": str(model_path)},
    )
    return entry


def _plan(project_dir: Path, parts: Any, library_name: Any, library_dir: Any, defaults: dict) -> dict | str:
    bad = _library_names_problem(library_name, library_dir)
    if bad:
        return bad
    if not isinstance(parts, list) or not parts:
        return "parts must be a non-empty list of part objects"
    paths = _library_paths(project_dir, library_name, library_dir)
    existing = _existing_symbols(paths["symbols"])
    if isinstance(existing, str):
        return existing
    planned = [_plan_part(spec, i, paths, existing, defaults) for i, spec in enumerate(parts)]
    seen: Counter = Counter(p["lcsc_id"] for p in planned if p["lcsc_id"])
    for p in planned:
        if p["lcsc_id"] and seen[p["lcsc_id"]] > 1 and p["status"] != "rejected":
            p["status"] = "rejected"
            p["problems"].append(f"{p['lcsc_id']} is listed more than once in this call")
    for key in ("symbol", "footprint"):
        names = Counter(p[key] for p in planned if p.get(key))
        for p in planned:
            if p.get(key) and names[p[key]] > 1 and p["status"] == "ready":
                p["status"] = "rejected"
                p["problems"].append(f"another part in this call has the same {key} name {p[key]!r}")
    return {"paths": paths, "parts": planned}


def _default_output_dir(pcb_path: Path) -> Path:
    resolved = pcb_path.resolve()
    return resolved.parent.parent / f"{resolved.parent.name}_adopted"


def _public(part: dict) -> dict:
    return {k: v for k, v in part.items() if k not in ("sources", "index")} | {"position": part["index"]}


def propose_adopt_parts(
    pcb_path: str | Path,
    parts: list[dict],
    library_name: str = DEFAULT_LIBRARY,
    library_dir: str = DEFAULT_LIBRARY_DIR,
    license: str | None = None,  # noqa: A002  (the tool's argument name)
    source: str | None = None,
    source_version: str | None = None,
    output_dir: str | Path | None = None,
) -> dict:
    """Dry run. Reads every part's files and lists what would be adopted; writes nothing. One
    rejected part means no token."""
    src = Path(pcb_path)
    if not src.is_file():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
    default_source = _default_source()
    defaults = {
        "license": _clean_text(license),
        "source": _clean_text(source) or (default_source[0] if default_source and not source_version else None),
        "source_version": _clean_text(source_version)
        or (default_source[1] if default_source and not _clean_text(source) else None),
    }
    plan = _plan(src.parent, parts, library_name, library_dir, defaults)
    if isinstance(plan, str):
        return {"status": "error", "reason": plan}
    planned = plan["parts"]
    report = [_public(p) for p in planned]
    rejected = [p for p in planned if p["status"] == "rejected"]
    ready = [p for p in planned if p["status"] == "ready"]
    base = {"library": {"name": library_name, "dir": library_dir}, "parts": report}
    if rejected:
        return {
            "status": "blocked",
            **base,
            "revision_token": None,
            "reason": f"{len(rejected)} of {len(planned)} part(s) cannot be adopted (see each part's problems); "
            "nothing is proposed, so nothing half-adopts. Fix them or leave them out, then propose again",
        }
    if not ready:
        return {"status": "nothing_to_do", **base, "revision_token": None}
    out_dir = Path(output_dir) if output_dir else _default_output_dir(src)
    problem = checkpoint.output_dir_problem(src, out_dir, what="the adopted project")
    if problem:
        return {"status": "error", "reason": problem}
    sources = [Path(p["sources"][k]) for p in ready for k in ("symbol", "footprint", "model")]
    payload = {
        "out_dir": str(out_dir),
        "library_name": library_name,
        "library_dir": library_dir,
        "parts": [
            {k: p[k] for k in ("lcsc_id", "symbol", "footprint", "model", "properties", "sources")} for p in ready
        ],
    }
    try:
        token = _PENDING.issue(src, payload, extra_files=sources)
    except checkpoint.Refused as exc:
        return {"status": "incomplete", "reason": str(exc)}
    return {
        "status": "proposed",
        **base,
        "output_pcb": str(out_dir / src.name),
        "revision_token": token,
        "note": (
            "creates a new project revision with the library added; the original is untouched. "
            "confirm_adopt_parts checks the model path, the model file, the mandatory properties and a "
            "side-view render before it moves the revision into place. Call it with this revision_token."
        ),
    }


# ---------------------------------------------------------------------------- the render check


class _Unreadable(Exception):
    pass


def _read_png_alpha_rows(path: Path) -> list[bool]:
    """For each row of an 8-bit RGBA PNG: whether any pixel has alpha above zero. Raises
    ``_Unreadable`` for anything else (palette, 16-bit, interlaced, no alpha channel)."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise _Unreadable("not a PNG file")
    pos, header, idat = 8, None, []
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if header is None or not idat:
        raise _Unreadable("PNG without header or data")
    width, height, depth, color, _compression, _filter, interlace = header
    if (depth, color, interlace) != (8, 6, 0):
        raise _Unreadable(f"PNG is not 8-bit RGBA without interlacing (depth {depth}, colour type {color})")
    stride = width * 4
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as exc:
        raise _Unreadable(f"PNG data does not decompress: {exc}") from exc
    if len(raw) != height * (stride + 1):
        raise _Unreadable("PNG data has the wrong size")
    previous = bytearray(stride)
    rows: list[bool] = []
    for y in range(height):
        offset = y * (stride + 1)
        kind_byte = raw[offset]
        line = bytearray(raw[offset + 1 : offset + 1 + stride])
        if kind_byte == 1:
            for i in range(4, stride):
                line[i] = (line[i] + line[i - 4]) & 255
        elif kind_byte == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 255
        elif kind_byte == 3:
            for i in range(stride):
                left = line[i - 4] if i >= 4 else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 255
        elif kind_byte == 4:
            for i in range(stride):
                a = line[i - 4] if i >= 4 else 0
                b = previous[i]
                c = previous[i - 4] if i >= 4 else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 255
        elif kind_byte != 0:
            raise _Unreadable(f"PNG filter type {kind_byte} is unknown")
        rows.append(any(line[3::4]))
        previous = line
    return rows


def _silhouette(path: Path) -> tuple[int, int]:
    """First and last row (from the top) with any opaque pixel. A render with no transparent
    background (the whole image opaque) cannot be measured and raises ``_Unreadable``."""
    rows = _read_png_alpha_rows(path)
    filled = [i for i, r in enumerate(rows) if r]
    if not filled:
        raise _Unreadable("the render is empty")
    if filled[0] == 0 and filled[-1] == len(rows) - 1 and len(filled) == len(rows):
        raise _Unreadable("the render has no transparent background, so the silhouette cannot be measured")
    return filled[0], filled[-1]


def kicad_cli_render(board: Path, png: Path, side: str) -> dict:
    """``kicad-cli pcb render --side <side>`` on a transparent background. ``{"status": "ok"}``
    or ``{"status": "incomplete", "reason"}``; the exit code and the file decide, never the text
    (kicad-cli prints in the user's language)."""
    try:
        from kicad_mcp.kicad_cli import find_kicad_cli

        cli = find_kicad_cli()
    except Exception as exc:  # noqa: BLE001
        return {"status": "incomplete", "reason": f"kicad-cli not found: {exc}"}
    cmd = [
        str(cli), "pcb", "render", "--side", side, "--background", "transparent", "--quality", "basic",
        "--width", str(RENDER_WIDTH), "--height", str(RENDER_HEIGHT), "-o", str(png), str(board),
    ]  # fmt: skip
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=RENDER_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "incomplete", "reason": f"kicad-cli pcb render did not finish: {exc}"}
    if done.returncode != 0 or not png.is_file() or png.stat().st_size == 0:
        return {"status": "incomplete", "reason": f"kicad-cli pcb render exited {done.returncode} with no image"}
    return {"status": "ok"}


def _raw_board_thickness(board_root: SExp) -> float:
    general = board_root.find("general")
    thickness = _floats(general.find("thickness")) if general is not None else None
    return thickness[0] if thickness else BOARD_THICKNESS_DEFAULT_MM


def _scratch_board(project: Path, footprint_file: Path, name: str, *, with_model: bool) -> tuple[Path, float]:
    """A blank board in the project's own folder (so ``${KIPRJMOD}`` is the project) carrying the
    adopted footprint at its middle, saved by the write layer; returns it and its thickness read
    from the raw ``(general (thickness ...))``."""
    footprint = write_layer.load(footprint_file)
    if not with_model:
        for node in _model_nodes(footprint):
            footprint.remove(node)
    reach_x, reach_y = _pad_extent(footprint)
    width = max(20.0, math.ceil(2 * reach_x + 10))
    height = max(20.0, math.ceil(2 * reach_y + 10))
    board = PCB.create(width=width, height=height, center=False)
    at = SExp.list("at")
    at.append(write_layer.number(width / 2))
    at.append(write_layer.number(height / 2))
    if footprint.find("layer") is not None:
        footprint.insert_after("layer", at)
    else:
        footprint.append(at)
    board._sexp.append(footprint)
    target = project / name
    write_layer.save_pcb(board, target)
    return target, _raw_board_thickness(write_layer.load(target))


def _render_check(
    project: Path, lcsc: str, footprint_file: Path, render: Callable[[Path, Path, str], dict]
) -> tuple[dict, dict]:
    """``(check, evidence)`` of the side-view render of one part."""
    evidence_dir = project / EVIDENCE_DIR
    evidence_dir.mkdir(exist_ok=True)
    front = evidence_dir / f"{lcsc}_front.png"
    baseline = evidence_dir / f"{lcsc}_front_without_model.png"
    top = evidence_dir / f"{lcsc}_top.png"
    check = {"check": "side_view_render", "status": "incomplete", "detail": ""}
    rel = f"{EVIDENCE_DIR}/"  # relative to the revision: confirm_adopt_parts makes them absolute
    evidence: dict = {
        "front": rel + front.name,
        "front_without_model": rel + baseline.name,
        "top": rel + top.name,
    }
    try:
        with_board, thickness = _scratch_board(project, footprint_file, f".adopt_{lcsc}_with.kicad_pcb", with_model=True)
        bare_board, _ = _scratch_board(project, footprint_file, f".adopt_{lcsc}_without.kicad_pcb", with_model=False)
        for board, png, side in ((with_board, front, "front"), (bare_board, baseline, "front")):
            result = render(board, png, side)
            if result.get("status") != "ok":
                check["detail"] = f"the {png.name} render could not be made: {result.get('reason')}"
                return check, evidence
        top_result = render(with_board, top, "top")  # evidence for a human only
        if top_result.get("status") != "ok":
            evidence["top"] = None
            evidence["top_note"] = f"not made: {top_result.get('reason')}"
        try:
            with_top, with_bottom = _silhouette(front)
            bare_top, bare_bottom = _silhouette(baseline)
        except _Unreadable as exc:
            check["detail"] = f"the render cannot be measured: {exc}"
            return check, evidence
    except Exception as exc:  # noqa: BLE001  (a check that cannot run says so)
        check["detail"] = f"the render check could not be set up: {type(exc).__name__}: {exc}"
        return check, evidence
    finally:
        for path in project.glob(f".adopt_{lcsc}_*"):  # the boards and the .kicad_prl kicad-cli leaves
            path.unlink(missing_ok=True)
    slab_px = bare_bottom - bare_top + 1
    px_per_mm = slab_px / thickness if thickness > 0 else 0.0
    rise_px, sink_px = bare_top - with_top, with_bottom - bare_bottom
    if px_per_mm <= 0 or slab_px < 3:
        check["detail"] = f"the board slab is {slab_px} px high in the render: too small to measure"
        return check, evidence
    rise_mm, sink_mm = rise_px / px_per_mm, sink_px / px_per_mm
    need_mm = max(MIN_RISE_MM, MIN_RISE_PX / px_per_mm)
    measured = {"body_above_board_mm": round(rise_mm, 3), "body_below_board_mm": round(sink_mm, 3)}
    evidence.update(measured)
    if sink_mm > max(MAX_SINK_MM, MIN_RISE_PX / px_per_mm):
        check.update(
            status="fail",
            detail=f"the model reaches {sink_mm:.2f} mm below the board's bottom face: it is mounted upside down "
            "or offset in Z",
        )
    elif rise_mm < need_mm:
        check.update(
            status="fail",
            detail=f"the model adds {rise_mm:.2f} mm above the board's top face, less than the {need_mm:.2f} mm "
            "needed: it is missing, not loaded, flat, or sunk into the board",
        )
    else:
        check.update(
            status="pass",
            detail=f"the model rises {rise_mm:.2f} mm above the top face and {sink_mm:.2f} mm below the bottom face "
            "(height profile only: sideways shift and rotation are for the human to judge in the renders)",
        )
    return check, evidence


# ---------------------------------------------------------------------------- building


def _set_property(symbol: SExp, key: str, value: str) -> None:
    """Set property ``key`` to the string ``value`` on a symbol node: replace the value of the
    existing one, else add a hidden one after the last property."""
    for prop in _children(symbol, "property"):
        if prop.get_string(0) == key:
            write_layer.set_string(prop, 1, value)
            return
    prop = SExp.list("property")
    prop.append(write_layer.string(key))
    prop.append(write_layer.string(value))
    at = SExp.list("at", 0, 0, 0)
    font = SExp.list("font", SExp.list("size", 1.27, 1.27))
    effects = SExp.list("effects", font, SExp.list("hide", write_layer.symbol("yes")))
    prop.append(at)
    prop.append(effects)
    existing = _children(symbol, "property")
    last = next((i for i, c in enumerate(symbol.children) if existing and c is existing[-1]), None)
    symbol.insert(len(symbol.children) if last is None else last + 1, prop)


def _registered(project: Path, path: Path, kind: str, nickname: str) -> str:
    result = lib._register_one(project, path, kind, nickname)
    if result.get("status") not in ("ok", "already_present"):
        raise checkpoint.Failed(
            "error", f"the {kind} library table was not changed: {result.get('reason')}", table_result=result
        )
    if result.get("disabled"):
        raise checkpoint.Failed(
            "error", f"the library row {result['nickname']!r} is disabled in the project's table: enable it first"
        )
    return str(result["nickname"])


def _write(src: Path, tmp: Path, payload: dict) -> dict:
    """Copy the project into ``tmp``, add the library files, rewrite the model paths, set the
    properties and register the library in both tables. Returns what ``_verify`` compares."""
    tmp_pcb = checkpoint.copy_project(src, tmp, max_bytes=gate._MAX_COPY_BYTES)
    paths = _library_paths(tmp, payload["library_name"], payload["library_dir"])
    paths["footprints"].mkdir(parents=True, exist_ok=True)
    paths["models"].mkdir(parents=True, exist_ok=True)
    sym_nick = _registered(tmp, paths["symbols"], "sym", payload["library_name"])
    fp_nick = _registered(tmp, paths["footprints"], "fp", payload["library_name"])
    rel = paths["models"].relative_to(tmp).as_posix()

    first_root: SExp | None = None
    sym_root = write_layer.load(paths["symbols"]) if paths["symbols"].exists() else None
    expected: list[dict] = []
    for part in payload["parts"]:
        sym_src = _one_symbol(Path(part["sources"]["symbol"]))
        fp_src = _one_footprint(Path(part["sources"]["footprint"]))
        if isinstance(sym_src, str) or isinstance(fp_src, str):
            raise checkpoint.Failed("error", f"a part file changed since the proposal: {sym_src if isinstance(sym_src, str) else fp_src}")
        src_root, symbol = sym_src
        fp_root, fp_name = fp_src
        first_root = first_root or src_root
        model_name = part["model"]
        shutil.copyfile(part["sources"]["model"], paths["models"] / model_name)
        uri = f"{URI_PREFIX}{rel}/{model_name}"
        nodes = _model_nodes(fp_root)
        if nodes:
            write_layer.set_string(nodes[0], 0, uri)
        else:
            model = SExp.list("model")
            model.append(write_layer.string(uri))
            model.append(SExp.list("offset", SExp.list("xyz", 0, 0, 0)))
            model.append(SExp.list("scale", SExp.list("xyz", 1, 1, 1)))
            model.append(SExp.list("rotate", SExp.list("xyz", 0, 0, 0)))
            fp_root.append(model)
        write_layer.save(fp_root, paths["footprints"] / f"{fp_name}.kicad_mod")
        props = {"Footprint": f"{fp_nick}:{fp_name}", **part["properties"]}
        before = _properties(symbol)
        for key, value in props.items():
            _set_property(symbol, key, value)
        if sym_root is None:
            sym_root = SExp.list(
                "kicad_symbol_lib", *[c for c in src_root.children if c.is_atom or c.name != "symbol"]
            )
        sym_root.append(symbol)
        expected.append(
            {
                "lcsc_id": part["lcsc_id"],
                "symbol": part["symbol"],
                "footprint": fp_name,
                "model_uri": uri,
                "model_file": paths["models"] / model_name,
                "model_source": part["sources"]["model"],
                "properties": props,
                "other_properties": {k: v for k, v in before.items() if k not in props},
                "pins": _pins(symbol),
                "pads": _pad_numbers(fp_root),
            }
        )
    assert sym_root is not None
    write_layer.save(sym_root, paths["symbols"])
    return {
        "tmp": tmp,
        "pcb": tmp_pcb,
        "paths": paths,
        "sym_nick": sym_nick,
        "fp_nick": fp_nick,
        "library_dir": payload["library_dir"],
        "library_name": payload["library_name"],
        "expected": expected,
    }


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expand(uri: str, project: Path) -> Path | None:
    """The file a ``${KIPRJMOD}/...`` path names inside ``project``; None for any other shape."""
    if not uri.startswith(URI_PREFIX):
        return None
    rest = uri[len(URI_PREFIX) :]
    parts = rest.replace("\\", "/").split("/")
    if not rest or any(p in ("", ".", "..") or ":" in p for p in parts):
        return None
    return project.joinpath(*parts)


def _verify(state: dict, render: Callable[[Path, Path, str], dict]) -> dict:
    """From a fresh reload of the saved files: first the self-check (what was written is what
    was intended: ``NotVerified``), then the gate checks of every part (``Failed``)."""
    tmp: Path = state["tmp"]
    paths = state["paths"]
    mismatches: list[dict] = []
    try:
        reloaded = write_layer.load(paths["symbols"])
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.NotVerified(
            f"the saved symbol library does not reload: {exc}",
            [checkpoint.mismatch(paths["symbols"].name, "symbol library", "a library that parses", str(exc))],
        ) from exc
    symbols = {str(s.get_string(0)): s for s in _children(reloaded, "symbol")}
    for want in state["expected"]:
        name = want["symbol"]
        symbol = symbols.get(name)
        if symbol is None:
            mismatches.append(checkpoint.mismatch(paths["symbols"].name, f"symbol {name}", "present", "missing"))
            continue
        have = _properties(symbol)
        for key, value in want["properties"].items():
            if have.get(key) != value:
                mismatches.append(checkpoint.mismatch(paths["symbols"].name, f"{name} property {key}", value, have.get(key)))
        for key, value in want["other_properties"].items():
            if have.get(key) != value:
                mismatches.append(checkpoint.mismatch(paths["symbols"].name, f"{name} property {key} (unchanged)", value, have.get(key)))
        if _pins(symbol) != want["pins"]:
            mismatches.append(checkpoint.mismatch(paths["symbols"].name, f"{name} pins", want["pins"], _pins(symbol)))
        fp_file = paths["footprints"] / f"{want['footprint']}.kicad_mod"
        try:
            fp_root = write_layer.load(fp_file)
        except Exception as exc:  # noqa: BLE001
            mismatches.append(checkpoint.mismatch(fp_file.name, "footprint", "a file that parses", str(exc)))
            continue
        if _model_uri(fp_root) != want["model_uri"]:
            mismatches.append(checkpoint.mismatch(fp_file.name, "model path", want["model_uri"], _model_uri(fp_root)))
        if _pad_numbers(fp_root) != want["pads"]:
            mismatches.append(checkpoint.mismatch(fp_file.name, "pad numbers", want["pads"], _pad_numbers(fp_root)))
        model_file: Path = want["model_file"]
        if not model_file.is_file() or _sha(model_file) != _sha(Path(want["model_source"])):
            mismatches.append(checkpoint.mismatch(model_file.name, "model file bytes", "equal to the source model", "missing or different"))
    if mismatches:
        raise checkpoint.NotVerified(
            "CRITICAL: the saved library is not what was intended", mismatches
        )

    nicks = {"fp": state["fp_nick"], "sym": state["sym_nick"]}
    table_problems: list[str] = []
    for kind, nick in nicks.items():
        table = tmp / lib._TABLE_FILENAME[kind]
        try:
            rows = {e["name"]: e["uri"] for e in lib._parse_table(table, kind)}
        except lib._TableUnreadable as exc:
            table_problems.append(f"{table.name} does not parse: {exc.problem}")
            continue
        if nick not in rows or not str(rows[nick]).startswith(URI_PREFIX):
            table_problems.append(f"{table.name} has no ${{KIPRJMOD}}-relative row {nick!r}")
    if table_problems:
        raise checkpoint.NotVerified(
            "CRITICAL: a library table is not what was intended",
            [checkpoint.mismatch("lib tables", "row", "present, project-relative", "; ".join(table_problems))],
        )

    parts_out: list[dict] = []
    for want in state["expected"]:
        symbol = symbols[want["symbol"]]
        fp_file = paths["footprints"] / f"{want['footprint']}.kicad_mod"
        fp_root = write_layer.load(fp_file)
        checks: list[dict] = []

        uri = _model_uri(fp_root) or ""
        target = _expand(uri, tmp)
        in_models = target is not None and target.parent.resolve() == paths["models"].resolve()
        checks.append(
            {
                "check": "model_path_project_relative",
                "status": "pass" if in_models else "fail",
                "detail": uri if in_models else f"model path {uri!r} is not a ${{KIPRJMOD}}/... path inside {paths['models'].name}",
            }
        )
        present = target is not None and target.is_file() and target.stat().st_size > 0
        checks.append(
            {
                "check": "model_file_present",
                "status": "pass" if present else "fail",
                "detail": f"{want['model_file'].name} is in the revision" if present else f"no non-empty file at {uri!r}",
            }
        )
        have = _properties(symbol)
        missing = [k for k in MANDATORY if have.get(k, "") in _NOT_SET]
        footprint_ref = have.get("Footprint", "")
        nick, _, fp_name = footprint_ref.partition(":")
        if not missing and (nick != state["fp_nick"] or not (paths["footprints"] / f"{fp_name}.kicad_mod").is_file()):
            missing.append("Footprint (names no footprint in the project's library)")
        if have.get("LCSC") != want["lcsc_id"]:
            missing.append("LCSC (differs from the part's id)")
        if not _VERSION.search(have.get("Source", "")) or " " not in have.get("Source", "").strip():
            missing.append("Source (name and version)")
        checks.append(
            {
                "check": "mandatory_properties",
                "status": "fail" if missing else "pass",
                "detail": f"not set or not valid: {missing}" if missing else f"{', '.join(MANDATORY)} are set",
            }
        )
        render_check, evidence = _render_check(tmp, want["lcsc_id"], fp_file, render)
        checks.append(render_check)
        parts_out.append(
            {
                "lcsc_id": want["lcsc_id"],
                "symbol": f"{state['sym_nick']}:{want['symbol']}",
                "footprint": f"{state['fp_nick']}:{want['footprint']}",
                "model": uri,
                "checks": checks,
                "evidence": evidence,
            }
        )
    failed = [(p["lcsc_id"], c) for p in parts_out for c in p["checks"] if c["status"] == "fail"]
    unrun = [(p["lcsc_id"], c) for p in parts_out for c in p["checks"] if c["status"] == "incomplete"]
    if failed or unrun:
        status = "failed" if failed else "incomplete"
        listing = [{"lcsc_id": i, "check": c["check"], "status": c["status"], "detail": c["detail"]} for i, c in failed + unrun]
        raise checkpoint.Failed(
            status,
            f"{len(failed)} check(s) failed and {len(unrun)} could not run: no revision was made",
            parts=parts_out,
            problems=listing,
            note=(
                "nothing was moved into place and the source project is untouched. A failed check names what "
                "to fix in the part's files, then propose again; an incomplete check could not run (it is "
                "never counted as clean), so fix what it names and call confirm_adopt_parts again."
            ),
        )
    return {
        "parts": parts_out,
        "libraries": {
            "symbols": f"{state['sym_nick']} ({paths['symbols'].relative_to(tmp).as_posix()})",
            "footprints": f"{state['fp_nick']} ({paths['footprints'].relative_to(tmp).as_posix()})",
            "models": paths["models"].relative_to(tmp).as_posix(),
        },
        "evidence_dir": EVIDENCE_DIR,
    }


def confirm_adopt_parts(
    pcb_path: str | Path,
    revision_token: str,
    render: Callable[[Path, Path, str], dict] | None = None,
) -> dict:
    """Apply a proposal. ``render`` is for the module's own tests: no registered tool forwards it."""
    src = Path(pcb_path)
    payload, refused = _PENDING.claim(revision_token, src, drift_reason=_DRIFT)
    if refused:
        return refused
    out_dir = Path(payload["out_dir"])
    problem = checkpoint.output_dir_problem(src, out_dir, what="the adopted project")
    if problem:
        return {"status": "error", "reason": problem}
    renderer = render or kicad_cli_render
    built = checkpoint.build_revision(
        out_dir,
        write=lambda tmp: _write(src, tmp, payload),
        verify=lambda tmp, state: _verify(state, renderer),
        success="adopted",
        before_verified="adopting the parts failed before a revision was verified",
    )
    if built["status"] != "adopted":
        return built
    _PENDING.spend(revision_token)
    for part in built["parts"]:
        for key in ("front", "front_without_model", "top"):
            if part["evidence"].get(key):
                part["evidence"][key] = str(out_dir / part["evidence"][key])
    return {
        **built,
        "output_pcb": str(out_dir / src.name),
        "output_dir": str(out_dir),
        "note": (
            "every machine check passed on the saved files. Open the renders in evidence_dir: they show the "
            "model's height profile and a top view, and a sideways shift or rotation of the model is for you "
            "to judge there, not checked here."
        ),
    }
