r"""The write layer (build ticket 42; docs/ARCHITECTURE.md decision 1).

The oracle is KiCad: copies of KiCad's own demos (never modified in place) and
``kicad-cli <pcb|sch|fp|sym> upgrade --force``, which loads a file and saves it in KiCad's
canonical form. Two files are KiCad-equivalent when KiCad's canonical saves of both hold the
same multiset of top-level children (KiCad orders tracks and zones freely).

Reproductions first: the ``kicad_tools`` writer faults the layer exists for (a bare numeric
string KiCad refuses, ``\r`` written raw and read back as ``\n``, 6-digit floats, ``-0`` lost,
0.23.0's Windows ``PCB.save`` failure), each shown on ``kicad_tools`` and then fixed by the
layer.

Demos come from ``KICAD_DEMOS_DIR`` or the install next to ``kicad-cli``. A missing demo skips
in an ad-hoc run and fails when ``KICAD_DEMOS_REQUIRED=1`` (docs/TESTING.md; build ticket 34 will
replace ``_demos`` with the shared fixture).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import kicad_tools
import pytest
from kicad_tools.sexp import Document, SExp, parse_string

from kicad_mcp import kicad_cli
from kicad_mcp import write_layer as wl
from tests import _kicad_faults as faults

_KT_VERSION = tuple(int(p) for p in kicad_tools.__version__.split(".")[:2])

ROUND_TRIP = [
    "pic_programmer/pic_programmer.kicad_pcb",
    "complex_hierarchy/complex_hierarchy.kicad_pcb",
    "stickhub/StickHub.kicad_pcb",
    "video/video.kicad_pcb",
    "cm5_minima/CM5_MINIMA_3.kicad_pcb",
    "pic_programmer/pic_programmer.kicad_sch",
    "stickhub/StickHub.kicad_sch",
    "complex_hierarchy/complex_hierarchy.kicad_sch",
]


def _missing(reason: str):
    if os.environ.get("KICAD_DEMOS_REQUIRED") == "1":
        pytest.fail(f"KICAD_DEMOS_REQUIRED=1: {reason}")
    pytest.skip(reason)


def _cli() -> Path:
    try:
        return kicad_cli.find_kicad_cli()
    except kicad_cli.KicadCliError as exc:
        _missing(f"kicad-cli not found: {exc}")


def _demos() -> Path:
    env = os.environ.get("KICAD_DEMOS_DIR")
    candidates = [Path(env)] if env else []
    cli = _cli()
    candidates += [cli.parent.parent / "share" / "kicad" / "demos", Path("/usr/share/kicad/demos")]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    _missing(f"KiCad demos not found (tried {[str(c) for c in candidates]})")


def _copy_demo(rel: str, dest: Path) -> Path:
    """Copy the KiCad files next to demo ``rel`` (not sub-directories) into ``dest``."""
    source = _demos() / rel
    if not source.is_file():
        _missing(f"demo {rel} not installed")
    dest.mkdir(parents=True, exist_ok=True)
    for f in source.parent.glob("*.kicad_*"):
        shutil.copyfile(f, dest / f.name)
    return dest / source.name


def _upgrade(path: Path, kind: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(_cli()), kind, "upgrade", "--force", str(path)], capture_output=True, timeout=300
    )


def _canonical(path: Path, kind: str) -> Counter:
    proc = _upgrade(path, kind)
    assert proc.returncode == 0, (path, proc.returncode, proc.stdout, proc.stderr)
    return Counter(wl.serialise(c, compact=True) for c in wl.load(path).children)


def _footprint_value(root: SExp) -> SExp:
    """The first footprint's ``(property "Value" ...)`` node."""
    for fp in root.children:
        if fp.name == "footprint":
            for prop in fp.children:
                if prop.name == "property" and prop.children[0].value == "Value":
                    return prop
    raise AssertionError("no footprint Value property")


def _first(root: SExp, tag: str) -> SExp:
    return next(c for c in root.children if c.name == tag)


# --------------------------------------------------------------------------- reproduce first


@pytest.mark.parametrize("rel", ROUND_TRIP, ids=[Path(r).name for r in ROUND_TRIP])
def test_round_trip_is_kicad_equivalent(rel: str, tmp_path: Path) -> None:
    """Ticket 03's 8 files: load and save through the layer; KiCad's canonical saves of the
    original and of ours hold the same top-level children, and every token is kept as read."""
    original = _copy_demo(rel, tmp_path / "original")
    ours = _copy_demo(rel, tmp_path / "ours")
    kind = "pcb" if ours.suffix == ".kicad_pcb" else "sch"

    wl.save(wl.load(ours), ours)

    token_stream = [wl.serialise(c, compact=True) for c in wl.load(original).children]
    assert [wl.serialise(c, compact=True) for c in wl.load(ours).children] == token_stream
    assert _canonical(ours, kind) == _canonical(original, kind)


def test_kicad_tools_writes_a_numeric_string_bare_and_kicad_refuses_it(tmp_path: Path) -> None:
    """Ticket 03 exp 6: ``set_value(1, "100")`` on a footprint Value writes ``100`` bare."""
    board = _copy_demo("pic_programmer/pic_programmer.kicad_pcb", tmp_path / "kt")
    doc = Document.load(board)
    _footprint_value(doc.root).set_value(1, "100")
    doc.save(board)
    assert '(property "Value" 100' in board.read_text(encoding="utf-8")
    assert _upgrade(board, "pcb").returncode != 0

    fixed = _copy_demo("pic_programmer/pic_programmer.kicad_pcb", tmp_path / "wl")
    root = wl.load(fixed)
    _footprint_value(root).set_value(1, "100")  # untyped, as kicad_tools' own code does it
    wl.save(root, fixed)
    assert '(property "Value" "100"' in fixed.read_text(encoding="utf-8")
    assert _footprint_value(wl.load(fixed)).children[1].value == "100"


def test_kicad_tools_loses_carriage_returns_precision_and_negative_zero() -> None:
    """kicad_tools writes ``\r`` raw (its reader turns it into ``\n``), new floats as ``%.6g``
    and ``-0.0`` as ``0``. The layer keeps all three."""
    node = SExp.list("x", "A\rB", 1234.56789, -0.0)
    # 0.25.0 escapes the CR itself (changelog of 0.25.0); up to 0.24 it was written raw.
    cr = "\\r" if _KT_VERSION >= (0, 25) else "\r"
    assert node.to_string(compact=True) == f'(x "A{cr}B" 1234.57 0)'
    assert wl.serialise(node, compact=True) == '(x "A\\rB" 1234.56789 -0)'
    reread = parse_string(wl.serialise(node, compact=True))
    assert [c.value for c in reread.children] == ["A\rB", 1234.56789, 0]
    assert wl.serialise(reread, compact=True) == '(x "A\\rB" 1234.56789 -0)'


@pytest.mark.skipif(sys.platform != "win32", reason="the fsync failure is Windows-only")
def test_kicad_tools_pcb_save_on_windows_by_version(tmp_path: Path) -> None:
    """0.22.0 and 0.23.0 fsync a read-only handle in ``atomic_write`` (#5106), so ``PCB.save``
    raises ``OSError [Errno 9]`` and leaves ``<file>.tmp`` (upstream #7478). 0.24.0 fsyncs
    through a writable handle (PR #7527): the save works and leaves no ``.tmp``. Named
    ``test_kicad_tools_pcb_save_fails_on_windows_from_0_22`` until build ticket 71 made it
    version-aware. The write layer stays the only save path either way (decision 1)."""
    import kicad_tools
    from kicad_tools.schema import PCB

    version = tuple(int(p) for p in kicad_tools.__version__.split(".")[:2])
    if version < (0, 22):
        pytest.skip(f"kicad_tools {kicad_tools.__version__} predates the atomic_write fsync (0.22.0)")
    board = _copy_demo("pic_programmer/pic_programmer.kicad_pcb", tmp_path)
    before = board.read_bytes()
    pcb = PCB.load(board)
    if version < (0, 24):
        with pytest.raises(OSError) as raised:
            pcb.save(board)
        assert raised.value.errno == 9
        assert board.read_bytes() == before
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    else:
        pcb.save(board)
        assert board.read_bytes() != before  # written (LF line endings, PCB.load's net table)
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
        assert _upgrade(board, "pcb").returncode == 0

    wl.save_pcb(pcb, board)  # the layer's save works on the same board
    assert _upgrade(board, "pcb").returncode == 0


def test_save_pcb_never_calls_pcb_save(tmp_path: Path, monkeypatch) -> None:
    from kicad_tools.schema import PCB

    board = _copy_demo("pic_programmer/pic_programmer.kicad_pcb", tmp_path)
    pcb = PCB.load(board)

    def refuse(*args, **kwargs):
        raise AssertionError("PCB.save called")

    monkeypatch.setattr(PCB, "save", refuse)
    wl.save_pcb(pcb, board)
    assert [wl.serialise(c, compact=True) for c in wl.load(board).children] == [
        wl.serialise(c, compact=True) for c in pcb._sexp.children
    ]


# --------------------------------------------------------------------------- save_bytes


@pytest.mark.parametrize("name", ["board.kicad_pcb", "x.kicad_sch", "fp-lib-table.bak", "my-sym-lib-table"])
def test_save_bytes_writes_only_a_lib_table(tmp_path: Path, name: str) -> None:
    # Review round 1 of build ticket 76: save_bytes wrote raw bytes to any path, a .kicad_pcb
    # included, past every parse check of the layer.
    with pytest.raises(wl.WriteLayerError) as refused:
        wl.save_bytes(b"(kicad_pcb)", tmp_path / name, verify=lambda temp: None)
    assert refused.value.code == "unsupported_file"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("verify", [None, "not a function"])
def test_save_bytes_needs_a_verify_function(tmp_path: Path, verify) -> None:
    with pytest.raises(TypeError, match="verify"):
        wl.save_bytes(b"(fp_lib_table)", tmp_path / "fp-lib-table", verify=verify)
    assert list(tmp_path.iterdir()) == []


def test_save_bytes_verifies_the_exact_bytes_before_it_replaces(tmp_path: Path) -> None:
    target = tmp_path / "sym-lib-table"
    target.write_bytes(b"old")
    data = b"(sym_lib_table\r\n  (version 7)\r)\n"
    seen = []

    def verify(temp: Path) -> None:
        seen.append((temp.read_bytes(), target.read_bytes()))

    wl.save_bytes(data, target, verify=verify)

    assert seen == [(data, b"old")]
    assert target.read_bytes() == data
    assert [p.name for p in tmp_path.iterdir()] == ["sym-lib-table"]


# --------------------------------------------------------------------------- typed atoms


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "0"),
        (-3, "-3"),
        (5.0, "5"),
        (0.0, "0"),
        (-0.0, "-0"),
        (1234.56789, "1234.56789"),
        (101.23456789, "101.23456789"),
        (0.1, "0.1"),
        (1e-07, "0.0000001"),
        (-2.5e-05, "-0.000025"),
        (123456789.123, "123456789.123"),
    ],
)
def test_numbers_keep_full_precision_and_negative_zero(value, text) -> None:
    assert wl.format_number(value) == text
    atom = wl.number(value)
    assert wl.serialise(atom) == text
    reread = parse_string(f"(n {text})").children[0]
    assert reread.value == value


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), "1"])
def test_numbers_refuse_non_numbers(bad) -> None:
    with pytest.raises((TypeError, ValueError)):
        wl.format_number(bad)


def test_a_parsed_number_keeps_its_text_unless_its_value_was_changed() -> None:
    root = parse_string("(at -0 1.50 2.000000)")
    assert wl.serialise(root, compact=True) == "(at -0 1.50 2.000000)"
    root.children[1].value = 3.25  # changed in place, the old text is stale
    assert wl.serialise(root, compact=True) == "(at -0 3.25 2.000000)"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("100", '"100"'),
        ("-1.5", '"-1.5"'),
        ("F.Cu", '"F.Cu"'),
        ("", '""'),
        ('A"B', '"A\\"B"'),
        ("back\\slash", '"back\\\\slash"'),
        ("line\nbreak", '"line\\nbreak"'),
        ("carriage\rreturn", '"carriage\\rreturn"'),
        ("tab\tstays raw", '"tab\tstays raw"'),
        ("µΩ ü", '"µΩ ü"'),
    ],
)
def test_strings_are_always_quoted_and_escaped_like_kicad(value: str, text: str) -> None:
    atom = wl.string(value)
    assert wl.serialise(atom) == text
    node = SExp.list("p")
    wl.set_string(node, 0, value)
    assert wl.serialise(node, compact=True) == f"(p {text})"
    assert parse_string(f"(p {text})").children[0].value == value


def test_untyped_strings_that_look_numeric_are_quoted() -> None:
    node = SExp.list("property", "Value", "x")
    node.set_value(1, "100")  # kicad_tools guesses the type: this would write a bare 100
    node.set_value(2, "1e5")
    node.set_value(3, "yes")  # a keyword kicad_tools writes bare stays bare
    node.set_value(4, "#PWR01")  # would read back as a comment if bare
    assert wl.serialise(node, compact=True) == '(property "Value" "100" "1e5" yes "#PWR01")'


def test_atoms_read_bare_stay_bare_and_atoms_read_quoted_stay_quoted() -> None:
    text = '(pad "1" smd roundrect (layers "F.Cu" "F.Mask") (zone_connect not_allowed) (v "9.0"))'
    assert wl.serialise(parse_string(text), compact=True) == text


def test_symbols_refuse_what_would_not_read_back_as_the_same_token() -> None:
    assert wl.serialise(wl.symbol("smd")) == "smd"
    for bad in ("100", "two words", "", "a(b", "#x", 'q"'):
        with pytest.raises(ValueError):
            wl.symbol(bad)
    with pytest.raises(TypeError):
        wl.string(100)


def test_setters_replace_or_append_but_never_pad() -> None:
    node = SExp.list("at", 1, 2)
    wl.set_number(node, 0, 1.25)
    wl.set_number(node, 2, 90)
    wl.set_symbol(node, 3, "unlocked")
    assert wl.serialise(node, compact=True) == "(at 1.25 2 90 unlocked)"
    with pytest.raises(IndexError):
        wl.set_number(node, 9, 1)


def test_kicad_reads_and_writes_the_escapes_exactly_as_the_layer_does(tmp_path: Path) -> None:
    """A footprint Value holding every special character: KiCad loads our file, keeps the
    value, and its own save writes the very token we wrote."""
    board = _copy_demo("pic_programmer/pic_programmer.kicad_pcb", tmp_path)
    value = 'A"B\\C\nD\rE\tF µ'
    root = wl.load(board)
    wl.set_string(_footprint_value(root), 1, value)
    wl.save(root, board)
    ours = board.read_bytes()
    assert wl.quote(value).encode("utf-8") in ours

    assert _upgrade(board, "pcb").returncode == 0
    assert wl.quote(value).encode("utf-8") in board.read_bytes()
    assert _footprint_value(wl.load(board)).children[1].value == value


def test_full_precision_coordinates_reach_kicad(tmp_path: Path) -> None:
    board = _copy_demo("pic_programmer/pic_programmer.kicad_pcb", tmp_path)
    root = wl.load(board)
    at = _first(root, "footprint").find("at")
    wl.set_number(at, 0, 101.23456789)
    wl.set_number(at, 1, 123.4567891)
    wl.save(root, board)
    assert "(at 101.23456789 123.4567891" in board.read_text(encoding="utf-8")
    assert _upgrade(board, "pcb").returncode == 0
    kicad_at = _first(wl.load(board), "footprint").find("at")
    assert [kicad_at.children[0].value, kicad_at.children[1].value] == [101.234568, 123.456789]


# --------------------------------------------------------------------------- the save


def _listing(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_a_file_kicad_cannot_load_is_never_written(tmp_path: Path) -> None:
    """An unknown token: ``pcb upgrade`` crashes (0xC0000005) on the copy. The original stays
    byte-identical and no temporary file is left."""
    board = _copy_demo("pic_programmer/pic_programmer.kicad_pcb", tmp_path)
    before, listing = board.read_bytes(), _listing(tmp_path)
    root = wl.load(board)
    root.children.append(SExp.list("kicad_mcp_marker", 1))
    with pytest.raises(wl.WriteLayerError) as raised:
        wl.save(root, board)
    assert raised.value.code == "parse_check_failed"
    assert raised.value.details["exit_code"] != 0
    assert board.read_bytes() == before
    assert _listing(tmp_path) == listing


@pytest.mark.parametrize(
    "rel",
    ["complex_hierarchy/complex_hierarchy.kicad_sym", "complex_hierarchy/complex_hierarchy.pretty"],
    ids=["sym", "fp"],
)
def test_symbol_and_footprint_files_are_parse_checked_too(rel: str, tmp_path: Path) -> None:
    source = _demos() / rel
    if source.is_dir():
        source = sorted(source.glob("*.kicad_mod"))[0]
    if not source.is_file():
        _missing(f"demo {rel} not installed")
    target = tmp_path / source.name
    shutil.copyfile(source, target)
    wl.save(wl.load(target), target)
    before = target.read_bytes()

    root = wl.load(target)
    root.children.append(SExp.list("kicad_mcp_marker", 1))
    with pytest.raises(wl.WriteLayerError, match="parse_check_failed"):
        wl.save(root, target)
    assert target.read_bytes() == before
    assert _listing(tmp_path) == [source.name]


def _break_sub_sheet(sheet: Path) -> None:
    """A sub-sheet KiCad cannot load: an unknown top-level token, added through kicad_tools'
    low-level Document API (a test fixture, never raw text)."""
    doc = Document.load(sheet)
    doc.root.children.append(SExp.list("kicad_mcp_marker", 1))
    doc.save(sheet)


def test_every_sub_sheet_of_a_schematic_is_checked_on_its_own(tmp_path: Path) -> None:
    root_file = _copy_demo("complex_hierarchy/complex_hierarchy.kicad_sch", tmp_path / "p")
    _break_sub_sheet(tmp_path / "p" / "ampli_ht.kicad_sch")

    # The fact behind the rule: sch upgrade on the root reports success over the broken sheet.
    probe = tmp_path / "probe"
    shutil.copytree(tmp_path / "p", probe)
    assert _upgrade(probe / root_file.name, "sch").returncode == 0
    assert _upgrade(probe / "ampli_ht.kicad_sch", "sch").returncode != 0

    before, listing = root_file.read_bytes(), _listing(tmp_path / "p")
    with pytest.raises(wl.WriteLayerError) as raised:
        wl.save(wl.load(root_file), root_file)
    assert raised.value.code == "parse_check_failed"
    assert raised.value.details["file"].endswith("ampli_ht.kicad_sch")
    assert root_file.read_bytes() == before
    assert _listing(tmp_path / "p") == listing


def test_parse_check_names_every_sheet_it_checked(tmp_path: Path) -> None:
    root_file = _copy_demo("complex_hierarchy/complex_hierarchy.kicad_sch", tmp_path)
    checked = wl.parse_check(root_file, "sch")
    assert checked[0] == "complex_hierarchy.kicad_sch"
    assert "ampli_ht.kicad_sch" in checked


def test_a_missing_sub_sheet_refuses_the_save(tmp_path: Path) -> None:
    root_file = _copy_demo("complex_hierarchy/complex_hierarchy.kicad_sch", tmp_path)
    (tmp_path / "ampli_ht.kicad_sch").unlink()
    before = root_file.read_bytes()
    with pytest.raises(wl.WriteLayerError, match="sub_sheet_missing"):
        wl.save(wl.load(root_file), root_file)
    assert root_file.read_bytes() == before


def test_verify_sees_the_temporary_file_and_can_refuse(tmp_path: Path) -> None:
    board = _copy_demo("pic_programmer/pic_programmer.kicad_pcb", tmp_path)
    before, listing = board.read_bytes(), _listing(tmp_path)
    root = wl.load(board)
    wl.set_string(_footprint_value(root), 1, "CHANGED")
    seen: list[Path] = []

    def refuse(temp: Path) -> None:
        seen.append(temp)
        assert temp.parent == tmp_path and temp != board
        assert _footprint_value(wl.load(temp)).children[1].value == "CHANGED"
        raise RuntimeError("verification failed")

    with pytest.raises(RuntimeError, match="verification failed"):
        wl.save(root, board, verify=refuse)
    assert seen and not seen[0].exists()
    assert board.read_bytes() == before
    assert _listing(tmp_path) == listing

    wl.save(root, board, verify=lambda temp: None)
    assert _footprint_value(wl.load(board)).children[1].value == "CHANGED"
    assert _listing(tmp_path) == listing


def test_the_file_is_utf8_whatever_the_locale(tmp_path: Path) -> None:
    board = _copy_demo("pic_programmer/pic_programmer.kicad_pcb", tmp_path)
    root = wl.load(board)
    wl.set_string(_footprint_value(root), 1, "10µF Ω")
    wl.save(root, board)
    assert '"10µF Ω"'.encode() in board.read_bytes()


def test_a_raw_carriage_return_survives_load(tmp_path: Path) -> None:
    """KiCad tolerates a raw CR inside a string (kicad_tools 0.20 wrote them); ``load`` keeps
    it, where ``kicad_tools.sexp.parse_file`` reads it as a line feed."""
    from kicad_tools.sexp import parse_file

    path = tmp_path / "cr.kicad_sym"
    doc = Document(parse_string("(kicad_symbol_lib (version 20251024))"), path)
    doc.root.children.append(SExp.list("x", "A\rB"))
    doc.save(path)  # the fixture is built by kicad_tools' own writer
    if _KT_VERSION >= (0, 25):
        # 0.25.0 escapes the CR itself, so the raw-CR file (what 0.20 to 0.24 wrote and KiCad
        # tolerates) is rebuilt from that output, and the premise holds on every version.
        faults.rewrite_bytes(path, lambda data: data.replace(b'"A\\rB"', b'"A\rB"'))
    assert b'"A\rB"' in path.read_bytes()
    assert wl.load(path).children[1].children[0].value == "A\rB"
    # kicad_tools' reader turned the CR into a line feed up to 0.24; 0.25.0 reads it as is.
    expected = "A\rB" if _KT_VERSION >= (0, 25) else "A\nB"
    assert parse_file(path).children[1].children[0].value == expected


def test_unsupported_roots_and_a_missing_kicad_cli_write_nothing(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(wl.WriteLayerError, match="unsupported_file"):
        wl.save(parse_string("(fp_lib_table (version 7))"), tmp_path / "fp-lib-table")
    with pytest.raises(wl.WriteLayerError, match="unsupported_file"):
        wl.save_pcb(parse_string("(kicad_sch (version 20260306))"), tmp_path / "x.kicad_pcb")

    def unavailable() -> Path:
        raise kicad_cli.KicadCliError("not installed")

    monkeypatch.setattr(kicad_cli, "find_kicad_cli", unavailable)
    with pytest.raises(wl.WriteLayerError, match="kicad_cli_unavailable"):
        wl.save(parse_string("(kicad_pcb (version 20260206))"), tmp_path / "x.kicad_pcb")
    assert _listing(tmp_path) == []


def test_serialise_lays_out_one_tab_per_level() -> None:
    root = parse_string('(kicad_pcb (version 1) (footprint "R" (at 1 2) (pad "1" smd rect (at 0 0) (size 1 1))))')
    assert wl.serialise(root) == (
        "(kicad_pcb\n"
        "\t(version 1)\n"
        '\t(footprint "R"\n'
        "\t\t(at 1 2)\n"
        '\t\t(pad "1" smd rect (at 0 0) (size 1 1))\n'
        "\t)\n"
        ")"
    )


def test_save_project_writes_kicad_tools_json_shape_through_a_checked_commit(tmp_path: Path) -> None:
    """``.kicad_pro`` saves: ``json.dumps(indent=2)`` as kicad_tools formats them, read back
    before the replace, never through 0.22.0+'s Windows-broken ``atomic_write_text``."""
    import json

    target = tmp_path / "p.kicad_pro"
    data = {"board": {"design_settings": {"rules": {"min_clearance": 0.127}}}, "label": "Qté"}
    wl.save_project(data, target)
    text = target.read_text(encoding="utf-8")
    assert text.replace("\r\n", "\n") == json.dumps(data, indent=2)
    before = target.read_bytes()

    def refuse(temp: Path) -> None:
        assert json.loads(temp.read_text(encoding="utf-8"))["label"] == "other"
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        wl.save_project({**data, "label": "other"}, target, verify=refuse)
    with pytest.raises(TypeError):
        wl.save_project({"x": object()}, target)
    assert target.read_bytes() == before
    assert _listing(tmp_path) == ["p.kicad_pro"]


def _sharing_denied() -> PermissionError:
    exc = PermissionError(13, "Zugriff verweigert")
    exc.winerror = 5
    return exc


@pytest.mark.skipif(sys.platform != "win32", reason="the retry covers Windows sharing denials only")
def test_commit_directory_retries_a_transient_windows_sharing_denial(tmp_path: Path, monkeypatch) -> None:
    """Windows refuses a directory rename while a scanner holds a just-written file open
    (measured 0.26-0.39 s, build ticket 42); the commit waits it out."""
    temp, target = tmp_path / ".rev.tmp", tmp_path / "rev"
    temp.mkdir()
    (temp / "f.kicad_pcb").write_bytes(b"x")  # scratch content, not a KiCad file KiCad sees
    real, calls = os.rename, []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) < 3:
            raise _sharing_denied()
        return real(src, dst)

    monkeypatch.setattr(wl.os, "rename", flaky)
    assert wl.commit_directory(temp, target) == target
    assert len(calls) == 3 and (target / "f.kicad_pcb").is_file() and not temp.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="the retry covers Windows sharing denials only")
def test_commit_directory_gives_up_after_its_budget_and_leaves_the_temp_dir(tmp_path: Path, monkeypatch) -> None:
    temp, target = tmp_path / ".rev.tmp", tmp_path / "rev"
    temp.mkdir()

    def denied(src, dst):
        raise _sharing_denied()

    monkeypatch.setattr(wl, "COMMIT_RETRY_BUDGET_S", 0.1)
    monkeypatch.setattr(wl.os, "rename", denied)
    with pytest.raises(PermissionError):
        wl.commit_directory(temp, target)
    assert temp.is_dir() and not target.exists()


def test_commit_directory_never_retries_other_errors(tmp_path: Path, monkeypatch) -> None:
    temp, target = tmp_path / ".rev.tmp", tmp_path / "rev"
    temp.mkdir()
    target.mkdir()
    (target / "keep").write_bytes(b"x")
    calls = []
    real = os.rename

    def counting(src, dst):
        calls.append(src)
        return real(src, dst)

    monkeypatch.setattr(wl.os, "rename", counting)
    with pytest.raises(OSError):
        wl.commit_directory(temp, target)  # a non-empty target is never overwritten
    assert len(calls) == 1 and temp.is_dir() and (target / "keep").is_file()


# --------------------------------------------------------------------------- fix round 1
# Each test below comes from the adversarial review of build ticket 42 and failed on the
# reviewed head 5ef4bb2, or kills a mutant that survived there (named in its docstring).


def _changed_board(tmp_path: Path) -> tuple[Path, SExp, bytes, list[str]]:
    board = _copy_demo("pic_programmer/pic_programmer.kicad_pcb", tmp_path)
    before, listing = board.read_bytes(), _listing(tmp_path)
    root = wl.load(board)
    wl.set_string(_footprint_value(root), 1, "CHANGED")
    return board, root, before, listing


def test_a_parse_check_that_times_out_refuses_the_save(tmp_path: Path, monkeypatch) -> None:
    """A hung kicad-cli is a failed parse check, never a pass. Kills the mutant that turns
    the ``TimeoutExpired`` handler into ``return``."""
    board, root, before, listing = _changed_board(tmp_path)
    real = subprocess.run

    def hung(cmd, *args, **kwargs):
        if "upgrade" in cmd:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(wl.subprocess, "run", hung)
    with pytest.raises(wl.WriteLayerError) as raised:
        wl.save(root, board)
    assert raised.value.code == "parse_check_failed"
    assert "did not finish" in str(raised.value)
    assert board.read_bytes() == before
    assert _listing(tmp_path) == listing


def test_a_broken_sheet_two_levels_below_the_root_refuses_the_roots_save(tmp_path: Path) -> None:
    """vme-wren's pp_driver_8x.kicad_sch is loaded by a sub-sheet, never by the root: the
    check recurses. Kills the mutant that drops the recursive ``_check_sub_sheets`` call
    (complex_hierarchy has one level only)."""
    root_file = _copy_demo("vme-wren/vme-wren.kicad_sch", tmp_path / "p")
    leaf = tmp_path / "p" / "pp_driver_8x.kicad_sch"
    direct = set(wl._sub_sheets(wl.load(root_file)))
    assert leaf.name not in direct and leaf.is_file()
    assert any(leaf.name in wl._sub_sheets(wl.load(tmp_path / "p" / d)) for d in direct)
    _break_sub_sheet(leaf)

    before, listing = root_file.read_bytes(), _listing(tmp_path / "p")
    with pytest.raises(wl.WriteLayerError) as raised:
        wl.save(wl.load(root_file), root_file)
    assert raised.value.code == "parse_check_failed"
    assert raised.value.details["file"].endswith("pp_driver_8x.kicad_sch")
    assert root_file.read_bytes() == before
    assert _listing(tmp_path / "p") == listing


def test_a_lone_surrogate_is_refused_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    """The review's reproduction: a lone surrogate (it can arrive through JSON) made the UTF-8
    write raise after the temporary file was created, and the file was left behind."""
    board = _copy_demo("pic_programmer/pic_programmer.kicad_pcb", tmp_path)
    before, listing = board.read_bytes(), _listing(tmp_path)
    with pytest.raises(ValueError):
        root = wl.load(board)
        wl.set_string(_footprint_value(root), 1, "bad\ud800")
        wl.save(root, board)
    assert board.read_bytes() == before
    assert _listing(tmp_path) == listing


@pytest.mark.parametrize(
    "value",
    ["bad\ud800", "\udfff", "A\x00B", "A\x0bB", "A\x1bB", "A\x01", "\x1f"],
    ids=["high-surrogate", "low-surrogate", "NUL", "VT", "ESC", "SOH", "US"],
)
def test_new_strings_refuse_lone_surrogates_and_control_characters_up_front(value: str) -> None:
    """A NEW value a caller supplies (``string``, ``set_string``, ``symbol``) is refused
    where it is made, with a named error, before any file exists. TAB, LF and CR stay
    allowed (``test_strings_are_always_quoted_and_escaped_like_kicad``). A control character
    already in a file KiCad wrote is not refused (fix round 2,
    ``test_a_control_character_kicad_itself_wrote_is_saved_as_kicad_writes_it``)."""
    with pytest.raises(wl.InvalidStringError, match=r"U\+"):
        wl.string(value)
    with pytest.raises(wl.InvalidStringError):
        wl.set_string(SExp.list("p"), 0, value)
    with pytest.raises(wl.InvalidStringError):
        wl.symbol(value)
    assert issubclass(wl.InvalidStringError, ValueError)


@pytest.mark.parametrize(
    ("value", "refused"),
    [("bad\ud800", True), ("\udfff", True), ("A\x1bB", False), ("A\x00B", False)],
    ids=["high-surrogate", "low-surrogate", "ESC", "NUL"],
)
def test_quote_refuses_only_lone_surrogates(value: str, refused: bool) -> None:
    """``quote`` also writes atoms read from disk, so it refuses only what no strict UTF-8
    read can produce (a lone surrogate); a control character is written raw, as KiCad does."""
    if refused:
        with pytest.raises(wl.InvalidStringError, match=r"U\+"):
            wl.quote(value)
    else:
        assert wl.quote(value) == f'"{value}"'


@pytest.mark.parametrize("bare", [False, True], ids=["read-quoted", "read-bare"])
def test_a_lone_surrogate_set_behind_the_layers_back_is_refused_before_anything_is_written(
    tmp_path: Path, bare: bool
) -> None:
    """An atom made without ``string()`` (kicad_tools' own setters, a value changed in place)
    is checked for lone surrogates when it is serialised, on the quoted and the bare path."""
    board = _copy_demo("pic_programmer/pic_programmer.kicad_pcb", tmp_path)
    before, listing = board.read_bytes(), _listing(tmp_path)
    root = wl.load(board)
    flag = "_originally_bare" if bare else "_originally_quoted"
    _footprint_value(root).children[1] = SExp(value="A\ud800B", **{flag: True})
    with pytest.raises(wl.InvalidStringError):
        wl.save(root, board)
    assert board.read_bytes() == before
    assert _listing(tmp_path) == listing


def _board_kicad_wrote_with_an_esc_in_a_value(dest: Path) -> Path:
    """Test-only input: footprint Value "A<ESC>B". The token is set on kicad_tools' low-level
    ``Document`` tree and written by ``Document.save`` (the layer refuses a NEW control
    character), then KiCad loads and saves the board itself (``kicad-cli pcb upgrade
    --force``, exit 0), so the file under test is KiCad's own, with the ESC written raw."""
    board = _copy_demo("pic_programmer/pic_programmer.kicad_pcb", dest)
    doc = Document.load(board)
    _footprint_value(doc.root).children[1].value = "A\x1bB"
    doc.save(board)
    assert _upgrade(board, "pcb").returncode == 0
    assert '"A\x1bB"'.encode() in board.read_bytes()
    return board


def test_a_control_character_kicad_itself_wrote_is_saved_as_kicad_writes_it(tmp_path: Path) -> None:
    """Fix round 2 blocker: KiCad writes ESC raw inside a quoted string and loads it again;
    the layer refused to save such a board (``InvalidStringError`` from ``atom_text``).
    Through ``save`` and ``save_pcb``: the save succeeds (parse-checked), writes every token
    of the tree it was handed (``save``: KiCad's file exactly; ``save_pcb``: ``PCB.load``'s
    tree, which adds a top-level net table to a KiCad 10 board, see docs/ARCHITECTURE.md), writes
    the ESC raw as KiCad does, and a second save is byte-identical."""
    from kicad_tools.schema import PCB

    kicad = _board_kicad_wrote_with_an_esc_in_a_value(tmp_path / "kicad")
    via_save = tmp_path / "save" / kicad.name
    via_pcb = tmp_path / "pcb" / kicad.name
    for copy in (via_save, via_pcb):
        copy.parent.mkdir()
        shutil.copyfile(kicad, copy)
    expected = {
        via_save: [wl.serialise(c, compact=True) for c in wl.load(kicad).children],
        via_pcb: [wl.serialise(c, compact=True) for c in PCB.load(kicad)._sexp.children],
    }

    wl.save(wl.load(via_save), via_save)
    wl.save_pcb(PCB.load(via_pcb), via_pcb)

    for saved, tokens in expected.items():
        assert [wl.serialise(c, compact=True) for c in wl.load(saved).children] == tokens
        assert '"A\x1bB"'.encode() in saved.read_bytes()
        first = saved.read_bytes()
        wl.save(wl.load(saved), saved)
        assert saved.read_bytes() == first


class _DiskFull:
    """A file handle whose write stores half the text, then fails like a full disk."""

    def __init__(self, fh) -> None:
        self.fh = fh

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.fh.close()

    def write(self, text: str) -> int:
        self.fh.write(text[: len(text) // 2])
        raise OSError(28, "No space left on device")


def test_a_write_that_fails_midway_leaves_no_temporary_file(tmp_path: Path, monkeypatch) -> None:
    """The cleanup backstop: the temporary file is created and written inside the guard that
    removes it. Kills the mutant that drops the unlink in ``_write_temp``."""
    import builtins

    board, root, before, listing = _changed_board(tmp_path)
    monkeypatch.setattr(wl, "open", lambda *a, **k: _DiskFull(builtins.open(*a, **k)), raising=False)
    with pytest.raises(OSError, match="No space"):
        wl.save(root, board)
    with pytest.raises(OSError, match="No space"):
        wl.save_project({"x": 1}, tmp_path / "p.kicad_pro")
    assert board.read_bytes() == before
    assert _listing(tmp_path) == listing


def test_an_interrupt_during_verify_leaves_no_temporary_file(tmp_path: Path) -> None:
    """``KeyboardInterrupt`` is no ``Exception``: the commit's cleanup catches
    ``BaseException``. Kills the mutant ``except Exception``."""
    board, root, before, listing = _changed_board(tmp_path)

    def interrupt(temp: Path) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        wl.save(root, board, verify=interrupt)
    assert board.read_bytes() == before
    assert _listing(tmp_path) == listing


def test_a_bare_atom_changed_in_place_to_text_that_breaks_the_token_is_quoted() -> None:
    """Kills the mutant that writes a bare-read atom bare whatever its new text."""
    root = parse_string("(pad smd (zone_connect not_allowed) (net x))")
    root.children[0].value = "two words"
    root.children[1].children[0].value = "a(b"
    root.children[2].children[0].value = "c)"
    assert wl.serialise(root, compact=True) == '(pad "two words" (zone_connect "a(b") (net "c)"))'


def test_a_float_zero_changed_in_place_to_negative_zero_keeps_the_new_sign() -> None:
    """Kills the mutant that ignores the sign when deciding whether the read text is stale."""
    root = parse_string("(at 0.0 -0.0 -0)")
    root.children[0].value = -0.0
    root.children[1].value = 0.0
    assert wl.serialise(root, compact=True) == "(at -0 0 -0)"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
def test_save_project_refuses_non_finite_numbers_before_writing(tmp_path: Path, bad: float) -> None:
    """JSON has no NaN or Infinity; ``json.dumps`` would write them and KiCad could not read
    the project. Refused before any file exists."""
    target = tmp_path / "p.kicad_pro"
    with pytest.raises(ValueError, match="finite"):
        wl.save_project({"board": {"rules": {"min_clearance": bad}}}, target)
    assert _listing(tmp_path) == []


@pytest.mark.parametrize("written", ['{"x": 1', '{"x": 2}'], ids=["truncated", "other-data"])
def test_save_project_refuses_a_temporary_file_that_does_not_read_back(
    tmp_path: Path, monkeypatch, written: str
) -> None:
    """The read-back checks the bytes on disk, not the data: it catches a damaged write (a
    short write, a wrong encoding). Kills the mutant that disables the comparison."""
    target = tmp_path / "p.kicad_pro"
    wl.save_project({"x": 1}, target)
    before = target.read_bytes()
    real = wl._write_temp

    def damaged(text: str, path: Path) -> Path:
        return real(written, path)

    monkeypatch.setattr(wl, "_write_temp", damaged)
    with pytest.raises(wl.WriteLayerError, match="parse_check_failed"):
        wl.save_project({"x": 1}, target)
    assert target.read_bytes() == before
    assert _listing(tmp_path) == ["p.kicad_pro"]


@pytest.mark.parametrize(
    "rel",
    ["pic_programmer/pic_programmer.kicad_pcb", "pic_programmer/pic_programmer.kicad_sch"],
    ids=["pcb", "sch"],
)
def test_the_layer_writes_the_line_endings_kicad_writes_on_this_platform(
    rel: str, tmp_path: Path, monkeypatch
) -> None:
    """The review asked for LF everywhere, on the premise that KiCad writes LF. Measured:
    KiCad 10.0.7's own save writes CRLF on Windows (from an LF input too) and LF elsewhere,
    so the layer writes the platform's line ending. Every line of our file ends the way
    every line of KiCad's save of it does. Kills the mutant ``newline="\\n"`` on Windows
    (and ``newline="\\r\\n"`` elsewhere)."""
    ours = _copy_demo(rel, tmp_path / "ours")
    kind = "pcb" if ours.suffix == ".kicad_pcb" else "sch"
    wl.save(wl.load(ours), ours)
    ours_bytes = ours.read_bytes()

    kicad = _copy_demo(rel, tmp_path / "kicad")
    with monkeypatch.context() as m:  # an LF input, written by the layer itself
        m.setattr(wl, "_KICAD_NEWLINE", "\n")
        wl.save(wl.load(kicad), kicad)
    assert b"\r\n" not in kicad.read_bytes()
    assert _upgrade(kicad, kind).returncode == 0
    kicad_bytes = kicad.read_bytes()
    eol = b"\r\n" if b"\r\n" in kicad_bytes else b"\n"
    assert kicad_bytes.count(eol) == kicad_bytes.count(b"\n") > 100
    assert ours_bytes.count(eol) == ours_bytes.count(b"\n") > 100
    assert eol == os.linesep.encode()


# --------------------------------------------------------------------------- build ticket 78
# ``_commit`` (every file save) retries ``os.replace`` on a transient Windows sharing denial
# with ``commit_directory``'s schedule, through one shared helper. Under parallel gates a
# scanner or indexer briefly holding the target failed a ``save_project`` once (WinError 5,
# ticket 43's gate).

needs_windows = pytest.mark.skipif(sys.platform != "win32", reason="the retry covers Windows sharing denials only")


def _denied(winerror: int | None) -> PermissionError:
    exc = PermissionError(13, "Zugriff verweigert")
    exc.winerror = winerror
    return exc


class _FlakyReplace:
    """Stands in for ``os.replace``: a move onto ``target`` raises ``errors`` in turn (a
    fresh exception each attempt, kept in ``raised``), then really replaces; a move onto any
    other path passes straight through. ``events`` logs each attempt as ``("replace", src)``."""

    def __init__(self, target: Path, errors: list[BaseException], events: list | None = None) -> None:
        self.target, self.errors, self.raised = target, list(errors), []
        self.events = [] if events is None else events
        self.real = os.replace

    def __call__(self, src, dst):
        if Path(dst) != self.target:
            return self.real(src, dst)
        self.events.append(("replace", Path(src)))
        if self.errors:
            exc = self.errors.pop(0)
            self.raised.append(exc)
            raise exc
        return self.real(src, dst)


@needs_windows
@pytest.mark.parametrize("winerror", [5, 32], ids=["WinError5", "WinError32"])
def test_save_project_rides_out_a_transient_sharing_denial(tmp_path: Path, monkeypatch, winerror: int) -> None:
    """Reproduction (ticket 78): one denied ``os.replace`` failed ``save_project`` outright."""
    import json

    target = tmp_path / "p.kicad_pro"
    wl.save_project({"x": 1}, target)
    flaky = _FlakyReplace(target, [_denied(winerror)])
    monkeypatch.setattr(wl.os, "replace", flaky)
    assert wl.save_project({"x": 2}, target) == target
    assert json.loads(target.read_text(encoding="utf-8")) == {"x": 2}
    assert len(flaky.events) == 2
    assert _listing(tmp_path) == ["p.kicad_pro"]


@needs_windows
@pytest.mark.parametrize(
    "rel",
    ["pic_programmer/pic_programmer.kicad_pcb", "pic_programmer/pic_programmer.kicad_sch"],
    ids=["pcb", "sch"],
)
def test_a_board_or_schematic_save_rides_out_a_transient_sharing_denial(rel: str, tmp_path: Path, monkeypatch) -> None:
    """Reproduction (ticket 78) for ``save``, the path every board and schematic save takes."""
    target = _copy_demo(rel, tmp_path)
    listing = _listing(tmp_path)
    root = wl.load(target)
    expected = wl.serialise(root)
    flaky = _FlakyReplace(target, [_denied(5)])
    monkeypatch.setattr(wl.os, "replace", flaky)
    assert wl.save(root, target) == target
    assert wl.serialise(wl.load(target)) == expected
    assert len(flaky.events) == 2
    assert _listing(tmp_path) == listing


class _FakeClock:
    """``time.monotonic`` and ``time.sleep`` for the retry: sleeping advances the clock and
    is logged in ``slept``; nothing really waits. ``install`` stands it in for the write
    layer's own ``time`` name only (as ``test_cap_clocks.py`` does), never for the process's
    ``time`` module. ``on_sleep(n)``, if set, runs before the n-th sleep (1-based)."""

    def __init__(self) -> None:
        self.now, self.slept = 1000.0, []
        self.on_sleep = None

    def install(self, monkeypatch) -> _FakeClock:
        from types import SimpleNamespace

        monkeypatch.setattr(wl, "time", SimpleNamespace(monotonic=self.monotonic, sleep=self.sleep))
        return self

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(len(self.slept))
        self.now += seconds


# The schedule, worked out by hand from build ticket 42's commit_directory: 0.02 s doubling
# to at most 0.5 s while the next attempt starts within COMMIT_RETRY_BUDGET_S (3 s). After
# these nine sleeps (2.62 s) a tenth would end at 3.12 s, so the tenth attempt is the last.
_SCHEDULE = [0.02, 0.04, 0.08, 0.16, 0.32, 0.5, 0.5, 0.5, 0.5]


@needs_windows
def test_a_persistent_sharing_denial_gives_up_on_commit_directorys_schedule(tmp_path: Path, monkeypatch) -> None:
    """``_commit`` and ``commit_directory`` share one schedule: the same sleeps, the same ten
    attempts. On giving up, the denial ``os.replace`` raised last propagates unchanged, the
    temporary file is gone, the target is byte-identical and ``verify`` ran once."""
    target = tmp_path / "proj" / "p.kicad_pro"
    target.parent.mkdir()
    wl.save_project({"x": 1}, target)
    before = target.read_bytes()

    file_clock = _FakeClock().install(monkeypatch)
    flaky = _FlakyReplace(target, [_denied(5) for _ in range(50)])
    monkeypatch.setattr(wl.os, "replace", flaky)
    verified: list[Path] = []
    with pytest.raises(PermissionError) as raised:
        wl.save_project({"x": 2}, target, verify=verified.append)
    assert raised.value is flaky.raised[-1] and raised.value.winerror == 5
    assert len(flaky.events) == len(_SCHEDULE) + 1 == 10
    assert file_clock.slept == pytest.approx(_SCHEDULE)
    assert len(verified) == 1
    assert target.read_bytes() == before
    assert _listing(target.parent) == ["p.kicad_pro"]

    dir_clock = _FakeClock().install(monkeypatch)
    temp_dir, target_dir, renames = tmp_path / ".rev.tmp", tmp_path / "rev", []
    temp_dir.mkdir()

    def denied(src, dst):
        renames.append(src)
        raise _denied(32)

    monkeypatch.setattr(wl.os, "rename", denied)
    with pytest.raises(PermissionError):
        wl.commit_directory(temp_dir, target_dir)
    assert len(renames) == 10
    assert dir_clock.slept == file_clock.slept


@pytest.mark.parametrize(
    "error",
    [_denied(None), _denied(33), _denied(1), OSError(28, "No space left on device"), FileNotFoundError(2, "gone")],
    ids=["PermissionError-no-winerror", "WinError33-lock-violation", "WinError1", "ENOSPC", "FileNotFoundError"],
)
def test_a_commit_error_that_is_no_sharing_denial_is_never_retried(tmp_path: Path, monkeypatch, error: OSError) -> None:
    """Only WinError 5 and 32 are retried. Any other error from ``os.replace`` propagates
    from the first attempt, with no sleep, the target byte-identical and the temp file gone."""
    target = tmp_path / "p.kicad_pro"
    wl.save_project({"x": 1}, target)
    before = target.read_bytes()
    clock = _FakeClock().install(monkeypatch)
    flaky = _FlakyReplace(target, [error, _denied(5)])
    monkeypatch.setattr(wl.os, "replace", flaky)
    with pytest.raises(OSError) as raised:
        wl.save_project({"x": 2}, target)
    assert raised.value is error
    assert len(flaky.events) == 1 and clock.slept == []
    assert target.read_bytes() == before
    assert _listing(tmp_path) == ["p.kicad_pro"]


@needs_windows
def test_verify_runs_once_on_the_temporary_file_before_the_retried_replace(tmp_path: Path, monkeypatch) -> None:
    """The checks are not part of the retry: ``verify`` sees the temporary file once, before
    the first move, and the target ends up with exactly the bytes it verified."""
    target = tmp_path / "p.kicad_pro"
    wl.save_project({"x": 1}, target)
    clock = _FakeClock().install(monkeypatch)
    events: list = []
    flaky = _FlakyReplace(target, [_denied(5), _denied(32)], events)
    monkeypatch.setattr(wl.os, "replace", flaky)
    verified: list[bytes] = []

    def verify(temp: Path) -> None:
        events.append(("verify", temp))
        verified.append(temp.read_bytes())

    wl.save_project({"x": 2}, target, verify=verify)
    temp = events[0][1]
    assert temp != target and temp.parent == tmp_path
    assert events == [("verify", temp), ("replace", temp), ("replace", temp), ("replace", temp)]
    assert target.read_bytes() == verified[0]
    assert clock.slept == pytest.approx(_SCHEDULE[:2])
    assert _listing(tmp_path) == ["p.kicad_pro"]


# --------------------------------------------------------------------------- ticket 78 fix round 1
# Each test below comes from the adversarial review of build ticket 78 (head bc7bbf0) and
# failed there, or kills a mutant that survived there (named in its docstring).


class _RecordingReplace:
    """The real ``os.replace``, logging each move onto ``target`` and keeping every error it
    raised in ``raised``; a move onto any other path passes straight through."""

    def __init__(self, target: Path) -> None:
        self.target, self.raised, self.real = target, [], os.replace

    def __call__(self, src, dst):
        if Path(dst) != self.target:
            return self.real(src, dst)
        try:
            return self.real(src, dst)
        except OSError as exc:
            self.raised.append(exc)
            raise


@pytest.mark.skipif(os.name != "nt", reason="a held handle blocks a move or delete on Windows only")
@pytest.mark.parametrize("released", [True, False], ids=["handle-released-during-cleanup", "handle-never-released"])
def test_a_handle_held_on_the_temporary_file_never_masks_the_move_denial(
    tmp_path: Path, monkeypatch, released: bool
) -> None:
    """Review round 1, finding 1 (blocker): a real handle on the temporary file denies the
    move (WinError 32) past the budget, and denied the cleanup's delete too, whose error then
    replaced the move's and left the temporary file behind. The cleanup now retries on the
    same schedule; the move's last denial propagates unchanged; a temporary file that still
    cannot go is named in a note on that error."""
    target = tmp_path / "p.kicad_pro"
    wl.save_project({"x": 1}, target)
    before = target.read_bytes()
    clock = _FakeClock().install(monkeypatch)
    moves = _RecordingReplace(target)
    monkeypatch.setattr(wl.os, "replace", moves)
    handles: list = []

    def hold(temp: Path) -> None:
        handles.append((temp, open(temp, "rb")))  # a scanner holding the file it just saw

    if released:  # the handle goes after the move's last attempt, during the cleanup's retry
        clock.on_sleep = lambda n: n > len(_SCHEDULE) and handles[0][1].close()
    try:
        with pytest.raises(OSError) as raised:
            wl.save_project({"x": 2}, target, verify=hold)
        temp = handles[0][0]
        assert len(moves.raised) == len(_SCHEDULE) + 1
        assert raised.value is moves.raised[-1]
        assert raised.value.winerror == 32 and raised.value.filename2 == str(target)
        assert target.read_bytes() == before
        notes = getattr(raised.value, "__notes__", [])
        if released:
            assert _listing(tmp_path) == ["p.kicad_pro"]
            assert notes == []
            assert len(clock.slept) > len(_SCHEDULE)
        else:
            assert temp.exists()
            assert len(notes) == 1 and str(temp) in notes[0] and "WinError 32" in notes[0]
            assert clock.slept == pytest.approx(_SCHEDULE + _SCHEDULE)
    finally:
        for _, fh in handles:
            fh.close()


@needs_windows
def test_the_parse_check_runs_once_however_often_the_move_is_retried(tmp_path: Path, monkeypatch) -> None:
    """Review round 1, finding 2: only the move is retried, never the checks. Kills the mutant
    ``_retry_sharing_denial(lambda: (check(), os.replace(temp, path)))``, which survived on
    bc7bbf0 (``verify`` was counted there, ``check`` was not)."""
    target = _copy_demo("pic_programmer/pic_programmer.kicad_pcb", tmp_path)
    root = wl.load(target)
    _FakeClock().install(monkeypatch)
    flaky = _FlakyReplace(target, [_denied(5), _denied(32)])
    monkeypatch.setattr(wl.os, "replace", flaky)
    real, checks = wl.parse_check, []

    def counting(*args, **kwargs):
        checks.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(wl, "parse_check", counting)
    wl.save(root, target)
    assert len(flaky.events) == 3
    assert checks == [flaky.events[0][1]]
