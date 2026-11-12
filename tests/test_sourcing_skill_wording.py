"""The kicad-sourcing skill uses the same words as the tool descriptions (issue 51).

Each phrase is pinned in the skill AND in its own source string (found through the AST, so
implicit string concatenation is joined), so changing any one of them turns a test red.
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = " ".join((REPO / ".claude" / "skills" / "kicad-sourcing" / "SKILL.md").read_text(encoding="utf-8").split())
TOOLS = REPO / "src" / "boardproof" / "toolsets"


def _strings(name):
    tree = ast.parse((TOOLS / name).read_text(encoding="utf-8"))
    return [" ".join(n.value.split()) for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _having(name, *needles):
    return [s for s in _strings(name) if all(n in s for n in needles)]


def test_skill_pins_unverified():
    assert "`false` means the numbering is UNVERIFIED (pin diagrams" in SKILL
    assert "NOT verified" not in SKILL


def test_inspect_datasheet_description_pins_unverified():
    assert _having("sourcing.py", "Extract structured content", "false means the numbering is UNVERIFIED")


def test_inspect_datasheet_docstring_pins_unverified():
    assert _having("sourcing.py", "the numbering is UNVERIFIED and a symbol must not be passed on it")


NOT_CAUGHT = "shifted sideways or rotated about the vertical axis is not caught by"


def test_skill_pins_not_caught_by():
    assert NOT_CAUGHT + " the machine check" in SKILL
    assert "still passes the machine check" not in SKILL


def test_confirm_adopt_parts_description_pins_not_caught_by():
    assert _having("library.py", "Apply a propose_adopt_parts proposal", NOT_CAUGHT + " it")


MISSING_PACKAGE = "If `fetch_part_assets` returns `status: incomplete` because the easyeda2kicad package is missing"


def test_skill_pins_missing_package_handling():
    assert MISSING_PACKAGE in SKILL
    assert "relay the install command from its `reason` to the user and do not fall back to `generate_footprint` silently" in SKILL


def test_description_and_skill_agree_on_missing_package():
    assert _having("sourcing.py", "Retrieve a ready KiCad symbol/footprint/3D model", "status incomplete with the install command")


def test_skill_pins_the_bom_check_and_its_offline_behaviour():
    assert "`check_bom_against_lcsc(sch_path)`" in SKILL
    assert "a package that disagrees with the footprint is an ERROR line" in SKILL
    assert "unknown means no data, never a pass" in SKILL
    assert "the stock only with `include_stock=true`" in SKILL
    assert "wide, exposed-pad and connector packages" in SKILL and "have unknown fit" in SKILL


def test_bom_check_description_and_skill_agree():
    assert _having("sourcing.py", "Read-only batch check of every placed part", "A package that disagrees with the placed footprint", "is an ERROR line")
    assert _having("sourcing.py", "Offline: stage and package fit use only the shipped index", "unknown means no data")
    assert _having("sourcing.py", "Offline behaviour, also stated in the result under data", "stage unknown (not Extended)")
    assert _having("sourcing.py", "Fit compares package family, pin count and imperial passive size only", "wide, exposed-pad and connector packages come back unknown")
    assert _having("sourcing.py", "A W (wide) suffix, a wide body, an exposed-pad (-nEP) variant", "an SMA coax connector")


def test_same_size_note_is_pinned_in_skill_description_and_docstring():
    note = "does not tell R, C, L, D and LED apart (a resistor part on an LED_0603 footprint is a match)"
    assert note in SKILL
    assert _having("sourcing.py", "Read-only batch check of every placed part", note)
    assert _having("sourcing.py", "Offline behaviour, also stated in the result under data", note)
