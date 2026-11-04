"""Adopt parts into a project-local library (build ticket 38): propose and confirm, the
machine-checked gate (model path, model file, mandatory properties, side-view render), the
self-check and the checkpoint contract.

Fixtures are built through ``kicad_tools``' object model and saved through the write layer; the
3D models are VRML text (not a KiCad file). The render is the real ``kicad-cli pcb render``; the
only seam is the ``render=`` argument of ``part_adoption.confirm_adopt_parts``, which no
registered tool forwards (guarded below).
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from kicad_tools.schema import PCB
from kicad_tools.sexp import SExp
from mcp.server.mcpserver import MCPServer

from kicad_mcp import part_adoption as pa
from kicad_mcp import write_layer
from kicad_mcp.sourcing.footprint_gen import generate_footprint
from kicad_mcp.toolsets import library as library_toolset


def _cli_available() -> bool:
    try:
        from kicad_mcp.kicad_cli import find_kicad_cli

        find_kicad_cli()
        return True
    except Exception:  # noqa: BLE001
        return False


needs_cli = pytest.mark.skipif(not _cli_available(), reason="kicad-cli not installed")

BOX = (
    "#VRML V2.0 utf8\nTransform {{ translation 0 0 {z} children [ Shape {{ appearance Appearance {{ material "
    "Material {{ diffuseColor 0.8 0.1 0.1 }} }} geometry Box {{ size 1 0.5 {h} }} }} ] }}\n"
)
EMPTY_MODEL = "#VRML V2.0 utf8\nGroup { children [ ] }\n"


def _q(value: str) -> SExp:
    return write_layer.string(value)


def _prop(key: str, value: str) -> SExp:
    return SExp.list(
        "property", _q(key), _q(value), SExp.list("at", 0, 0, 0),
        SExp.list("effects", SExp.list("font", SExp.list("size", 1.27, 1.27))),
    )  # fmt: skip


def _pin(number: str) -> SExp:
    return SExp.list(
        "pin", write_layer.symbol("passive"), write_layer.symbol("line"), SExp.list("at", 0, 0, 0),
        SExp.list("length", 2.54), SExp.list("name", _q("~")), SExp.list("number", _q(number)),
    )  # fmt: skip


def _symbol_file(path: Path, name: str, *, datasheet: str = "https://example.test/ds.pdf") -> Path:
    symbol = SExp.list(
        "symbol", _q(name),
        _prop("Reference", "R"), _prop("Value", name), _prop("Footprint", ""), _prop("Datasheet", datasheet),
        SExp.list("symbol", _q(f"{name}_1_1"), _pin("1"), _pin("2")),
    )  # fmt: skip
    root = SExp.list(
        "kicad_symbol_lib", SExp.list("version", 20241209), SExp.list("generator", _q("kicad_symbol_editor")), symbol
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_layer.save(root, path)
    return path


def _part_assets(tmp: Path, lcsc: str, *, model_text: str | None = None, name: str | None = None) -> dict:
    """What fetch_part_assets leaves for one part: a symbol file, a .pretty directory with one
    footprint (carrying an absolute model path outside the project) and a .3dshapes directory."""
    name = name or f"R_{lcsc}"
    base = tmp / "fetched" / lcsc
    sym = _symbol_file(Path(f"{base}.kicad_sym"), name)
    fp_dir = Path(f"{base}.pretty")
    fp_dir.mkdir(parents=True)
    assert generate_footprint("chip", fp_dir / f"{name}.kicad_mod", size="0402")["status"] == "ok"
    root = write_layer.load(fp_dir / f"{name}.kicad_mod")
    root.children[0] = _q(name)
    model_dir = Path(f"{base}.3dshapes")
    model_dir.mkdir(parents=True)
    (model_dir / f"{name}.wrl").write_text(model_text or BOX.format(z=0.2, h=0.4), encoding="utf-8")
    model = SExp.list("model", _q(str(model_dir / f"{name}.wrl")), SExp.list("offset", SExp.list("xyz", 0, 0, 0)))
    root.append(model)
    (fp_dir / f"{name}.kicad_mod").unlink()
    write_layer.save(root, fp_dir / f"{name}.kicad_mod")
    return {"lcsc_id": lcsc, "symbol_path": str(sym), "footprint_dir": str(fp_dir), "model_3d_dir": str(model_dir)}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    board = PCB.create(width=40.0, height=30.0, title="adopt")
    path = tmp_path / "proj" / "adopt.kicad_pcb"
    path.parent.mkdir()
    write_layer.save_pcb(board, path)
    return path


def _digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }  # fmt: skip


COMMON = {"license": "CC-BY-SA-4.0 (as the user states)", "source": "easyeda2kicad", "source_version": "1.0.1"}


def _propose(project: Path, parts: list[dict], out: Path, **kwargs) -> dict:
    return pa.propose_adopt_parts(project, parts, output_dir=out, **{**COMMON, **kwargs})


def _props_of(sym_file: Path, name: str) -> dict[str, str]:
    root = write_layer.load(sym_file)
    symbol = next(s for s in root.children if not s.is_atom and s.name == "symbol" and s.get_string(0) == name)
    return pa._properties(symbol)


# --------------------------------------------------------------------------- propose


def test_propose_writes_nothing_and_lists_the_batch(project: Path, tmp_path: Path) -> None:
    parts = [_part_assets(tmp_path, "C100"), _part_assets(tmp_path, "C200")]
    before = _digest(project.parent)
    out = tmp_path / "rev"
    result = _propose(project, parts, out)
    assert result["status"] == "proposed", result
    assert [p["status"] for p in result["parts"]] == ["ready", "ready"]
    assert result["revision_token"]
    assert _digest(project.parent) == before
    assert not out.exists()


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"license": None}, "License is missing"),
        ({"source_version": "stable"}, "needs a version"),
        ({"datasheet": "~"}, "Datasheet is missing"),
    ],
)
def test_a_missing_mandatory_property_blocks_the_proposal(project: Path, tmp_path: Path, change, needle) -> None:
    part = _part_assets(tmp_path, "C100")
    sym = Path(part["symbol_path"])
    _symbol_file(sym, "R_C100", datasheet="~")  # the symbol has no datasheet either
    kwargs = {k: v for k, v in change.items() if k in ("license", "source_version")}
    if "datasheet" in change:
        part["datasheet"] = "~"
    result = _propose(project, [part], tmp_path / "rev", **kwargs)
    assert result["status"] == "blocked"
    assert result["revision_token"] is None
    assert any(needle in problem for problem in result["parts"][0]["problems"]), result


def test_a_part_without_a_model_is_not_adopted(project: Path, tmp_path: Path) -> None:
    part = _part_assets(tmp_path, "C100")
    del part["model_3d_dir"]
    result = _propose(project, [part, _part_assets(tmp_path, "C200")], tmp_path / "rev")
    assert result["status"] == "blocked"
    assert result["revision_token"] is None
    assert result["parts"][1]["status"] == "ready"  # the good one is listed, but nothing half-adopts
    assert any("3D model is required" in p for p in result["parts"][0]["problems"])


def test_bad_inputs_are_named(project: Path, tmp_path: Path) -> None:
    good = _part_assets(tmp_path, "C100")
    assert pa.propose_adopt_parts(project, [], license="x")["status"] == "error"
    assert _propose(project, [good], tmp_path / "r", library_name="a/b")["status"] == "error"
    assert _propose(project, [good], tmp_path / "r", library_dir="../out")["status"] == "error"
    dup = _propose(project, [good, dict(good)], tmp_path / "r")
    assert dup["status"] == "blocked"
    assert any("more than once" in p for part in dup["parts"] for p in part["problems"])
    bad_id = _propose(project, [{**good, "lcsc_id": "C:/x"}], tmp_path / "r")
    assert bad_id["status"] == "blocked"
    assert _propose(project, [good], project.parent / "inside")["status"] == "error"
    assert pa.propose_adopt_parts(tmp_path / "nope.kicad_pcb", [good], **COMMON)["status"] == "incomplete"


# --------------------------------------------------------------------------- confirm


@needs_cli
def test_confirm_adopts_a_batch_with_every_check_passing(project: Path, tmp_path: Path) -> None:
    parts = [_part_assets(tmp_path, "C100"), _part_assets(tmp_path, "C200")]
    before = _digest(project.parent)
    out = tmp_path / "rev"
    proposed = _propose(project, parts, out)
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "adopted", result
    assert _digest(project.parent) == before  # the source is never touched

    for part in result["parts"]:
        assert {c["check"]: c["status"] for c in part["checks"]} == {
            "model_path_project_relative": "pass",
            "model_file_present": "pass",
            "mandatory_properties": "pass",
            "side_view_render": "pass",
        }, part
        assert part["evidence"]["body_above_board_mm"] >= 0.1
        for key in ("front", "front_without_model", "top"):
            assert Path(part["evidence"][key]).is_file()

    lib = out / "lib"
    props = _props_of(lib / "project_parts.kicad_sym", "R_C100")
    assert props["Footprint"] == "project_parts:R_C100"
    assert props["LCSC"] == "C100"
    assert props["Source"] == "easyeda2kicad 1.0.1"
    assert props["License"] == COMMON["license"]
    assert props["Datasheet"] == "https://example.test/ds.pdf"
    assert set(_props_of(lib / "project_parts.kicad_sym", "R_C200")) >= set(pa.MANDATORY)
    footprint = write_layer.load(lib / "project_parts.pretty" / "R_C100.kicad_mod")
    assert pa._model_uri(footprint) == "${KIPRJMOD}/lib/project_parts.3dshapes/R_C100.wrl"
    assert (lib / "project_parts.3dshapes" / "R_C100.wrl").is_file()
    assert "${KIPRJMOD}/lib/project_parts.pretty" in (out / "fp-lib-table").read_text(encoding="utf-8")
    assert "${KIPRJMOD}/lib/project_parts.kicad_sym" in (out / "sym-lib-table").read_text(encoding="utf-8")
    assert not list(out.glob(".adopt_*"))  # the scratch boards are gone
    # the token is used up
    again = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert again["status"] == "error"


@needs_cli
def test_a_second_batch_extends_the_library_and_a_repeat_has_nothing_to_do(project: Path, tmp_path: Path) -> None:
    first_assets = _part_assets(tmp_path, "C100")
    first = _propose(project, [first_assets], tmp_path / "rev1")
    assert pa.confirm_adopt_parts(project, first["revision_token"])["status"] == "adopted"
    rev1 = tmp_path / "rev1" / project.name
    second_assets = _part_assets(tmp_path, "C200")
    second = _propose(rev1, [first_assets, second_assets], tmp_path / "rev2")
    assert second["status"] == "proposed", second
    assert [p["status"] for p in second["parts"]] == ["already_adopted", "ready"]
    assert pa.confirm_adopt_parts(rev1, second["revision_token"])["status"] == "adopted"
    symbols = write_layer.load(tmp_path / "rev2" / "lib" / "project_parts.kicad_sym")
    assert sorted(s.get_string(0) for s in symbols.children if not s.is_atom and s.name == "symbol") == [
        "R_C100", "R_C200",
    ]  # fmt: skip
    rev2 = tmp_path / "rev2" / project.name
    again = _propose(rev2, [first_assets, second_assets], tmp_path / "rev3")
    assert again["status"] == "nothing_to_do"
    assert [p["status"] for p in again["parts"]] == ["already_adopted", "already_adopted"]


@needs_cli
def test_nothing_in_the_library_is_overwritten(project: Path, tmp_path: Path) -> None:
    first = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev1")
    assert pa.confirm_adopt_parts(project, first["revision_token"])["status"] == "adopted"
    other = _part_assets(tmp_path, "C300", name="R_C100")  # another part, same symbol and footprint name
    result = _propose(tmp_path / "rev1" / project.name, [other], tmp_path / "rev2")
    assert result["status"] == "blocked"
    problems = " ".join(result["parts"][0]["problems"])
    assert "already has a symbol" in problems and "already exists" in problems


@needs_cli
def test_confirm_refuses_when_the_project_changed_in_between(project: Path, tmp_path: Path) -> None:
    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")
    _symbol_file(project.parent / "other.kicad_sym", "OTHER")  # a library file appears in the project
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "error"
    assert "changed since the proposal" in result["reason"]
    assert not (tmp_path / "rev").exists()


@needs_cli
def test_confirm_refuses_when_a_part_file_changed_in_between(project: Path, tmp_path: Path) -> None:
    part = _part_assets(tmp_path, "C100")
    proposed = _propose(project, [part], tmp_path / "rev")
    model = next(Path(part["model_3d_dir"]).iterdir())
    model.write_text(BOX.format(z=0.2, h=0.8), encoding="utf-8")
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "error"
    assert not (tmp_path / "rev").exists()


@needs_cli
def test_corrupted_write_is_caught_by_reload_verification(project: Path, tmp_path: Path, monkeypatch) -> None:
    """The self-check fires: the symbol library is saved without its Source property."""
    original = write_layer.save

    def save_then_corrupt(root, path, **kwargs):
        if root.name == "kicad_symbol_lib":
            for symbol in (c for c in root.children if not c.is_atom and c.name == "symbol"):
                for prop in pa._children(symbol, "property"):
                    if prop.get_string(0) == "Source":
                        symbol.remove(prop)
        return original(root, path, **kwargs)

    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")
    monkeypatch.setattr(write_layer, "save", save_then_corrupt)
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "error"
    assert result["code"] == "write_not_verified"
    assert any("Source" in m["item"] for m in result["mismatches"]["items"])
    assert not (tmp_path / "rev").exists()
    assert not list(tmp_path.glob(".rev.tmp-*"))


@needs_cli
def test_a_model_copied_wrongly_is_caught(project: Path, tmp_path: Path, monkeypatch) -> None:
    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")
    monkeypatch.setattr(pa, "shutil", SimpleNamespace(copyfile=lambda src, dst: Path(dst).write_bytes(b"not the model")))
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["code"] == "write_not_verified"
    assert any(m["item"] == "model file bytes" for m in result["mismatches"]["items"])


@needs_cli
def test_a_model_with_no_body_fails_the_render_check(project: Path, tmp_path: Path) -> None:
    part = _part_assets(tmp_path, "C100", model_text=EMPTY_MODEL)
    proposed = _propose(project, [part], tmp_path / "rev")
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "failed", result
    assert [(p["check"], p["status"]) for p in result["problems"]] == [("side_view_render", "fail")]
    assert not (tmp_path / "rev").exists()
    assert not list(tmp_path.glob(".rev.tmp-*"))
    # a failed check keeps the token: the revision can be asked for again once the files are fixed
    assert pa._PENDING._entries


@needs_cli
def test_a_model_below_the_board_fails_the_render_check(project: Path, tmp_path: Path) -> None:
    part = _part_assets(tmp_path, "C100", model_text=BOX.format(z=-1.0, h=0.4))
    proposed = _propose(project, [part], tmp_path / "rev")
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "failed", result
    assert "below the board" in result["problems"][0]["detail"]


def test_a_render_that_cannot_run_is_incomplete_never_clean(project: Path, tmp_path: Path) -> None:
    if not _cli_available():
        pytest.skip("kicad-cli not installed")  # the write layer's parse check needs it
    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")

    def no_render(board, png, side):
        return {"status": "incomplete", "reason": "no OpenGL here"}

    result = pa.confirm_adopt_parts(project, proposed["revision_token"], render=no_render)
    assert result["status"] == "incomplete"
    assert result["problems"][0]["check"] == "side_view_render"
    assert "no OpenGL" in result["problems"][0]["detail"]
    assert not (tmp_path / "rev").exists()


@needs_cli
def test_an_unreadable_render_is_incomplete(project: Path, tmp_path: Path) -> None:
    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")

    def junk(board, png, side):
        png.write_bytes(b"not a png")
        return {"status": "ok"}

    result = pa.confirm_adopt_parts(project, proposed["revision_token"], render=junk)
    assert result["status"] == "incomplete"
    assert "cannot be measured" in result["problems"][0]["detail"]


# --------------------------------------------------------------------------- the PNG reader


def _png(width: int, height: int, rows: list[bytes], filters: list[int]) -> bytes:
    """An 8-bit RGBA PNG whose row ``y`` carries ``rows[y]`` (raw RGBA) written with filter ``filters[y]``."""
    stride = width * 4
    out = bytearray()
    previous = bytes(stride)
    for row, kind in zip(rows, filters, strict=True):
        out.append(kind)
        for i, byte in enumerate(row):
            a = row[i - 4] if i >= 4 else 0
            b = previous[i]
            c = previous[i - 4] if i >= 4 else 0
            if kind == 0:
                pred = 0
            elif kind == 1:
                pred = a
            elif kind == 2:
                pred = b
            elif kind == 3:
                pred = (a + b) >> 1
            else:
                p = a + b - c
                pa_, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa_ <= pb and pa_ <= pc else (b if pb <= pc else c)
            out.append((byte - pred) & 255)
        previous = row

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(out))) + chunk(b"IEND", b"")


@pytest.mark.parametrize("kind", [0, 1, 2, 3, 4])
def test_png_reader_finds_the_silhouette_rows_for_every_filter(tmp_path: Path, kind: int) -> None:
    width, height = 6, 8
    rows = []
    for y in range(height):
        pixel = bytes([40 + y * 7, 90, 200 - y * 9, 255 if 2 <= y <= 5 else 0])
        rows.append(pixel * width)
    path = tmp_path / "x.png"
    path.write_bytes(_png(width, height, rows, [kind] * height))
    assert pa._silhouette(path) == (2, 5)


def test_png_reader_refuses_what_it_cannot_measure(tmp_path: Path) -> None:
    opaque = tmp_path / "o.png"
    opaque.write_bytes(_png(2, 3, [bytes([1, 2, 3, 255]) * 2] * 3, [0, 0, 0]))
    with pytest.raises(pa._Unreadable, match="no transparent background"):
        pa._silhouette(opaque)
    empty = tmp_path / "e.png"
    empty.write_bytes(_png(2, 3, [bytes(8)] * 3, [0, 0, 0]))
    with pytest.raises(pa._Unreadable, match="empty"):
        pa._silhouette(empty)
    rgb = tmp_path / "rgb.png"
    data = bytearray(_png(2, 3, [bytes(8)] * 3, [0, 0, 0]))
    data[25] = 2  # IHDR colour type: RGB
    rgb.write_bytes(bytes(data))
    with pytest.raises(pa._Unreadable):
        pa._silhouette(rgb)


def test_png_reader_agrees_with_pillow_on_a_real_image(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL.Image")
    image = pil.new("RGBA", (50, 40), (0, 0, 0, 0))
    for y in range(11, 29):
        for x in range(5, 45):
            image.putpixel((x, y), (200, 10, y * 3 % 255, 255))
    path = tmp_path / "p.png"
    image.save(path)
    assert pa._silhouette(path) == (11, 28)


# --------------------------------------------------------------------------- the tools


def _tools() -> dict:
    mcp = MCPServer("test")
    names = library_toolset.build(mcp)
    return names, {t.name: t for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def test_the_library_toolset_registers_the_pair_and_no_seam() -> None:
    names, tools = _tools()
    assert {"propose_adopt_parts", "confirm_adopt_parts"} <= set(names)
    for name in ("propose_adopt_parts", "confirm_adopt_parts"):
        props = set(tools[name].parameters["properties"])
        assert "render" not in props and not any("kwargs" in p for p in props), name
    assert tools["propose_adopt_parts"].annotations.read_only_hint is True
    assert tools["confirm_adopt_parts"].annotations.destructive_hint is True
    assert tools["confirm_adopt_parts"].annotations.read_only_hint is False


def test_no_registered_tool_forwards_a_render_seam() -> None:
    import ast

    source = (Path(library_toolset.__file__)).read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)]
    forwarding = [
        c for c in calls
        if isinstance(c.func, ast.Attribute) and c.func.attr in ("propose_adopt_parts", "confirm_adopt_parts")
    ]  # fmt: skip
    assert len(forwarding) == 2  # the guard really saw both calls
    for call in forwarding:
        assert all(kw.arg not in ("render", None) for kw in call.keywords)


@needs_cli
def test_the_tools_adopt_through_the_mcp_functions(project: Path, tmp_path: Path) -> None:
    _, tools = _tools()
    part = _part_assets(tmp_path, "C100")
    proposed = tools["propose_adopt_parts"].fn(
        pcb_path=str(project), parts=[part], output_dir=str(tmp_path / "rev"), **COMMON
    )
    assert proposed["status"] == "proposed"
    done = tools["confirm_adopt_parts"].fn(pcb_path=str(project), revision_token=proposed["revision_token"])
    assert done["status"] == "adopted"
    assert done["pcb_path"] == str(tmp_path / "rev" / project.name)  # the gate field marks the revision
    assert done["source_pcb"] == str(project)


# --------------------------------------------------------------------------- guards the self-check cannot see
# The self-check compares the saved files with what _write intended. To reach the gate checks, these
# tests make _write intend the wrong thing, consistently, so the self-check passes and only the gate fires.


def _rewrite_model_path(monkeypatch, make_uri) -> None:
    real_write = pa._write

    def write_wrong(src, tmp, payload):
        state = real_write(src, tmp, payload)
        for want in state["expected"]:
            fp_file = state["paths"]["footprints"] / f"{want['footprint']}.kicad_mod"
            root = write_layer.load(fp_file)
            uri = make_uri(state, want)
            write_layer.set_string(pa._model_nodes(root)[0], 0, uri)
            write_layer.save(root, fp_file)
            want["model_uri"] = uri
        return state

    monkeypatch.setattr(pa, "_write", write_wrong)


def _failed_checks(result: dict) -> list[tuple[str, str]]:
    return [(p["check"], p["status"]) for p in result["problems"]]


@needs_cli
def test_a_model_path_outside_the_models_directory_fails_the_path_check(project: Path, tmp_path: Path, monkeypatch) -> None:
    def elsewhere(state, want):
        other = state["tmp"] / "elsewhere"
        other.mkdir(exist_ok=True)
        (other / "m.wrl").write_bytes(want["model_file"].read_bytes())  # the file exists: only the location is wrong
        return "${KIPRJMOD}/elsewhere/m.wrl"

    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")
    _rewrite_model_path(monkeypatch, elsewhere)
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "failed", result
    assert ("model_path_project_relative", "fail") in _failed_checks(result)
    assert ("model_file_present", "fail") not in _failed_checks(result)
    assert not (tmp_path / "rev").exists()


@needs_cli
def test_a_model_path_to_a_missing_file_fails_the_file_check(project: Path, tmp_path: Path, monkeypatch) -> None:
    def missing(state, want):
        return "${KIPRJMOD}/" + want["model_file"].parent.relative_to(state["tmp"]).as_posix() + "/absent.wrl"

    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")
    _rewrite_model_path(monkeypatch, missing)
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "failed", result
    assert ("model_file_present", "fail") in _failed_checks(result)
    assert ("model_path_project_relative", "fail") not in _failed_checks(result)
    assert not (tmp_path / "rev").exists()


@needs_cli
def test_a_symbol_lcsc_that_differs_from_the_part_fails_the_properties_check(project: Path, tmp_path: Path, monkeypatch) -> None:
    real_write = pa._write

    def write_other_id(src, tmp, payload):
        state = real_write(src, tmp, payload)
        state["expected"][0]["lcsc_id"] = "C999"  # the symbol still says C100
        return state

    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")
    monkeypatch.setattr(pa, "_write", write_other_id)
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "failed", result
    problem = next(p for p in result["problems"] if p["check"] == "mandatory_properties")
    assert "LCSC" in problem["detail"]


def test_expand_accepts_only_a_plain_path_below_kiprjmod(tmp_path: Path) -> None:
    assert pa._expand("${KIPRJMOD}/lib/m.wrl", tmp_path) == tmp_path / "lib" / "m.wrl"
    for bad in ("${KIPRJMOD}/../x", "${KIPRJMOD}/lib/../../x", "${KIPRJMOD}/./x", "${KIPRJMOD}/C:/x", "${KIPRJMOD}/", "${KIPRJMOD}//x", "C:/x", "../x", "/x", ""):
        assert pa._expand(bad, tmp_path) is None, bad


def test_expand_refuses_other_shapes_that_are_long_enough_to_slip_past_the_prefix_cut(tmp_path: Path) -> None:
    for bad in ("/abs/long/dir/x/model.wrl", "C:/models/abc/def/model.wrl", "../../../../models/model.wrl"):
        assert pa._expand(bad, tmp_path) is None, bad


def _change_payload(monkeypatch, edit) -> None:
    real_write = pa._write

    def write_edited(src, tmp, payload):
        edit(payload)
        return real_write(src, tmp, payload)

    monkeypatch.setattr(pa, "_write", write_edited)


@needs_cli
def test_a_source_without_a_version_fails_the_properties_check(project: Path, tmp_path: Path, monkeypatch) -> None:
    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")
    _change_payload(monkeypatch, lambda payload: payload["parts"][0]["properties"].update(Source="easyeda2kicad"))
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "failed", result
    assert "Source" in next(p for p in result["problems"] if p["check"] == "mandatory_properties")["detail"]


@needs_cli
@pytest.mark.parametrize("ref", ["elsewhere:Nope", "elsewhere:R_C100"])  # unknown name; right name under another library
def test_a_footprint_property_naming_no_library_footprint_fails_the_properties_check(project: Path, tmp_path: Path, monkeypatch, ref: str) -> None:
    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")
    _change_payload(monkeypatch, lambda payload: payload["parts"][0]["properties"].update(Footprint=ref))
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "failed", result
    assert "Footprint" in next(p for p in result["problems"] if p["check"] == "mandatory_properties")["detail"]


@needs_cli
def test_an_empty_model_file_fails_the_file_check(project: Path, tmp_path: Path, monkeypatch) -> None:
    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")
    empty = tmp_path / "empty.wrl"
    empty.write_bytes(b"")
    _change_payload(monkeypatch, lambda payload: payload["parts"][0]["sources"].update(model=str(empty)))
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "failed", result
    assert ("model_file_present", "fail") in _failed_checks(result)


# --------------------------------------------------------------------------- follow-ups of ticket 38 (issue 50)


def _edit_footprint(part: dict, edit) -> None:
    """Rewrite the one footprint of a ``_part_assets`` part through the write layer."""
    path = next(Path(part["footprint_dir"]).glob("*.kicad_mod"))
    root = write_layer.load(path)
    edit(root)
    path.unlink()
    write_layer.save(root, path)


def test_a_footprint_without_pads_is_rejected_not_waved_through(project: Path, tmp_path: Path) -> None:
    part = _part_assets(tmp_path, "C100")

    def drop_pads(root: SExp) -> None:
        for pad in pa._children(root, "pad"):
            root.remove(pad)

    _edit_footprint(part, drop_pads)
    result = _propose(project, [part], tmp_path / "rev")
    assert result["status"] == "blocked"
    assert result["revision_token"] is None
    assert any("no pads" in p for p in result["parts"][0]["problems"]), result["parts"][0]["problems"]


def _tall_png(width: int, first_row: int, last_row: int, height: int = 240) -> bytes:
    opaque = bytes([200, 0, 0, 255]) * width
    clear = bytes(4 * width)
    rows = [opaque if first_row <= y <= last_row else clear for y in range(height)]
    return _png(width, height, rows, [0] * height)


@needs_cli
def test_a_body_height_clipped_by_the_render_is_reported_as_clipped(project: Path, tmp_path: Path) -> None:
    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")

    def clipped_render(board, png, side):
        name = png.name
        # the bare slab is rows 100..119; with the model the silhouette runs off the top of the image
        data = _tall_png(4, 100, 119) if "without_model" in name else _tall_png(4, 0, 119)
        png.write_bytes(data)
        return {"status": "ok"}

    result = pa.confirm_adopt_parts(project, proposed["revision_token"], render=clipped_render)
    assert result["status"] == "adopted", result
    part = result["parts"][0]
    check = next(c for c in part["checks"] if c["check"] == "side_view_render")
    assert check["status"] == "pass"
    assert "at least" in check["detail"] and "clipped" in check["detail"]
    assert part["evidence"]["body_above_board_clipped"] is True
    assert "body_above_board_mm" not in part["evidence"]  # a lower bound is not a measurement
    assert part["evidence"]["body_above_board_at_least_mm"] > 0


@needs_cli
def test_an_unclipped_body_height_stays_a_measurement(project: Path, tmp_path: Path) -> None:
    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")

    def fine_render(board, png, side):
        png.write_bytes(_tall_png(4, 100, 119) if "without_model" in png.name else _tall_png(4, 90, 119))
        return {"status": "ok"}

    result = pa.confirm_adopt_parts(project, proposed["revision_token"], render=fine_render)
    assert result["status"] == "adopted", result
    evidence = result["parts"][0]["evidence"]
    assert evidence["body_above_board_mm"] > 0
    assert evidence["body_above_board_clipped"] is False
    assert "body_above_board_at_least_mm" not in evidence


def _easyeda_shaped(tmp: Path, lcsc: str) -> dict:
    """Like an easyeda2kicad footprint: the pads sit away from the footprint origin and the
    (model ...) carries an XY offset and a rotation about the vertical axis."""
    part = _part_assets(tmp, lcsc)

    def shape(root: SExp) -> None:
        for pad in pa._children(root, "pad"):
            at = pad.find("at")
            write_layer.set_number(at, 0, float(at.values[0]) + 12.0)
            write_layer.set_number(at, 1, float(at.values[1]) - 15.0)
        model = pa._model_nodes(root)[0]
        model.find("offset").find("xyz").children[:] = [write_layer.number(v) for v in (1.5, -0.8, 0)]
        model.append(SExp.list("rotate", SExp.list("xyz", 0, 0, 90)))

    _edit_footprint(part, shape)
    return part


@needs_cli
def test_an_easyeda_shaped_footprint_with_offset_and_rotation_is_adopted_unchanged(project: Path, tmp_path: Path) -> None:
    part = _easyeda_shaped(tmp_path, "C100")
    proposed = _propose(project, [part], tmp_path / "rev")
    assert proposed["status"] == "proposed", proposed
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "adopted", result
    assert {c["check"]: c["status"] for c in result["parts"][0]["checks"]}["side_view_render"] == "pass"
    saved = write_layer.load(tmp_path / "rev" / "lib" / "project_parts.pretty" / "R_C100.kicad_mod")
    model = pa._model_nodes(saved)[0]
    assert [float(v) for v in model.find("offset").find("xyz").values] == [1.5, -0.8, 0.0]
    assert [float(v) for v in model.find("rotate").find("xyz").values] == [0.0, 0.0, 90.0]
    reach_x, reach_y = pa._pad_extent(saved)
    assert reach_x > 12.0 and reach_y > 15.0  # the offset pads were really read from the raw (at ...)


def test_the_tool_texts_name_the_new_behaviour() -> None:
    tools = _tools()[1]
    assert "no pads" in tools["propose_adopt_parts"].description
    assert "clipped" in tools["confirm_adopt_parts"].description
    assert "cut-off sink" in tools["confirm_adopt_parts"].description and "is incomplete" in tools["confirm_adopt_parts"].description


def _render_with(project: Path, tmp_path: Path, bare: tuple[int, int], with_model: tuple[int, int]) -> dict:
    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")

    def stub(board, png, side):
        rows = bare if "without_model" in png.name else with_model
        png.write_bytes(_tall_png(4, *rows))
        return {"status": "ok"}

    return pa.confirm_adopt_parts(project, proposed["revision_token"], render=stub)


def _render_part(result: dict) -> tuple[dict, dict]:
    part = result["parts"][0]
    return next(c for c in part["checks"] if c["check"] == "side_view_render"), part["evidence"]


@needs_cli
def test_a_clipped_sink_is_reported_as_a_lower_bound(project: Path, tmp_path: Path) -> None:
    # the model runs off the bottom edge of the image: far below the slab, so even the lower bound fails
    result = _render_with(project, tmp_path, (100, 119), (100, 239))
    assert result["status"] == "failed", result
    check, evidence = _render_part(result)
    assert evidence["body_below_board_clipped"] is True
    assert "body_below_board_mm" not in evidence
    assert evidence["body_below_board_at_least_mm"] > 0
    assert "at least" in check["detail"]


@needs_cli
def test_a_clipped_sink_whose_lower_bound_is_small_is_incomplete_never_pass(project: Path, tmp_path: Path) -> None:
    # slab rows 230..238; with the model the silhouette ends on the last row (239): 1 px below the
    # slab is within the limit, but the true sink is unknown
    result = _render_with(project, tmp_path, (230, 238), (200, 239))
    assert result["status"] == "incomplete", result
    check, evidence = _render_part(result)
    assert check["status"] == "incomplete"
    assert evidence["body_below_board_clipped"] is True
    assert "cut off" in check["detail"]


@needs_cli
def test_a_clipped_rise_that_is_too_small_is_incomplete_not_fail(project: Path, tmp_path: Path) -> None:
    # slab rows 1..20; the model reaches row 0: a 1 px rise that is only a lower bound
    result = _render_with(project, tmp_path, (1, 20), (0, 20))
    assert result["status"] == "incomplete", result
    check, evidence = _render_part(result)
    assert evidence["body_above_board_clipped"] is True
    assert "cut off" in check["detail"]


@needs_cli
def test_the_scratch_board_covers_the_offset_pads(tmp_path: Path) -> None:
    part = _easyeda_shaped(tmp_path, "C100")
    footprint_file = next(Path(part["footprint_dir"]).glob("*.kicad_mod"))
    board, _ = pa._scratch_board(tmp_path, footprint_file, ".probe.kicad_pcb", with_model=False)
    root = write_layer.load(board)
    xs, ys = [], []
    for node in pa._children(root, "gr_line"):
        for key in ("start", "end"):
            x, y = pa._floats(node.find(key))
            xs.append(x)
            ys.append(y)
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    fp = pa._children(root, "footprint")[0]
    reach_x, reach_y = pa._pad_extent(fp)
    assert reach_x > 12.0 and reach_y > 15.0
    assert width / 2 >= reach_x and height / 2 >= reach_y  # the footprint sits at the middle


# --------------------------------------------------------------------------- issue 78: what easyeda2kicad delivers

EASYEDA_NAME = "SOP-4_L4.3-W4.4-P2.54-LS7.0-BL"
_EASYEDA_MODULE = """(module easyeda2kicad:{name} (layer F.Cu) (tedit 5DC5F6A4)
  (attr smd)
  (fp_text reference REF** (at 0 -3.5) (layer F.SilkS) (effects (font (size 1 1) (thickness 0.15))))
  (fp_text value {name} (at 0 3.5) (layer F.Fab) (effects (font (size 1 1) (thickness 0.15))))
  (fp_arc (start -1.52 1.52) (end -1.67 1.52) (angle 359.03) (layer F.SilkS) (width 0.3))
  (fp_circle (center -1.9 2.67) (end -1.88 2.67) (layer F.SilkS) (width 0.06))
  (pad 1 smd rect (at -3.5 -1.27) (size 0.9 2) (layers F.Cu F.Paste F.Mask))
  (pad 2 smd rect (at -3.5 1.27) (size 0.9 2) (layers F.Cu F.Paste F.Mask))
  (pad 3 smd rect (at 3.5 1.27) (size 0.9 2) (layers F.Cu F.Paste F.Mask))
  (pad 4 smd rect (at 3.5 -1.27) (size 0.9 2) (layers F.Cu F.Paste F.Mask))
  (model ${{KISYS3DMOD}}/easyeda2kicad.3dshapes/{name}.wrl (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))
)
"""


def _easyeda_delivered(tmp: Path, lcsc: str) -> dict:
    """The shape easyeda2kicad leaves: an old `(module <lib>:<name> ...)` footprint, and a symbol
    whose Footprint property reads `<lcsc>:<name>`; four pads and four pins."""
    part = _part_assets(tmp, lcsc, name="GAQY221S")
    symbol_path = Path(part["symbol_path"])
    sym_root = write_layer.load(symbol_path)
    symbol = pa._children(sym_root, "symbol")[0]
    for extra in ("3", "4"):
        symbol.children[-1].append(_pin(extra))
    write_layer.set_string(next(p for p in pa._children(symbol, "property") if p.get_string(0) == "Footprint"), 1, f"{lcsc}:{EASYEDA_NAME}")
    symbol_path.unlink()
    write_layer.save(sym_root, symbol_path)
    fp_dir = Path(part["footprint_dir"])
    for old in fp_dir.glob("*.kicad_mod"):
        old.unlink()
    model = Path(part["model_3d_dir"]) / "GAQY221S.wrl"
    root = write_layer.parse_string(_EASYEDA_MODULE.format(name=EASYEDA_NAME))
    write_layer.save(root, fp_dir / f"{EASYEDA_NAME}.kicad_mod")
    (model.parent / f"{EASYEDA_NAME}.wrl").write_text(model.read_text(encoding="utf-8"), encoding="utf-8")
    model.unlink()
    return part


def test_the_fixture_has_the_shape_easyeda2kicad_delivers(tmp_path: Path) -> None:
    part = _easyeda_delivered(tmp_path, "C7435105")
    root = write_layer.load(next(Path(part["footprint_dir"]).glob("*.kicad_mod")))
    assert root.name == "module" and root.get_string(0) == f"easyeda2kicad:{EASYEDA_NAME}"
    assert root.find("tedit") is not None
    assert _props_of(Path(part["symbol_path"]), "GAQY221S")["Footprint"] == f"C7435105:{EASYEDA_NAME}"


def test_a_library_prefixed_old_format_footprint_is_proposed_and_the_name_change_is_stated(project: Path, tmp_path: Path) -> None:
    part = _easyeda_delivered(tmp_path, "C7435105")
    proposed = _propose(project, [part], tmp_path / "rev")
    assert proposed["status"] == "proposed", proposed
    entry = proposed["parts"][0]
    assert entry["footprint"] == EASYEDA_NAME
    assert "normalised" in _tools()[1]["propose_adopt_parts"].description
    assert any(
        "easyeda2kicad:" in n and EASYEDA_NAME in n for n in entry["normalised"]
    ), entry["normalised"]


@needs_cli
def test_a_library_prefixed_old_format_footprint_is_adopted_and_the_symbol_points_at_the_chosen_library(project: Path, tmp_path: Path) -> None:
    part = _easyeda_delivered(tmp_path, "C7435105")
    proposed = _propose(project, [part], tmp_path / "rev", library_name="scbe")
    assert proposed["status"] == "proposed", proposed
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "adopted", result
    assert result["parts"][0]["footprint"] == f"scbe:{EASYEDA_NAME}"
    assert result["parts"][0]["normalised"], result["parts"][0]
    assert _props_of(tmp_path / "rev" / "lib" / "scbe.kicad_sym", "GAQY221S")["Footprint"] == f"scbe:{EASYEDA_NAME}"
    saved = tmp_path / "rev" / "lib" / "scbe.pretty" / f"{EASYEDA_NAME}.kicad_mod"
    saved_root = write_layer.load(saved)
    assert saved_root.get_string(0) == EASYEDA_NAME  # no library prefix left in the file
    assert pa._model_uri(saved_root).startswith("${KIPRJMOD}/lib/scbe.3dshapes/")
    assert len(pa._pad_numbers(saved_root)) == 4


def test_a_footprint_name_with_other_bad_characters_is_still_refused(project: Path, tmp_path: Path) -> None:
    part = _easyeda_delivered(tmp_path, "C7435105")

    def rename(root: SExp) -> None:
        write_layer.set_symbol(root, 0, "easyeda2kicad:bad*name")

    _edit_footprint(part, rename)
    result = _propose(project, [part], tmp_path / "rev")
    assert result["status"] == "blocked"
    assert any("cannot be a file name" in p for p in result["parts"][0]["problems"])


@needs_cli
def test_an_old_format_footprint_with_arcs_gets_a_scratch_board_kicad_cli_loads(tmp_path: Path) -> None:
    """Issue 80: an old `(fp_arc (start) (end) (angle))` inside a current board makes kicad-cli
    pcb upgrade fail to parse ("Expected 'mid'") and crash (0xC0000005); the scratch board
    carries the footprint upgraded to the current format instead."""
    part = _easyeda_delivered(tmp_path, "C7435105")
    footprint_file = next(Path(part["footprint_dir"]).glob("*.kicad_mod"))
    for with_model in (True, False):
        board, _ = pa._scratch_board(tmp_path, footprint_file, ".probe.kicad_pcb", with_model=with_model)
        placed = pa._children(write_layer.load(board), "footprint")[0]
        assert placed.name == "footprint"
        assert not pa._children(placed, "fp_arc") or all(a.find("mid") is not None for a in pa._children(placed, "fp_arc"))
        assert len(pa._pad_numbers(placed)) == 4
        assert (placed.find("model") is not None) == with_model
    assert write_layer.load(footprint_file).name == "module"  # the adopted file is untouched


@needs_cli
def test_a_kicad_cli_crash_on_the_scratch_board_is_reported_as_a_crash_not_a_bad_model(project: Path, tmp_path: Path, monkeypatch) -> None:
    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")

    def crash(*args, **kwargs):
        raise write_layer.WriteLayerError("parse_check_failed", "kicad-cli pcb upgrade exited 3221225477 (0xC0000005)")

    monkeypatch.setattr(pa, "_scratch_board", crash)
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "incomplete"
    detail = result["problems"][0]["detail"]
    assert "side view could not be rendered" in detail and "0xC0000005" in detail
    assert "does not parse" not in detail
