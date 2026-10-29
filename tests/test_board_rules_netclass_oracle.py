r"""Net-class resolution against KiCad itself (build ticket 36, review round 1, finding 1).

The oracle is ``NET_SETTINGS.GetEffectiveNetClass`` in KiCad's bundled Python
(``tests/_netclass_oracle.py``), on scratch copies whose ``.kicad_pro`` is written through
``write_layer.save_project``, in a throwaway ``KICAD_CONFIG_HOME``. What KiCad 10.0 answered
(measured 2026-10-20) and ``board_rules`` must reproduce:

* Every matching class takes part, not only the winner. They are ranked by priority (lower
  first, equal priorities by class name), and each value comes from the first class in that
  order that defines it, then from Default. The effective class is named after all of them,
  joined by commas ("A,B"); Default joins the name when a matching class leaves one of the
  values KiCad fills undefined ("A,Default").
* A pattern matches as a wildcard (``*``, ``?``) or as a regular expression anchored as text:
  ``^`` unless it starts with ``^``, ``$`` unless it ends with ``$`` (so ``VFF\$`` stays open at
  the end); a pattern that is not a valid regex (``+3V3``) only matches literally, and an
  empty pattern matches nothing. Matching is case-sensitive; ``\w \d \s \W \S`` are ASCII-only.
  The net named "" (net 0) is never classed, not even by ``*``.
* Python's ``re`` stands in for KiCad's wxRegEx only where the two agree. Syntax where they
  were measured to differ (``(?i)``, ``[[:digit:]]``, any brace, escapes such as ``\m``), or
  that Python flags as a possible future change (``[[``), is refused with
  ``UnsupportedNetclassPattern`` naming the class and the pattern (``REGEX_ROWS``, delta
  reviews 1 and 2, and the generated sweep).
* A net no pattern matches by its whole name is read by KiCad as a bus group when it holds
  braces, and classed through its members (``BRACE_NET_ROWS``); such a net refuses when a
  pattern matches text inside its braces, and otherwise stays in Default as in KiCad.
* A ``.kicad_pro`` whose ``net_settings`` schema is older than version 4 has its classes
  ranked by their order in the file: KiCad ignores their ``priority`` keys there. Every rung
  input and every ``krt_*`` fixture is such a version-3 file.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from kicad_mcp import board_rules, write_layer
from kicad_mcp.toolsets import routing as routing_toolset
from tests import _kicad_demos as demos

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden_attiny1616_board" / "golden_attiny1616_board.kicad_pcb"
ORACLE = Path(__file__).parent / "_netclass_oracle.py"
KEYS = ("track_width", "via_diameter", "via_drill", "microvia_diameter", "microvia_drill", "clearance")
_VALUES = ("track_width", "via_diameter", "via_drill", "microvia_diameter", "microvia_drill")

try:
    KICAD_PYTHON = routing_toolset.find_kicad_python()
    _NO_KICAD = None
except routing_toolset.KicadPythonUnavailable as exc:
    KICAD_PYTHON, _NO_KICAD = None, str(exc)


def _require_kicad_python(python: Path | None, problem: str | None, env=os.environ) -> Path:
    """KiCad's bundled Python, or skip with the reason; under ``KICAD_DEMOS_REQUIRED`` (the
    gate run) a missing one fails: the oracle never goes quiet (delta review 1, finding 3)."""
    if python is not None:
        return python
    reason = f"KiCad's bundled Python is not usable here: {problem}"
    if demos.demos_required(env):
        pytest.fail(f"{reason} ({demos.REQUIRED_FLAG} is set: the net-class oracle must run)", pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def kicad_python() -> Path:
    return _require_kicad_python(KICAD_PYTHON, _NO_KICAD)


def _cls(default: dict, name: str, priority: int | None, drop=(), **values) -> dict:
    cls = {k: v for k, v in copy.deepcopy(default).items() if k not in drop}
    cls["name"] = name
    if priority is not None:
        cls["priority"] = priority
    cls.update(values)
    return cls


# Version 5 (KiCad 10's own schema), on the golden board's KiCad-written project.
V5_CLASSES = [
    ("A", 0, _VALUES, {}),  # defines no track or via values of its own
    ("B", 1, (), {"track_width": 0.5, "via_diameter": 0.8, "via_drill": 0.4}),
    ("R", 2, (), {"track_width": 0.45}),
    ("P", 3, (), {"track_width": 0.4, "microvia_diameter": 0.35, "microvia_drill": 0.15}),
    ("Wide", 5, (), {"track_width": 0.35}),  # listed first, loses the tie by name
    ("Narrow", 5, (), {"track_width": 0.15}),
    ("Alt", 6, (), {"track_width": 0.36}),
    ("NoTrack", 7, ("track_width",), {}),
    ("NoVia", 8, ("via_diameter",), {"track_width": 0.27}),
]
V5_PATTERNS = [
    ("A", "SIG*"),
    ("B", "SIG1"),
    ("R", "VD[DX]"),
    ("P", "+3V3"),  # not a valid regex
    ("Wide", "GND"),
    ("Narrow", "GND"),
    ("Alt", "AB|CD"),
    ("NoTrack", "NT*"),
    ("NoVia", "NT1"),
    ("Ghost", "GH1"),  # a class the file does not define
]
V5_NETS = ["SIG1", "SIG2", "SI", "xSIG1", "sig1", "VDD", "VDX", "VDDX", "+3V3", "3V3", "+3V33", "GND",
           "CD", "XCD", "ABX", "NT1", "NT2", "GH1", "NOPE", "Net-(D1-K)"]

# Version 3 (what every rung input and krt fixture is): the file order ranks the classes.
V3_CLASSES = [
    ("X1", 1, (), {"track_width": 0.31}),
    ("X0", 0, (), {"track_width": 0.30}),
]
V3_PATTERNS = [("X1", "H1"), ("X0", "H1")]
V3_NETS = ["H1", "SIG1"]


def _project(dest: Path, source_pcb: Path, classes, patterns) -> Path:
    dest.mkdir(parents=True)
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_prl"):
        src = source_pcb.with_suffix(suffix)
        if src.exists():
            shutil.copy(src, dest)
    board = dest / source_pcb.name
    pro = board.with_suffix(".kicad_pro")
    data = board_rules._read_project(pro)
    ns = data["net_settings"]
    default = next(c for c in ns["classes"] if c["name"] == "Default")
    template = {k: v for k, v in default.items() if k != "priority"}
    ns["classes"] = [default] + [_cls(template, n, p, drop, **vals) for n, p, drop, vals in classes]
    ns["netclass_patterns"] = [{"netclass": c, "pattern": p} for c, p in patterns]
    write_layer.save_project(data, pro)
    return board


def _kicad(board: Path, nets: list[str]) -> dict:
    with tempfile.TemporaryDirectory(prefix="kicad_mcp_netclass_", ignore_cleanup_errors=True) as home:
        proc = subprocess.run(
            [str(KICAD_PYTHON), "-I", "-X", "utf8", str(ORACLE), str(board.resolve()), json.dumps(nets)],
            capture_output=True, timeout=180, cwd=home, check=False,
            env={**os.environ, "KICAD_CONFIG_HOME": home},
        )
    out = proc.stdout.decode("utf-8", errors="replace")
    for line in reversed(out.splitlines()):
        if line.startswith("NETCLASS_ORACLE "):
            result = json.loads(line[len("NETCLASS_ORACLE "):])
            assert result["ok"], result
            return result["nets"]
    raise AssertionError(f"no oracle result (exit {proc.returncode}): {out[-600:]} {proc.stderr[-600:]!r}")


@pytest.fixture(scope="module")
def oracle(tmp_path_factory, kicad_python) -> dict:
    """``{"v5"|"v3": (project data, KiCad's answer per net)}``, one KiCad run per project."""
    root = tmp_path_factory.mktemp("netclass_oracle")
    golden = FIXTURES / "golden_attiny1616_board" / "golden_attiny1616_board.kicad_pcb"
    stress = FIXTURES / "krt_stress_congestion.kicad_pcb"
    out = {}
    for name, source, classes, patterns, nets in (
        ("v5", golden, V5_CLASSES, V5_PATTERNS, V5_NETS),
        ("v3", stress, V3_CLASSES, V3_PATTERNS, V3_NETS),
    ):
        board = _project(root / name, source, classes, patterns)
        data = board_rules._read_project(board.with_suffix(".kicad_pro"))
        out[name] = (data, _kicad(board, nets))
    assert out["v5"][0]["net_settings"]["meta"]["version"] == 5
    assert out["v3"][0]["net_settings"]["meta"]["version"] == 3
    return out


def _ours(data: dict, net: str) -> dict:
    return {"name": board_rules._effective_netclass(data, net)} | {
        key: board_rules._net_value(data, net, key)[0] for key in KEYS
    }


@pytest.mark.parametrize("version, net", [("v5", n) for n in V5_NETS] + [("v3", n) for n in V3_NETS])
def test_every_probed_net_resolves_like_kicad(oracle, version, net) -> None:
    data, kicad = oracle[version]
    ours = _ours(data, net)
    assert ours["name"] == kicad[net]["name"]
    for key in KEYS:
        assert ours[key] == pytest.approx(kicad[net][key], abs=1e-6), key


def test_composite_class_takes_the_value_from_the_next_matching_class_like_kicad(oracle) -> None:
    data, kicad = oracle["v5"]
    # A (priority 0) defines no track or via values: B's apply, not Default's.
    assert kicad["SIG1"] == {"name": "A,B", "track_width": 0.5, "via_diameter": 0.8,
                                                   "via_drill": 0.4, "microvia_diameter": 0.3,
                                                   "microvia_drill": 0.1, "clearance": 0.2}
    assert board_rules._effective_netclass(data, "SIG1") == "A,B"
    assert board_rules._net_value(data, "SIG1", "track_width") == (0.5, "B")
    assert board_rules._net_value(data, "SIG1", "via_drill") == (0.4, "B")
    assert board_rules._net_value(data, "SIG1", "clearance") == (0.2, "A")
    # A alone leaves values open: Default fills them and joins the name.
    assert kicad["SIG2"]["name"] == "A,Default" and kicad["SIG2"]["track_width"] == 0.25
    assert board_rules._net_value(data, "SIG2", "track_width") == (0.25, "Default")
    # Two incomplete classes that complete each other need no Default.
    assert kicad["NT1"]["name"] == "NoTrack,NoVia" and kicad["NT1"]["track_width"] == 0.27
    assert kicad["NT2"]["name"] == "NoTrack,Default"
    # A pattern naming an undefined class: the name joins, no value comes from it.
    assert kicad["GH1"]["name"] == "Ghost,Default"
    # The winner (what the assignments report and the winner check use) is the first one.
    assert board_rules._effective_class(data, "SIG1") == "A"


def test_regex_netclass_pattern_is_matched_like_kicad(oracle) -> None:
    data, kicad = oracle["v5"]
    assert [kicad[n]["name"] for n in ("VDD", "VDX", "VDDX")] == ["R", "R", "Default"]
    assert kicad["VDD"]["track_width"] == 0.45
    assert board_rules._net_value(data, "VDD", "track_width") == (0.45, "R")
    # Anchored with ^...$ around the whole pattern, and a match must start at the first character.
    assert [kicad[n]["name"] for n in ("CD", "ABX", "XCD")] == ["Alt", "Alt", "Default"]
    # "SIG*" as a regex also matches "SI"; never a match in the middle of a name; case-sensitive.
    assert [kicad[n]["name"] for n in ("SI", "xSIG1", "sig1")] == ["A,Default", "Default", "Default"]
    for net in ("VDD", "VDX", "VDDX", "CD", "ABX", "XCD", "SI", "xSIG1", "sig1"):
        assert board_rules._effective_netclass(data, net) == kicad[net]["name"], net


def test_an_invalid_regex_pattern_is_matched_literally_like_kicad(oracle) -> None:
    data, kicad = oracle["v5"]
    assert [kicad[n]["name"] for n in ("+3V3", "3V3", "+3V33")] == ["P", "Default", "Default"]
    assert kicad["+3V3"]["track_width"] == 0.4 and kicad["+3V3"]["microvia_diameter"] == 0.35
    assert board_rules._net_value(data, "+3V3", "microvia_drill") == (0.15, "P")
    assert board_rules._effective_class(data, "+3V3") == "P"


def test_a_net_no_pattern_matches_is_in_default_like_kicad(oracle) -> None:
    data, kicad = oracle["v5"]
    for net in ("NOPE", "Net-(D1-K)"):
        assert kicad[net] == {"name": "Default", "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3,
                              "microvia_diameter": 0.3, "microvia_drill": 0.1, "clearance": 0.2}
        assert board_rules._effective_netclass(data, net) == "Default"
        assert board_rules._effective_class(data, net) == "Default"
        assert board_rules._net_value(data, net, "track_width") == (0.25, "Default")


def test_a_priority_tie_goes_to_the_smaller_class_name_like_kicad(oracle) -> None:
    data, kicad = oracle["v5"]
    assert kicad["GND"]["name"] == "Narrow,Wide" and kicad["GND"]["track_width"] == 0.15
    assert board_rules._effective_netclass(data, "GND") == "Narrow,Wide"
    assert board_rules._effective_class(data, "GND") == "Narrow"
    assert board_rules._net_value(data, "GND", "track_width") == (0.15, "Narrow")


def test_a_version_3_project_ranks_classes_by_file_order_like_kicad(oracle) -> None:
    data, kicad = oracle["v3"]
    # X1 is listed first with priority 1, X0 second with priority 0: KiCad ignores the keys.
    assert kicad["H1"]["name"] == "X1,X0" and kicad["H1"]["track_width"] == 0.31
    assert board_rules._effective_netclass(data, "H1") == "X1,X0"
    assert board_rules._effective_class(data, "H1") == "X1"
    assert board_rules._net_value(data, "H1", "track_width") == (0.31, "X1")


def test_a_priority_change_that_keeps_the_winner_but_moves_a_value_is_a_loosening(tmp_path) -> None:
    """The rules diff judges a net by KiCad's per-key values, not by its winning class alone:
    A wins Net-(D1-K) before and after, but A defines no track width, so moving B behind D
    lowers the net's track width from B's 0.5 to D's 0.21 mm."""
    golden = FIXTURES / "golden_attiny1616_board" / "golden_attiny1616_board.kicad_pcb"
    board = _project(
        tmp_path / "proj", golden,
        [("A", 0, ("track_width",), {}), ("B", 1, (), {"track_width": 0.5}), ("D", 2, (), {"track_width": 0.21})],
        [("A", "Net-(D1-K)"), ("B", "Net-(D1-K)"), ("D", "Net-(D1-K)")],
    )
    proposal = board_rules.propose_board_rules(board, netclasses=[{"name": "B", "priority": 5}],
                                               output_dir=tmp_path / "out")
    assert proposal["status"] == "refused", proposal
    assert {"where": "assignment", "key": "Net-(D1-K)", "before": "A,B,D", "after": "A,D,B"} in proposal["changes"]
    assert {
        "kind": "assignment", "where": "assignment", "key": "Net-(D1-K)", "value_key": "track_width",
        "before": 0.5, "after": 0.21, "from_class": "A,B,D", "to_class": "A,D,B",
    } in proposal["loosened"]


def test_kicad_s_own_regex_demo_resolves_every_net_like_kicad(kicad_demo, kicad_python) -> None:
    r"""tiny_tapeout ships regex patterns (``in\d+``, ``.*out\d+``) and an invalid one
    (``+?V?``): before the fix its 28 in/out nets were judged at Default's 0.2 mm instead of
    their class's 0.245 mm. Every net of the board, against KiCad (a scratch copy)."""
    board = kicad_demo("tiny_tapeout") / "tinytapeout-demo.kicad_pcb"
    data = board_rules._read_project(board.with_suffix(".kicad_pro"))
    nets = sorted(board_rules._board_net_names(board))
    assert len(nets) == 114
    kicad = _kicad(board, nets)
    disagree = [(n, kicad[n], _ours(data, n)) for n in nets
                if _ours(data, n)["name"] != kicad[n]["name"]
                or any(_ours(data, n)[k] != pytest.approx(kicad[n][k], abs=1e-6) for k in KEYS)]
    assert disagree == []
    assert kicad["in0"]["name"] == "ioin" and kicad["in0"]["track_width"] == 0.245


def test_on_a_version_3_project_a_priority_key_cannot_beat_an_earlier_class(tmp_path) -> None:
    """KiCad ignores ``priority`` in a version-3 file (``test_a_version_3_project_ranks_classes_by_file_order_like_kicad``),
    so a new class appended with priority 0 still loses SIG1 to the class listed before it
    (whose ``SIG*`` matches SIG1 too). The
    winner check now says so instead of reporting an assignment KiCad would not apply."""
    board = _project(tmp_path / "proj", FIXTURES / "krt_stress_congestion.kicad_pcb",
                     [("Early", 5, (), {"track_width": 0.3})], [("Early", "SIG*")])
    proposal = board_rules.propose_board_rules(
        board, netclasses=[{"name": "Late", "priority": 0, "track_width": 0.4, "nets": ["SIG1"]}],
        output_dir=tmp_path / "out")
    assert proposal["status"] == "error"
    assert "would still resolve to class 'Early', not 'Late'" in proposal["reason"]


# --- delta review 1: where Python's ``re`` and KiCad's wxRegEx read a pattern differently ---------

# (class, pattern, nets probed, the nets KiCad 10.0 puts in the class (measured 2026-10-21 with
# ``GetEffectiveNetClass``; the test re-measures it), refused). A refused row uses syntax where
# the two engines are known to differ: ``board_rules`` raises ``UnsupportedNetclassPattern``
# naming the class and the pattern instead of guessing. Every other row must agree with KiCad.
REGEX_ROWS = [
    ("Ci", "(?i)vdd", ["vdd", "VDD"], {"vdd", "VDD"}, True),  # re: no match (case-sensitive)
    ("Posix", "IN[[:digit:]]+", ["IN5", "INX"], {"IN5"}, True),  # re: a nested set, no match
    ("Nested", "T[a[:digit:]]", ["Ta", "T1", "T:"], {"Ta", "T1"}, True),
    ("WordStart", r"\mVBB", ["VBB"], {"VBB"}, True),  # re: \m is an error, so only literally
    ("Boundary", r"\bVCC", ["VCC"], {"VCC"}, True),  # agrees, but \b means different things
    ("EndAnchor", r"D\d\Z", ["D1"], {"D1"}, True),
    ("Bound0", "Q{,2}X", ["QX", "QQX"], set(), True),  # re: {0,2}, matches both
    ("Bound2", "R{2}", ["RR"], set(), True),  # re: matches RR; KiCad does not
    ("Empty", "", [""], set(), False),  # re and the wildcard: "" matches ""; KiCad: never
    ("Literal", "+3V3", ["+3V3", "3V3"], {"+3V3"}, False),  # invalid in both: literal only
    ("Escaped", r"V\.1", ["V.1", "VX1"], {"V.1"}, False),
    ("NonDigit", r"E\D", ["Ex", "E1"], {"Ex"}, False),
    ("Word", r"W\w+", ["Wa_1", "W-"], {"Wa_1"}, False),
    ("InSet", r"F[\d]", ["F1", "Fd"], {"F1"}, False),
    ("NotInSet", r"G[\D]", ["Ga", "G1"], {"Ga"}, False),
    ("Dot", "K[.]", ["K.", "Kx"], {"K."}, False),
    # Round 3: what a brace pattern matches depends on the other classes. Alone, ``M{x}`` gave
    # ``M{x}`` and ``Q{x}`` its class and ``/VPP{slash}MCLR`` every net holding ``{slash}``; beside
    # this file's ``*`` (row Star) neither matches anything. Every pattern with a brace refuses.
    ("Escape", "/VPP{slash}MCLR", ["/VPP{slash}MCLR", "/VPPslashMCLR", "x{slash}y"], set(), True),
    ("Brace", "M{x}", ["M{x}", "Mx", "Q{x}"], set(), True),
    # Round 6: a possessive quantifier agreed with KiCad but is outside the allowlist (two quantifiers in a row).
    ("Possessive", "PA*+", ["PAA", "P"], {"PAA", "P"}, True),
    ("Lazy", "LZ.*?9", ["LZa9", "LZ9x"], {"LZa9"}, False),
    ("Space", " SP1", [" SP1", "SP1"], {" SP1"}, False),
    ("Comma", "C1,C2", ["C1,C2", "C1"], {"C1,C2"}, False),
    # Delta review 2: KiCad's \w \d \s \W \S are ASCII-only; Python's re is Unicode by default.
    ("WordAscii", r"W\w", ["WÄ", "Wµ", "W٣", "Wa"], {"Wa"}, False),
    ("DigitAscii", r"D\d", ["D٣", "D１", "D1"], {"D1"}, False),
    ("SpaceAscii", r"S\s", ["S\xa0", "S "], {"S "}, False),
    ("NonWordAscii", r"X\W", ["XÄ", "X-", "Xa"], {"XÄ", "X-"}, False),
    ("NonSpaceAscii", r"Z\S", ["Z\xa0", "Zz", "Z "], {"Z\xa0", "Zz"}, False),
    # Delta review 2: KiCad adds "^" unless the pattern starts with "^", and "$" unless it ends
    # with "$" -- an escaped "\$" too, which leaves the end open.
    ("OpenEnd", r"VFF\$", ["VFF$", "VFF$X", "VFF"], {"VFF$", "VFF$X"}, False),
    ("AltEnd", r"VG$|VH\$", ["VG", "VGX", "VH$", "VH$X", "XVH$"], {"VG", "VH$", "VH$X"}, False),
    # Round 3: KiCad and today's Python agree here, but Python warns that it may read "[[" as a
    # nested set in future: refused rather than left to a silent change of meaning.
    ("NestedSet", "N[[]", ["N[", "N]"], {"N["}, True),
    # Delta review 2: KiCad never gives the net named "" (net 0) a class, even through "*".
    ("Star", "*", ["", "A"], {"A"}, False),
]


@pytest.fixture(scope="module")
def regex_oracle(tmp_path_factory, kicad_python) -> tuple[dict, dict]:
    """One project with every row's class and pattern, and KiCad's answer for every net."""
    classes = [(c, i + 1, (), {"track_width": round(0.3 + 0.005 * i, 3)}) for i, (c, *_rest) in enumerate(REGEX_ROWS)]
    board = _project(tmp_path_factory.mktemp("regex_oracle") / "rx", GOLDEN, classes,
                     [(c, p) for c, p, *_rest in REGEX_ROWS])
    data = board_rules._read_project(board.with_suffix(".kicad_pro"))
    return data, _kicad(board, [n for _c, _p, nets, *_rest in REGEX_ROWS for n in nets])


@pytest.mark.parametrize("cls, pattern, nets, kicad_matches, refused", REGEX_ROWS, ids=[r[0] for r in REGEX_ROWS])
def test_a_pattern_is_resolved_like_kicad_or_refused(regex_oracle, cls, pattern, nets, kicad_matches, refused) -> None:
    data, kicad = regex_oracle
    assert {n for n in nets if cls in kicad[n]["name"].split(",")} == kicad_matches  # KiCad's answer
    alone = copy.deepcopy(data)  # the row's pattern alone: another row's refusal must not decide it
    alone["net_settings"]["netclass_patterns"] = [{"netclass": cls, "pattern": pattern}]
    for net in nets:
        if refused:
            with pytest.raises(board_rules.UnsupportedNetclassPattern) as caught:
                board_rules._effective_netclass(alone, net)
            assert (caught.value.netclass, caught.value.pattern) == (cls, pattern)
            assert repr(cls) in str(caught.value) and repr(pattern) in str(caught.value)
            with pytest.raises(board_rules.UnsupportedNetclassPattern):
                board_rules._net_value(alone, net, "track_width")
        else:
            assert (cls in board_rules._effective_netclass(alone, net).split(",")) == (net in kicad_matches), net


def test_propose_board_rules_refuses_a_pattern_python_and_kicad_read_differently(tmp_path) -> None:
    """The rules diff and the winner check cannot judge a net whose class KiCad resolves
    differently: an error naming the class and the pattern, never a guessed diff."""
    board = _project(tmp_path / "proj", GOLDEN, [("Pwr", 0, (), {"track_width": 0.41})], [("Pwr", "(?i)vdd")])
    proposal = board_rules.propose_board_rules(board, min_track_width=0.21, output_dir=tmp_path / "out")
    assert proposal["status"] == "error", proposal
    assert (proposal["netclass"], proposal["pattern"]) == ("Pwr", "(?i)vdd")
    assert "'Pwr'" in proposal["reason"] and "'(?i)vdd'" in proposal["reason"]
    assert not (tmp_path / "out").exists()


def test_propose_board_rules_refuses_a_requested_net_that_reads_differently_as_a_pattern(
    tmp_path, monkeypatch
) -> None:
    """A requested net is written as a pattern (``_apply_request``): a net named ``R{2}``
    is a bound to Python and something else to KiCad. The new project is checked before any
    net is resolved: an error naming class and pattern, never an exception or a guessed winner."""
    board = _project(tmp_path / "proj", GOLDEN, [], [])
    real = board_rules._board_net_names
    monkeypatch.setattr(board_rules, "_board_net_names", lambda path: real(path) | {"R{2}"})
    proposal = board_rules.propose_board_rules(
        board, netclasses=[{"name": "Odd", "track_width": 0.3, "nets": ["R{2}"]}], output_dir=tmp_path / "out")
    assert proposal["status"] == "error", proposal
    assert (proposal["netclass"], proposal["pattern"]) == ("Odd", "R{2}")


def test_the_refusal_points_to_kicad_and_listing_the_nets_does_not_get_round_it(tmp_path) -> None:
    """Delta review 2, finding 3: no tool edits an existing pattern, and listing the class's nets
    keeps the old pattern (``_apply_request``), so it errors again. The reason sends the user to
    KiCad's Board Setup and never invites a hand edit of the ``.kicad_pro``."""
    board = _project(tmp_path / "proj", GOLDEN, [("Pwr", 0, (), {"track_width": 0.41})], [("Pwr", "(?i)vdd")])
    proposal = board_rules.propose_board_rules(
        board, netclasses=[{"name": "Pwr", "track_width": 0.41, "nets": ["VCC"]}], output_dir=tmp_path / "out")
    assert proposal["status"] == "error", proposal
    assert (proposal["netclass"], proposal["pattern"]) == ("Pwr", "(?i)vdd")
    assert "Board Setup" in proposal["reason"] and "not be edited by hand" in proposal["reason"]
    assert "list the nets" not in proposal["reason"]
    assert not (tmp_path / "out").exists()


def test_a_divergent_pattern_after_a_plain_one_still_refuses(tmp_path) -> None:
    """Delta review 2, finding 5: every pattern is checked, not only the first."""
    board = _project(tmp_path / "proj", GOLDEN,
                     [("Sig", 0, (), {"track_width": 0.25}), ("Pwr", 1, (), {"track_width": 0.41})],
                     [("Sig", "GND"), ("Pwr", "(?i)vdd")])
    data = board_rules._read_project(board.with_suffix(".kicad_pro"))
    with pytest.raises(board_rules.UnsupportedNetclassPattern) as caught:
        board_rules._effective_netclass(data, "GND")
    assert (caught.value.netclass, caught.value.pattern) == ("Pwr", "(?i)vdd")
    proposal = board_rules.propose_board_rules(board, min_track_width=0.21, output_dir=tmp_path / "out")
    assert proposal["status"] == "error", proposal
    assert (proposal["netclass"], proposal["pattern"]) == ("Pwr", "(?i)vdd")
    assert "'Pwr'" in proposal["reason"]


def test_the_empty_net_keeps_the_default_class_like_kicad(regex_oracle) -> None:
    """Delta review 2, finding 4: KiCad gives the net named "" (net 0) no class, even through
    ``*`` (the ``Star`` row's KiCad answer); its values are Default's."""
    data, kicad = regex_oracle
    assert kicad[""]["name"] == "Default"
    alone = copy.deepcopy(data)
    alone["net_settings"]["netclass_patterns"] = [{"netclass": "Star", "pattern": "*"}]
    assert board_rules._effective_netclass(alone, "") == "Default"
    default = board_rules._class_by_name(alone, "Default")["track_width"]
    assert board_rules._net_value(alone, "", "track_width") == (default, "Default")
    assert board_rules._net_value(alone, "A", "track_width")[1] == "Star"


# Measured 2026-10-21 (round 3): a net that no pattern matches by its whole name is read by KiCad
# as a bus group when it holds braces, and gets a class when every member inside the first
# "{...}" (split at spaces and commas) gets it: ``x`` puts ``Q{x}`` and ``Q{x}Z{y}`` in the
# class, not ``Q{x y}``; ``?`` puts ``Q{x y}`` in it. Not reproduced here: such a net refuses
# whenever a pattern matches any text inside its braces, and otherwise stays in Default as in
# KiCad (rung 2's ``/VPP{slash}MCLR`` under the patterns GND and VCC).
BRACE_NET_ROWS = [  # (pattern, nets): each pattern in its own project beside a class it never matches
    ("x", ["Q{x}", "Q{x y}", "Q{x}Z{y}", "Q{y}Z{x}", "Qx", "x"]),
    ("?", ["Q{x y}", "Q{xy}", "{slash}", "Q{}", "Q{~{CS}}", "~{RESET}", "Q{x}~{CS}", "~{CS}Q{x}"]),
    ("slash", ["/VPP{slash}MCLR", "unconnected-(U4-S{slash}S-Pad4)", "/VPP/MCLR"]),
    ("GND", ["/VPP{slash}MCLR", "X{GND}", "{GND VCC}", "GND"]),
    # Round 4 (delta review 3, L2): an overbar "~{...}" is no bus group; a plain group beside it is.
    ("CS", ["~{CS}", "/~{CS}", "Net-(U1-~{CS})", "Q{~{CS}}", "A~{CS}", "Q{CS}", "~{CS}{CS}", "~{X}{CS}"]),
    ("*CS", ["~{CS}", "/~{CS}", "Net-(U1-~{CS})", "Q{~{CS}}", "~CS", "Q{CS}"]),
    # Round 5 (delta review 4): a bus vector "D[0..1]", bare or in a group, is classed through
    # its expanded members D0, D1 when all of them share the class.
    ("D?", ["D[0..1]", "Q{D[0..1]}", "{D[0..1]}", "D[1..0]", "D[0..2]", "D[0..10]", "Q{D[0..1] E}", "D0"]),
]
OVERBAR_ONLY = ["~{CS}", "/~{CS}", "Net-(U1-~{CS})", "Q{~{CS}}", "A~{CS}", "~{RESET}"]


@pytest.fixture(scope="module")
def brace_net_oracle(tmp_path_factory, kicad_python) -> list[tuple[str, dict, dict]]:
    root = tmp_path_factory.mktemp("brace_net_oracle")
    out = []
    for i, (pattern, nets) in enumerate(BRACE_NET_ROWS):
        board = _project(root / f"b{i}", GOLDEN, [("K", 1, (), {"track_width": 0.33}), ("S", 2, (), {"track_width": 0.34})],
                         [("K", pattern), ("S", "Zzz")])
        out.append((pattern, board_rules._read_project(board.with_suffix(".kicad_pro")), _kicad(board, nets)))
    return out


def test_a_net_with_braces_is_resolved_like_kicad_or_refused(brace_net_oracle) -> None:
    for pattern, data, kicad in brace_net_oracle:
        for net, answer in kicad.items():
            try:
                ours = board_rules._effective_netclass(data, net)
            except board_rules.UnsupportedNetclassPattern as exc:
                assert (exc.netclass, exc.pattern, exc.net) == ("K", pattern, net)
                assert repr(net) in str(exc) and "Board Setup" in str(exc)
                continue
            assert ours == answer["name"], (pattern, net)
    by_pattern = {pattern: kicad for pattern, _data, kicad in brace_net_oracle}
    # KiCad's own answer, the reason for refusing: classed through a member, never through the whole name.
    assert by_pattern["x"]["Q{x}"]["name"] == "K" and by_pattern["x"]["Q{x y}"]["name"] == "Default"
    assert by_pattern["slash"]["/VPP{slash}MCLR"]["name"] == "K"


def test_an_overbar_group_is_no_bus_group_like_kicad(brace_net_oracle) -> None:
    """Delta review 3, L2: KiCad keeps ``~{CS}``, ``/~{CS}``, ``Net-(U1-~{CS})`` and ``Q{~{CS}}``
    in Default under ``CS`` and ``*CS``; ours agrees without a false bus-group refusal. A plain
    group before or after an overbar is still a bus group (``~{CS}{CS}``, ``Q{x}~{CS}``)."""
    for pattern, data, kicad in brace_net_oracle:
        for net in OVERBAR_ONLY:
            if net in kicad:
                assert board_rules._effective_netclass(data, net) == kicad[net]["name"], (pattern, net)
    by_pattern = {pattern: (data, kicad) for pattern, data, kicad in brace_net_oracle}
    data, kicad = by_pattern["CS"]
    assert kicad["~{CS}{CS}"]["name"] == "K" and kicad["~{X}{CS}"]["name"] == "K"
    for net in ("Q{CS}", "~{CS}{CS}", "~{X}{CS}"):
        with pytest.raises(board_rules.UnsupportedNetclassPattern):
            board_rules._effective_netclass(data, net)
    data, kicad = by_pattern["?"]
    assert kicad["Q{x}~{CS}"]["name"] == "K" and kicad["~{CS}Q{x}"]["name"] == "K"
    with pytest.raises(board_rules.UnsupportedNetclassPattern):
        board_rules._effective_netclass(data, "~{CS}Q{x}")


def test_a_bus_vector_net_is_classed_like_kicad_or_refused(brace_net_oracle) -> None:
    """Delta review 4, finding 1: KiCad puts ``D[0..1]`` and ``Q{D[0..1]}`` in the class of ``D?``
    through their expanded members; ours must give that class or refuse, never Default."""
    data, kicad = {pattern: (d, k) for pattern, d, k in brace_net_oracle}["D?"]
    for net in ("D[0..1]", "Q{D[0..1]}", "{D[0..1]}", "D[1..0]", "D[0..2]"):
        assert kicad[net]["name"] == "K", net
        try:
            ours = board_rules._effective_netclass(data, net)
        except board_rules.UnsupportedNetclassPattern as exc:
            assert exc.net == net and "[a..b]" in str(exc) and "whole name" in str(exc)
            continue
        assert ours == "K", net
    assert board_rules._effective_netclass(data, "D0") == "K"


def test_a_bus_vector_net_refuses_under_any_pattern_or_assignment_and_not_without() -> None:
    """The refusal is broad on purpose (every pattern or legacy assignment could class a member);
    a pattern that matches the whole name wins in KiCad (measured: ``*..1]`` over ``D?``), and a
    project with no pattern and no assignment classes nothing."""
    classes = [{"name": "Default", "track_width": 0.2}, {"name": "K", "track_width": 0.4}, {"name": "S", "track_width": 0.5}]

    def project(patterns, legacy=None):
        settings = {"classes": classes, "netclass_patterns": [{"netclass": c, "pattern": p} for c, p in patterns]}
        if legacy is not None:
            settings["netclass_assignments"] = legacy
        return {"net_settings": settings}

    for data in (project([("K", "Zzz")]), project([], {"D0": "K", "D1": "K"})):
        with pytest.raises(board_rules.UnsupportedNetclassPattern) as caught:
            board_rules._effective_netclass(data, "D[0..1]")
        assert caught.value.net == "D[0..1]"
    # S's own values first, Default only filling keys these bare classes leave undefined; never K.
    assert board_rules._netclass_chain(project([("K", "D?"), ("S", "D*")]), "D[0..1]")[0] == "S"
    assert "K" not in board_rules._netclass_chain(project([("K", "D?"), ("S", "D*")]), "D[0..1]")
    assert board_rules._netclass_chain(project([]), "D[0..1]") == ["Default"]


def test_rung_2_pic_programmer_resolves_every_net_like_kicad(kicad_demo, kicad_python) -> None:
    """Delta review 3, L3: rung 2's board (KiCad's pic_programmer demo, a copy): every net,
    including its ``{slash}`` nets under the patterns GND and VCC, resolves without a refusal
    and exactly as KiCad resolves it."""
    from kicad_mcp import pour_step

    board = kicad_demo("pic_programmer") / "pic_programmer.kicad_pcb"
    data = board_rules._read_project(board.with_suffix(".kicad_pro"))
    nets = sorted(pour_step.board_net_names(board))
    assert any("{slash}" in n for n in nets) and len(nets) > 100
    kicad = _kicad(board, nets)
    differ = {n: (board_rules._effective_netclass(data, n), kicad[n]["name"]) for n in nets}
    assert {n: pair for n, pair in differ.items() if pair[0] != pair[1]} == {}


def test_propose_board_rules_explains_a_requested_braced_net_as_its_own_request(tmp_path, monkeypatch) -> None:
    """Delta review 3, B1: a requested net is written as a pattern; with braces it is refused,
    but the reason is about the request (assign the class in KiCad), never "change or remove
    this pattern", which the project does not hold."""
    board = _project(tmp_path / "proj", GOLDEN, [], [])
    real = board_rules._board_net_names
    monkeypatch.setattr(board_rules, "_board_net_names", lambda path: real(path) | {"/~{RESET}", "/VPP{slash}MCLR"})
    for net in ("/~{RESET}", "/VPP{slash}MCLR"):
        proposal = board_rules.propose_board_rules(
            board, netclasses=[{"name": "Pwr", "track_width": 0.4, "nets": [net]}], output_dir=tmp_path / "out")
        assert proposal["status"] == "error", proposal
        assert (proposal["net"], proposal["netclass"], proposal["pattern"]) == (net, "Pwr", net)
        reason = proposal["reason"]
        assert repr(net) in reason and "requested" in reason
        assert "Change or remove this pattern" not in reason
        assert "schematic" in reason and "not be edited by hand" in reason
        assert not (tmp_path / "out").exists()


def test_propose_board_rules_keeps_the_pattern_reason_for_a_pattern_already_in_the_project(tmp_path, monkeypatch) -> None:
    """The same braced text already in the project is the user's pattern, even when the request
    names it as a net: change it in KiCad."""
    board = _project(tmp_path / "proj", GOLDEN, [("Pwr", 0, (), {"track_width": 0.41})], [("Pwr", "/~{RESET}")])
    real = board_rules._board_net_names
    monkeypatch.setattr(board_rules, "_board_net_names", lambda path: real(path) | {"/~{RESET}"})
    for request in ({"min_track_width": 0.21},
                    {"netclasses": [{"name": "Hi", "track_width": 0.4, "nets": ["/~{RESET}"]}]}):
        proposal = board_rules.propose_board_rules(board, output_dir=tmp_path / "out", **request)
        assert proposal["status"] == "error", proposal
        assert proposal["pattern"] == "/~{RESET}" and "net" not in proposal
        assert "Change or remove this pattern in KiCad (Board Setup > Net Classes)" in proposal["reason"]


def test_propose_board_rules_explains_a_requested_net_another_net_names_in_its_braces(tmp_path, monkeypatch) -> None:
    """B1, second path: the requested ``/CTL`` has no braces, but its new pattern matches the
    member of the board net ``Q{/CTL}``; the reason is still about the request."""
    board = _project(tmp_path / "proj", GOLDEN, [], [])
    real = board_rules._board_net_names
    monkeypatch.setattr(board_rules, "_board_net_names", lambda path: real(path) | {"/CTL", "Q{/CTL}"})
    proposal = board_rules.propose_board_rules(
        board, netclasses=[{"name": "Pwr", "track_width": 0.4, "nets": ["/CTL"]}], output_dir=tmp_path / "out")
    assert proposal["status"] == "error", proposal
    assert (proposal["net"], proposal["bus_net"], proposal["pattern"]) == ("/CTL", "Q{/CTL}", "/CTL")
    assert "'Q{/CTL}'" in proposal["reason"] and "Change or remove this pattern" not in proposal["reason"]
    assert not (tmp_path / "out").exists()


def test_propose_board_rules_keeps_the_project_reason_when_the_old_project_already_refuses(tmp_path, monkeypatch) -> None:
    """Delta review 4, finding 2: the legacy assignment GND -> Pwr already refuses ``Q{GND}`` in
    the project as it is; a request naming GND must not be blamed for it."""
    board = _project(tmp_path / "proj", GOLDEN, [("Pwr", 0, (), {"track_width": 0.41}), ("Sig", 1, (), {})],
                     [("Sig", "ZZZ")])
    pro = board.with_suffix(".kicad_pro")
    data = board_rules._read_project(pro)
    data["net_settings"]["netclass_assignments"] = {"GND": ["Pwr"]}
    write_layer.save_project(data, pro)
    real = board_rules._board_net_names
    monkeypatch.setattr(board_rules, "_board_net_names", lambda path: real(path) | {"GND", "Q{GND}"})
    answers = []
    for request in ({"min_track_width": 0.21}, {"netclasses": [{"name": "Sig", "nets": ["GND"]}]}):
        proposal = board_rules.propose_board_rules(board, output_dir=tmp_path / "out", **request)
        assert proposal["status"] == "error", proposal
        assert proposal["net"] == "Q{GND}" and "bus_net" not in proposal
        assert "requested" not in proposal["reason"] and "other requested nets" not in proposal["reason"]
        answers.append(proposal)
    assert answers[0] == answers[1]
    assert not (tmp_path / "out").exists()


def test_propose_board_rules_explains_a_requested_net_that_makes_a_bus_vector_unresolvable(tmp_path, monkeypatch) -> None:
    """With no pattern in the project, a bus-vector net resolves to Default; the first pattern a
    request adds makes it unresolvable, and the reason says so about the request."""
    board = _project(tmp_path / "proj", GOLDEN, [], [])
    real = board_rules._board_net_names
    monkeypatch.setattr(board_rules, "_board_net_names", lambda path: real(path) | {"/CTL", "D[0..1]"})
    proposal = board_rules.propose_board_rules(
        board, netclasses=[{"name": "Pwr", "track_width": 0.4, "nets": ["/CTL"]}], output_dir=tmp_path / "out")
    assert proposal["status"] == "error", proposal
    assert (proposal["net"], proposal["bus_net"]) == ("/CTL", "D[0..1]")
    reason = proposal["reason"]
    assert "'D[0..1]'" in reason and "[a..b]" in reason and "inside the braces" not in reason
    assert "Change or remove this pattern" not in reason
    assert not (tmp_path / "out").exists()


def test_a_net_with_braces_whose_members_no_pattern_matches_stays_in_default(brace_net_oracle) -> None:
    """Rung 2's case: no refusal where KiCad gives Default (no pattern matches a member)."""
    data = {pattern: d for pattern, d, _kicad in brace_net_oracle}
    assert board_rules._effective_netclass(data["GND"], "/VPP{slash}MCLR") == "Default"
    assert board_rules._effective_netclass(data["?"], "{slash}") == "Default"
    assert board_rules._effective_netclass(data["x"], "Qx") == "Default"
    with pytest.raises(board_rules.UnsupportedNetclassPattern):
        board_rules._effective_netclass(data["slash"], "/VPP{slash}MCLR")


def test_propose_board_rules_refuses_a_board_net_kicad_would_class_through_its_members(tmp_path, monkeypatch) -> None:
    """The rules diff resolves every board net before and after: a net KiCad classes through its
    bus members is an error naming the net, the class and the pattern, never a guessed diff."""
    board = _project(tmp_path / "proj", GOLDEN, [("Pwr", 0, (), {"track_width": 0.41})], [("Pwr", "VCC")])
    real = board_rules._board_net_names
    monkeypatch.setattr(board_rules, "_board_net_names", lambda path: real(path) | {"Q{VCC}"})
    proposal = board_rules.propose_board_rules(board, min_track_width=0.21, output_dir=tmp_path / "out")
    assert proposal["status"] == "error", proposal
    assert (proposal["net"], proposal["netclass"], proposal["pattern"]) == ("Q{VCC}", "Pwr", "VCC")
    assert "Board Setup" in proposal["reason"] and "not be edited by hand" in proposal["reason"]
    assert not (tmp_path / "out").exists()


def test_a_legacy_assignment_for_a_member_refuses_too() -> None:
    """``netclass_assignments`` gives a member its class as a pattern does: the same refusal."""
    data = {"net_settings": {"classes": [{"name": "Default", "track_width": 0.2}, {"name": "Pwr", "track_width": 0.4}],
                             "netclass_patterns": [], "netclass_assignments": {"VCC": "Pwr"}}}
    with pytest.raises(board_rules.UnsupportedNetclassPattern) as caught:
        board_rules._effective_netclass(data, "Q{VCC}")
    assert (caught.value.net, caught.value.netclass) == ("Q{VCC}", "Pwr")
    assert board_rules._effective_netclass(data, "Q{GND}") == "Default"


# A generated sweep (delta review 2): every pattern alone in its class, every net, against KiCad.
SWEEP_PATTERNS = [
    r"\w+", r"A\w", r"\d", r"\D+", r"\s", r"A\sB", r"\S+", r"\W", r"A\WB", r"W\w", r"D\d", r"S\s", r"X\W", r"Z\S",
    ".", "..", "A.", ".*Ä", "Ä*", "Ä", "?", "??", "A?", "*Ä*", "µ?", "[A-Z]+", "[^A]", "[ÄÖÜ]+", "[a-zä]", "[^a-z]+",
    "Ä|µ1", "^A", "A$", "^A$", r"A\$", r"VFF\$", r"VG$|VH\$", r"^\$", r"\^A", "VCC_.*", "V.C*", "+5V", "+?V?",
    "/x/*", "Net-(U1-Pad1)", "Net-*", "😀", "?😀", "A😀", ".😀", "😀?", "ß", "S+", "[ß]", "x{slash}y", "GND|",
    "|GND", "*", ".*", "UA|", "A|", "^", "$", "^$", "A*B", "A.B", r"A\.B", "[[]", "[]]", "a]", "A{", "A}", "(A)",
    "(A|B)C", r"A\\", r"[\d]", r"[\w]+", "[^[:alpha:]]", "A**", "A+?", "(?:A)",
]
SWEEP_NETS = [
    "", "A", "a", "Ä", "ä", "AÄ", "AA", "A B", "A\xa0B", "A\tB", "AB", "A_B", "A.B", "AxB", "A$", "A$X", "$", "^A",
    "µ1", "µ", "٣", "D٣", "D1", "D１", "１", "WÄ", "Wµ", "W٣", "Wa", "S\xa0", "S ", "XÄ", "X-", "Z\xa0", "Zz",
    "VFF$", "VFF$X", "VFF", "VG", "VGX", "VH$", "VH$X", "XVH$", "+5V", "5V", "/x/y", "/x{slash}y", "x{slash}y",
    "x/y", "VCC", "VCC_3V3", "VCCC", "GND", "XGND", "Net-(U1-Pad1)", "Net-U1-Pad1", "😀", "A😀", "AA😀", "😀😀",
    "ß", "SS", "ÄÖÜ", "ǅ", "Ⅻ", "x", "[", "]", "a]", "A{", "A}", "(A)", "AC", "BC", "UA", "A\\", "9", "٣9",
    "Q{x}", "Q{x y}", "/VPP{slash}MCLR", "~{RESET}",
]


@pytest.fixture(scope="module")
def sweep_oracle(tmp_path_factory, kicad_python) -> tuple[dict, dict]:
    classes = [(f"K{i:02d}", i + 1, (), {"track_width": 0.3}) for i in range(len(SWEEP_PATTERNS))]
    board = _project(tmp_path_factory.mktemp("sweep_oracle") / "sw", GOLDEN, classes,
                     [(f"K{i:02d}", p) for i, p in enumerate(SWEEP_PATTERNS)])
    return board_rules._read_project(board.with_suffix(".kicad_pro")), _kicad(board, SWEEP_NETS)


def test_a_generated_sweep_of_patterns_agrees_with_kicad_or_refuses(sweep_oracle) -> None:
    """ASCII and non-ASCII nets (letters, digits, no-break space, emoji, ``$``, braces) under
    wildcards and regexes: each pattern either refuses or puts exactly KiCad's nets in its class."""
    data, kicad = sweep_oracle
    disagreements, refused = [], 0
    for i, pattern in enumerate(SWEEP_PATTERNS):
        cls = f"K{i:02d}"
        alone = copy.deepcopy(data)
        alone["net_settings"]["netclass_patterns"] = [{"netclass": cls, "pattern": pattern}]
        for net in SWEEP_NETS:
            try:
                ours = cls in board_rules._effective_netclass(alone, net).split(",")
            except board_rules.UnsupportedNetclassPattern:
                refused += 1
                continue
            if ours != (cls in kicad[net]["name"].split(",")):
                disagreements.append((pattern, net, "ours" if ours else "KiCad's"))
    assert disagreements == []  # (pattern, net, whose class it is)
    assert refused < len(SWEEP_PATTERNS) * len(SWEEP_NETS) // 4  # most of the sweep is judged, not refused


def _tweak(board: Path, change) -> dict:
    pro = board.with_suffix(".kicad_pro")
    data = board_rules._read_project(pro)
    change(data)
    write_layer.save_project(data, pro)
    return board_rules._read_project(pro)


@pytest.fixture(scope="module")
def priority_oracle(tmp_path_factory, kicad_python) -> dict:
    root = tmp_path_factory.mktemp("priority_oracle")
    v4 = _project(root / "v4", GOLDEN, [("Y1", 1, (), {"track_width": 0.31}), ("Y0", 0, (), {"track_width": 0.30})],
                  [("Y1", "H1"), ("Y0", "H1")])
    v4_data = _tweak(v4, lambda d: d["net_settings"]["meta"].update(version=4))
    nop = _project(root / "nop", GOLDEN,
                   [("Zero", 0, (), {"track_width": 0.31}), ("NoP", None, (), {"track_width": 0.32}),
                    ("Neg", -1, (), {"track_width": 0.33})],
                   [("Zero", "PR*"), ("NoP", "PR*"), ("Neg", "PR2")])
    nop_data = board_rules._read_project(nop.with_suffix(".kicad_pro"))
    return {"v4": (v4_data, _kicad(v4, ["H1"])), "nop": (nop_data, _kicad(nop, ["PR1", "PR2"]))}


def test_a_version_4_project_ranks_by_priority_like_kicad(priority_oracle) -> None:
    """From ``net_settings`` version 4 on, the ``priority`` key ranks the classes, not the file order."""
    data, kicad = priority_oracle["v4"]
    assert data["net_settings"]["meta"]["version"] == 4
    assert kicad["H1"]["name"] == "Y0,Y1" and kicad["H1"]["track_width"] == 0.30
    assert board_rules._effective_netclass(data, "H1") == "Y0,Y1"
    assert board_rules._net_value(data, "H1", "track_width") == (0.30, "Y0")


def test_a_class_without_priority_ranks_as_minus_one_like_kicad(priority_oracle) -> None:
    """A version-5 class with no ``priority`` key ranks as -1: before priority 0, tied with -1
    (and then ordered by name)."""
    data, kicad = priority_oracle["nop"]
    assert "priority" not in board_rules._class_by_name(data, "NoP")
    assert kicad["PR1"]["name"] == "NoP,Zero" and kicad["PR1"]["track_width"] == 0.32
    assert kicad["PR2"]["name"] == "Neg,NoP,Zero" and kicad["PR2"]["track_width"] == 0.33
    for net in ("PR1", "PR2"):
        assert board_rules._effective_netclass(data, net) == kicad[net]["name"]
        assert board_rules._net_value(data, net, "track_width")[0] == kicad[net]["track_width"]


def test_without_kicad_python_the_oracle_fails_under_the_required_flag() -> None:
    """Delta review 1, finding 3: the gate run sets ``KICAD_DEMOS_REQUIRED``; there the oracle
    tests fail instead of skipping quietly. An ad-hoc run still skips with the reason."""
    with pytest.raises(pytest.fail.Exception, match="KICAD_DEMOS_REQUIRED"):
        _require_kicad_python(None, "not found here", {demos.REQUIRED_FLAG: "1"})
    with pytest.raises(pytest.skip.Exception, match="not found here"):
        _require_kicad_python(None, "not found here", {})
    assert _require_kicad_python(Path("python.exe"), None, {demos.REQUIRED_FLAG: "1"}) == Path("python.exe")


# --- fix round 6: an allowlist of pattern syntax, everything else refused -------------------------
# Delta review 5 found a third form (a vector ``[a..b]`` in a PATTERN, which KiCad expands as a
# bus: ``D[0..3]`` classes D0..D3) after the brace and the net-side vector. The design changed:
# a pattern is resolved here only when it is written in the small grammar
# ``board_rules._pattern_syntax_problem`` accepts; every other pattern refuses, fail-closed.

VECTOR_PATTERNS = ["D[0..3]", "D[0..11]", "?[0..1]", "*[0..1]", "D[3..0]", "D[00..03]", "[0..1]"]
ROUND6_NETS = [f"D{i}" for i in range(13)] + ["A1", "B1", "DX", "D[0..3]", "D[1]", "D0[0..1]"]


@pytest.mark.parametrize("pattern", VECTOR_PATTERNS)
def test_a_pattern_holding_a_bus_vector_refuses_for_every_net(pattern) -> None:
    """Delta review 5, finding A: KiCad classes D0..D3 under ``D[0..3]``, a character class would
    class D0 and D3 only. The pattern refuses, whatever net is asked."""
    data = {"net_settings": {"classes": [{"name": "Default", "track_width": 0.2}, {"name": "K", "track_width": 0.4}],
                             "netclass_patterns": [{"netclass": "K", "pattern": pattern}]}}
    for net in ("D1", "D2", "ZZZ", "D[0..3]"):
        with pytest.raises(board_rules.UnsupportedNetclassPattern) as caught:
            board_rules._effective_netclass(data, net)
        assert (caught.value.netclass, caught.value.pattern, caught.value.net) == ("K", pattern, None)
        assert repr(pattern) in str(caught.value) and "outside" in str(caught.value)


def test_a_requested_net_that_is_a_bus_vector_refuses_as_its_own_request(tmp_path, monkeypatch) -> None:
    """Delta review 5, the probe the review left open: a request naming the net ``D[0..1]`` adds
    that name as a pattern, which KiCad expands as a bus (D0, D1)."""
    board = _project(tmp_path / "proj", GOLDEN, [], [])
    real = board_rules._board_net_names
    monkeypatch.setattr(board_rules, "_board_net_names", lambda path: real(path) | {"D[0..1]"})
    proposal = board_rules.propose_board_rules(
        board, netclasses=[{"name": "Bus", "track_width": 0.3, "nets": ["D[0..1]"]}], output_dir=tmp_path / "out")
    assert proposal["status"] == "error", proposal
    assert (proposal["netclass"], proposal["pattern"], proposal["net"]) == ("Bus", "D[0..1]", "D[0..1]")
    reason = proposal["reason"]
    assert "'D[0..1]'" in reason and "outside" in reason and "Board Setup" in reason
    assert "Change or remove this pattern" not in reason
    assert not (tmp_path / "out").exists()


def test_no_refusal_claims_that_the_other_requested_nets_can_be_proposed(tmp_path, monkeypatch) -> None:
    """Delta review 5, finding B: on the bus-vector branch ANY added pattern refuses, so the old
    sentence was false there. Every branch of ``_requested_net_refusal`` now says only what holds."""
    board = _project(tmp_path / "proj", GOLDEN, [], [])
    real = board_rules._board_net_names
    monkeypatch.setattr(board_rules, "_board_net_names", lambda path: real(path) | {"/CTL", "D[0..1]", "Q{VCC}", "R{2}"})
    reasons = {}
    for label, nets in (("vector_board_net", ["/CTL"]), ("braced_request", ["R{2}"]), ("member", ["VCC"])):
        proposal = board_rules.propose_board_rules(
            board, netclasses=[{"name": "Pwr", "track_width": 0.4, "nets": nets}], output_dir=tmp_path / "out")
        assert proposal["status"] == "error", (label, proposal)
        reasons[label] = proposal["reason"]
    assert "'D[0..1]'" in reasons["vector_board_net"] and "'R{2}'" in reasons["braced_request"]
    for label, reason in reasons.items():
        assert "can be proposed" not in reason, label
    # what the vector branch says instead is true: while the board holds that net, a request that
    # adds a pattern is refused for it, whichever other nets the request names
    other = board_rules.propose_board_rules(
        board, netclasses=[{"name": "Pwr", "track_width": 0.4, "nets": ["GND"]}], output_dir=tmp_path / "out")
    assert other["status"] == "error" and other["bus_net"] == "D[0..1]"
    assert "any request that adds a net-class pattern" in reasons["vector_board_net"]


# Outside the allowlist: each is refused with a reason naming the pattern and saying why.
OUTSIDE_ALLOWLIST = [
    ("D[0..3]", "vector"), ("D[0:3]", "colon"), ("[0..1]", "vector"), ("D[a..c]", "vector"),
    ("x{slash}y", "brace"), ("R{2}", "brace"), ("A{", "brace"), ("A}", "brace"), ("{A,B}", "brace"),
    ("[[:alpha:]]", "posix"), ("[:alpha:]", "posix"), ("[[:digit:]]+", "posix"), ("[[.a.]]", "posix"), ("[[=a=]]", "posix"),
    ("(?i)vdd", "extension"), ("(?:A)", "extension"), ("(?=A)", "extension"),
    (r"\mVBB", "escape"), (r"\bVCC", "escape"), (r"D\d\Z", "escape"), (r"\1", "escape"), (r"\x41", "escape"),
    (r"\u0041", "escape"), ("\\é", "escape"), ("\\ ", "escape"), ("A\\", "escape"),
    ("A^B", "anchor"), ("A$B", "anchor"), ("A^", "anchor"), ("$A", "anchor"), ("^^A", "anchor"),
    ("A]", "bracket"), ("[A", "bracket"), ("[]", "bracket"), ("[]A]", "bracket"),
    ("[^]", "bracket"), ("[a-]", "range"), ("[-a]", "range"), ("[z-a]", "range"), ("[a-c-e]", "range"),
    ("[0-é]", "range"),
    ("[[]", "bracket"), ("[A[]", "bracket"), ("A**", "quantifier"), ("A+*", "quantifier"), ("PA*+", "quantifier"),
    ("A\n", "control"), ("A\tB", "control"), ("A\x00", "control"), ("A\x7f", "control"),
]
INSIDE_ALLOWLIST = [
    "", "GND", "+3V3", "/SPI_*", "Net-(U1-Pad1)", "D[0-7]", "D[0-9A-F]*", "VD[DX]", "AB|CD", "^A", "A$", "^A$", r"VFF\$",
    r"VG$|VH\$", "SIG?", "*", ".*", r"\d+", r"A\.B", "[^a-z]+", r"[\d]", "K[.]", "LZ.*?9", "Ä*", "\U0001f600",
    " SP1", "C1,C2", "(A|B)C", "Net-*", "Net-(D*", "(A", "A)", "A)B(", "?", "A+", "A?", "unconnected-(U4-Pad4)", "~RESET", "D[01]", "V[0-9]_[0-9]",
]


@pytest.mark.parametrize("pattern, kind", OUTSIDE_ALLOWLIST, ids=[repr(p) for p, _k in OUTSIDE_ALLOWLIST])
def test_a_pattern_outside_the_allowlist_refuses_naming_itself(pattern, kind) -> None:
    problem = board_rules._pattern_syntax_problem(pattern)
    assert problem, f"{pattern!r} ({kind}) was accepted"
    data = {"net_settings": {"classes": [{"name": "Default", "track_width": 0.2}, {"name": "K", "track_width": 0.4}],
                             "netclass_patterns": [{"netclass": "K", "pattern": pattern}]}}
    for net in ("GND", "D1", ""):
        with pytest.raises(board_rules.UnsupportedNetclassPattern) as caught:
            board_rules._effective_netclass(data, net)
        text = str(caught.value)
        assert repr(pattern) in text and "'K'" in text and "outside" in text and "Board Setup" in text
        assert (caught.value.netclass, caught.value.pattern) == ("K", pattern)


@pytest.mark.parametrize("pattern", INSIDE_ALLOWLIST, ids=[repr(p) for p in INSIDE_ALLOWLIST])
def test_a_pattern_inside_the_allowlist_is_accepted(pattern) -> None:
    assert board_rules._pattern_syntax_problem(pattern) is None


_FUZZ_TOKENS = [
    "A", "B", "D", "0", "1", "3", "_", ".", "*", "?", "+", "-", "/", "(", ")", "|", "^", "$", "[", "]", "\\", "{", "}",
    ":", ",", " ", "Ä", "\U0001f600", "..", "[0-3]", "[0..3]", "[A-C]", "[^A]", r"\d", r"\w", r"\s", r"\W", r"\.",
    r"\$", r"\m", "(?", "[:", ":]", "AB", "D1", "~",
]
_FUZZ_NET_TOKENS = ["A", "B", "D", "0", "1", "3", "_", ".", "-", "/", "(", ")", "[", "]", "{", "}", ":", ",", " ",
                    "Ä", "\U0001f600", "$", "^", "+", "~"]
_CONTROL = ("\n", "\t", "\x00", "\x7f")


def _fuzz() -> tuple[list[str], list[str]]:
    import random

    rng = random.Random(20261021)
    patterns = list(VECTOR_PATTERNS) + [p for p, _k in OUTSIDE_ALLOWLIST if not any(c in p for c in _CONTROL)]
    patterns += [p for p in INSIDE_ALLOWLIST if p]
    while len(patterns) < 420:
        patterns.append("".join(rng.choice(_FUZZ_TOKENS) for _ in range(rng.randint(1, 6))))
    nets = list(ROUND6_NETS) + ["", "GND", "A.B", "AB", "A B", "Net-(U1-Pad1)", "Net-U1-Pad1", "D[0-3]", "D-3", "A+", "AA",
                                "A$", "^A", "(A)", "A|B", "ÄÄ"]
    while len(nets) < 120:
        nets.append("".join(rng.choice(_FUZZ_NET_TOKENS) for _ in range(rng.randint(1, 5))))
    return list(dict.fromkeys(patterns)), list(dict.fromkeys(nets))


@pytest.fixture(scope="module")
def fuzz_oracle(tmp_path_factory, kicad_python) -> tuple[list[str], list[str], dict, dict]:
    patterns, nets = _fuzz()
    classes = [(f"K{i:03d}", i + 1, (), {"track_width": 0.3}) for i in range(len(patterns))]
    board = _project(tmp_path_factory.mktemp("fuzz_oracle") / "fz", GOLDEN, classes,
                     [(f"K{i:03d}", p) for i, p in enumerate(patterns)])
    return patterns, nets, board_rules._read_project(board.with_suffix(".kicad_pro")), _kicad(board, nets)


def test_a_generated_set_of_patterns_and_nets_never_disagrees_with_kicad(fuzz_oracle) -> None:
    """Delta review 5: the family of fail-opens is closed by construction. For ~400 patterns
    (the counterexamples, the outside-allowlist table, the inside table, seeded random token
    strings) against ~120 nets, each pattern alone: ours refuses, or its class holds exactly the
    nets KiCad puts in it. A refusal is allowed anywhere; a wrong class never."""
    patterns, nets, data, kicad = fuzz_oracle
    disagreements, accepted_patterns, judged = [], 0, 0
    for i, pattern in enumerate(patterns):
        cls = f"K{i:03d}"
        alone = copy.deepcopy(data)
        alone["net_settings"]["netclass_patterns"] = [{"netclass": cls, "pattern": pattern}]
        accepted = board_rules._pattern_syntax_problem(pattern) is None
        accepted_patterns += accepted
        for net in nets:
            try:
                ours = cls in board_rules._effective_netclass(alone, net).split(",")
            except board_rules.UnsupportedNetclassPattern:
                continue
            judged += 1
            if ours != (cls in kicad[net]["name"].split(",")):
                disagreements.append((pattern, net, "ours" if ours else "KiCad's"))
        if not accepted:  # an outside-the-allowlist pattern never reaches a verdict
            with pytest.raises(board_rules.UnsupportedNetclassPattern):
                board_rules._effective_netclass(alone, "GND")
    assert disagreements == []  # (pattern, net, whose class it is)
    assert accepted_patterns > 80 and judged > 5000  # the allowlist still judges real patterns


# Net side of the allowlist: bracket forms in a NET name, under patterns that could class a member.
# Measured 2026-10-21 (round 6): only ``[a..b]`` makes KiCad class a net through members
# (``D?`` classes ``D[0..1]``, ``?`` classes ``[0..1]``); ``D[0:1]``, ``D[1]``, ``D[0-1]``,
# ``D[0,1]``, ``D[0...1]`` and the rest stay in Default. A net holding ``..`` inside brackets refuses.
VECTORISH_NETS = [
    "D[0..1]", "D[0:1]", "D[1]", "D[0-1]", "D[0,1]", "D[0...1]", "D[ 0..1]", "D[0 ..1]", "D[0.. 1]", "D[a..b]", "D[]",
    "D[0..1]X", "XD[0..1]", "D[0..1]D[2..3]", "D[0..1][0..1]", "D[0..1,2]", "D[ 1]", "D[01]", "D[1..1]", "D[-1..1]",
    "D[1..-1]", "D[0..1.5]", "D[x]", "D[1][2]", "D[1", "D1]", "[1]", "[0..1]", "D[0..1]]", "D[[0..1]]", "D[0..00001]",
    "D[+1..2]", "D[0..1 ]", "D[0 ]", "D[0:1:2]", "/D[0..1]", "D[0..1]_N", "D[0..1]*", "D0[0..1]", "D[1:0]", "D[0;1]",
    "D[0|1]", "D(0..1)", "D<0..1>", "D0", "D1",
]
VECTORISH_PATTERNS = ["D?", "D*", "?", "D1", "D0", "*", "D[01]", "D[0-9]", "*1", "??", "D[0-9]*", "*D*", "D??", "D", ""]


@pytest.fixture(scope="module")
def vectorish_oracle(tmp_path_factory, kicad_python) -> list[tuple[str, dict, dict]]:
    root = tmp_path_factory.mktemp("vectorish_oracle")
    out = []
    for i, pattern in enumerate(VECTORISH_PATTERNS):
        board = _project(root / f"v{i}", GOLDEN, [("K", 1, (), {"track_width": 0.33}), ("S", 2, (), {"track_width": 0.34})],
                         [("K", pattern), ("S", "Zzz")])
        out.append((pattern, board_rules._read_project(board.with_suffix(".kicad_pro")), _kicad(board, VECTORISH_NETS)))
    return out


def test_a_net_with_brackets_is_classed_like_kicad_or_refused(vectorish_oracle) -> None:
    """Round 6: 45 bracket spellings x 15 patterns. Ours refuses or gives KiCad's class, never another;
    ``..`` between brackets is what refuses, and the empty pattern alone refuses nothing (KiCad: Default)."""
    judged = refused = 0
    for pattern, data, kicad in vectorish_oracle:
        for net in VECTORISH_NETS:
            try:
                ours = board_rules._effective_netclass(data, net)
            except board_rules.UnsupportedNetclassPattern:
                refused += 1
                assert board_rules._BUS_VECTOR.search(net), (pattern, net)
                continue
            judged += 1
            assert ours == kicad[net]["name"], (pattern, net)
    assert judged > 300 and refused > 100
    empty = {p: (d, k) for p, d, k in vectorish_oracle}[""]
    assert {k["name"] for k in empty[1].values()} == {"Default"}
    only_empty = copy.deepcopy(empty[0])
    only_empty["net_settings"]["netclass_patterns"] = [{"netclass": "K", "pattern": ""}]
    assert board_rules._effective_netclass(only_empty, "D[0..1]") == "Default"  # an empty pattern is none


# Fix round 7, F1: KiCad's ``.``, ``*`` and ``?`` match a newline, ours do not (measured by the
# review: ``*``, ``.`` and ``A.B`` put ``A\nB`` in the class, ours gave Default). The net side now
# refuses a name holding any control character, as the pattern side always did.
CONTROL_NETS = ["A\nB", "A\rB", "A\tB", "A\x0bB", "A\x0cB", "A\x01B", "A\x1bB", "A\x1fB", "A\x7fB", "\nA", "A\n", "\n"]
CONTROL_PATTERNS = ["*", ".", "A.B", "A?B", "A*", "?", ".*", "A", "GND", "A|B", "^A.B$"]


@pytest.fixture(scope="module")
def control_net_oracle(tmp_path_factory, kicad_python) -> list[tuple[str, dict, dict]]:
    root = tmp_path_factory.mktemp("control_net_oracle")
    out = []
    for i, pattern in enumerate(CONTROL_PATTERNS):
        board = _project(root / f"c{i}", GOLDEN, [("K", 1, (), {"track_width": 0.33}), ("S", 2, (), {"track_width": 0.34})],
                         [("K", pattern), ("S", "Zzz")])
        out.append((pattern, board_rules._read_project(board.with_suffix(".kicad_pro")), _kicad(board, CONTROL_NETS)))
    return out


def test_a_net_with_a_control_character_is_classed_like_kicad_or_refused(control_net_oracle) -> None:
    """Ours refuses or gives KiCad's class, never another (red on 138f13f: ``*`` vs ``A\nB``)."""
    refused = 0
    for pattern, data, kicad in control_net_oracle:
        for net in CONTROL_NETS:
            try:
                ours = board_rules._effective_netclass(data, net)
            except board_rules.UnsupportedNetclassPattern as exc:
                refused += 1
                assert exc.kind == "net" and exc.net == net
                continue
            assert ours == kicad[net]["name"], (pattern, net)
    assert refused > 0
    by_pattern = {p: k for p, _d, k in control_net_oracle}
    assert by_pattern["*"]["A\nB"]["name"] == "K"  # KiCad's own answer, the reason for refusing


def test_a_control_character_net_refuses_with_a_true_reason_and_only_under_a_pattern_or_assignment() -> None:
    classes = [{"name": "Default", "track_width": 0.2}, {"name": "K", "track_width": 0.4}]

    def project(patterns, legacy=None):
        settings = {"classes": classes, "netclass_patterns": [{"netclass": c, "pattern": p} for c, p in patterns]}
        if legacy is not None:
            settings["netclass_assignments"] = legacy
        return {"net_settings": settings}

    for data in (project([("K", "Zzz")]), project([], {"D0": "K"})):
        with pytest.raises(board_rules.UnsupportedNetclassPattern) as caught:
            board_rules._effective_netclass(data, "A\nB")
        assert caught.value.kind == "net" and repr("A\nB") in str(caught.value) and "control character" in str(caught.value)
    assert board_rules._netclass_chain(project([]), "A\nB") == ["Default"]


def test_a_requested_or_board_net_with_a_control_character_refuses_as_a_request(tmp_path, monkeypatch) -> None:
    board = _project(tmp_path / "proj", GOLDEN, [], [])
    real = board_rules._board_net_names
    monkeypatch.setattr(board_rules, "_board_net_names", lambda path: real(path) | {"/CTL", "A\nB"})
    proposal = board_rules.propose_board_rules(
        board, netclasses=[{"name": "Pwr", "track_width": 0.4, "nets": ["/CTL"]}], output_dir=tmp_path / "out")
    assert proposal["status"] == "error" and proposal["bus_net"] == "A\nB"
    assert repr("A\nB") in proposal["reason"] and "control character" in proposal["reason"]
    named = board_rules.propose_board_rules(
        board, netclasses=[{"name": "Pwr", "track_width": 0.4, "nets": ["A\nB"]}], output_dir=tmp_path / "out")
    assert named["status"] == "error" and "control character" in named["reason"]
    assert not (tmp_path / "out").exists()


# Fix round 7, F2: the bus-vector sentence names its exception; a requested name that is itself a
# pattern matching the vector net's whole name makes KiCad class that net by the name, no refusal.
def test_the_bus_vector_sentence_is_true_with_and_without_its_exception(tmp_path, monkeypatch) -> None:
    board = _project(tmp_path / "proj", GOLDEN, [], [])
    real = board_rules._board_net_names
    monkeypatch.setattr(board_rules, "_board_net_names", lambda path: real(path) | {"/CTL", "D[0..1]", "*"})
    refused = board_rules.propose_board_rules(
        board, netclasses=[{"name": "Pwr", "track_width": 0.4, "nets": ["/CTL"]}], output_dir=tmp_path / "out")
    assert refused["status"] == "error" and refused.get("bus_net") == "D[0..1]"
    assert "unless the added pattern matches this net's whole name" in refused["reason"]
    # the exception: the requested net "*" matches D[0..1] by its whole name, so no vector refusal fires
    star = board_rules.propose_board_rules(
        board, netclasses=[{"name": "Pwr", "track_width": 0.4, "nets": ["*"]}], output_dir=tmp_path / "out")
    assert star.get("bus_net") != "D[0..1]", star
