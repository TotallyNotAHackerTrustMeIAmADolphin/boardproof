"""The kicad-sourcing skill uses the same words as the tool descriptions (issue 51).

Each phrase is pinned in the skill AND in its own source string (found through the AST, so
implicit string concatenation is joined), so changing any one of them turns a test red.
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = " ".join((REPO / ".claude" / "skills" / "kicad-sourcing" / "SKILL.md").read_text(encoding="utf-8").split())
TOOLS = REPO / "src" / "kicad_mcp" / "toolsets"


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
