"""Publish-prep ticket 02a: rung 1's USB-C receptacle is this project's own symbol and footprint.

The old pair was an EasyEDA user-library export with no licence (it cannot be published). The
replacement is written by ``evals/rung1/build_usbc_asset.py`` through the write layer. Ticket 51
keeps the rung-1 board geometry fixed (nobody changes the board to help a router), so:

* the new footprint's pad table equals the old one within 0.001 mm (the old table is pinned below
  as a golden constant, read from the raw ``(pad ...)`` nodes of the footprint that rung 1 was
  pinned with: number, ``(at ...)``, ``(size ...)``, shape, layers, drill; its pad angles are all 0
  and J1 is placed unrotated, so board-absolute angles are 0 as well);
* every committed rung-1 board stage carries that table, and the placed stages keep every
  footprint position, the outline, the nets and the rules of the old build;
* the symbol's pins are typed (no "unspecified") and ERC has no violation left;
* the self-check of the asset writer fires on a corrupted write;
* no file of the old asset (by name, by content hash or by its library names) is under ``evals/``.

Everything is read from raw S-expressions (CLAUDE.md hard rule 2); ``kicad_tools`` convenience
properties are not used.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from kicad_mcp import kicad_cli, write_layer

ROOT = Path(__file__).resolve().parents[1]
RUNG1 = ROOT / "evals" / "rung1"
NAME = "rung1_usbc_attiny1616"
PCB = f"{NAME}.kicad_pcb"
STAGES = ["01_import", "02_outline", "03_placed", "04_rules", "07_poured"]
PLACED_STAGES = ["03_placed", "04_rules", "07_poured"]
TOLERANCE_MM = 0.001

FOOTPRINT_NAME = "USB_C_Receptacle_Shouhan_TYPE-C-6P"
LIB = "Rung1_USBC"
ASSET_FOOTPRINT = RUNG1 / "assets" / LIB / f"{LIB}.pretty" / f"{FOOTPRINT_NAME}.kicad_mod"
ASSET_SYMBOL = RUNG1 / "assets" / LIB / f"{LIB}.kicad_sym"

# The old footprint's pad table, from the raw (pad ...) nodes recorded before it was replaced:
# (number, type, shape, x, y, width, height, layers, drill). Pad (at ...) is relative to the
# footprint origin; every pad angle is 0.
SMD_LAYERS = ("F.Cu", "F.Mask", "F.Paste")
TAB_LAYERS = ("*.Cu", "*.Mask")
OLD_PADS = [
    ("B12", "smd", "rect", -2.70, -2.03, 0.8, 1.2, SMD_LAYERS, None),
    ("B9", "smd", "rect", -1.50, -2.03, 0.7, 1.2, SMD_LAYERS, None),
    ("A5", "smd", "rect", -0.50, -2.03, 0.7, 1.2, SMD_LAYERS, None),
    ("B5", "smd", "rect", 0.50, -2.03, 0.7, 1.2, SMD_LAYERS, None),
    ("A9", "smd", "rect", 1.50, -2.03, 0.7, 1.2, SMD_LAYERS, None),
    ("A12", "smd", "rect", 2.70, -2.03, 0.8, 1.2, SMD_LAYERS, None),
    ("7", "thru_hole", "oval", 4.32, 2.03, 1.1, 1.9, TAB_LAYERS, (0.599998, 1.299997)),
    ("7", "thru_hole", "oval", -4.32, 2.03, 1.1, 1.9, TAB_LAYERS, (0.599998, 1.299997)),
    ("7", "thru_hole", "oval", 4.32, -1.78, 1.1, 1.9, TAB_LAYERS, (0.599998, 1.299997)),
    ("7", "thru_hole", "oval", -4.32, -1.78, 1.1, 1.9, TAB_LAYERS, (0.599998, 1.299997)),
]
J1_NETS = {"B12": "GND", "A12": "GND", "7": "GND", "B9": "VBUS", "A9": "VBUS", "A5": "/CC1", "B5": "/CC2"}

# Footprint origins of the old build, board page coordinates (origin 128.5, 90 plus the layout).
OLD_POSITIONS = {
    "J1": (138.5, 108.0), "F1": (138.5, 103.0), "D1": (138.5, 98.5), "R1": (146.1, 105.8),
    "R2": (146.1, 108.6), "C1": (146.1, 102.5), "U2": (149.5, 98.0), "C2": (155.5, 98.0),
    "U1": (153.0, 107.5), "C3": (153.0, 111.2), "R3": (150.0, 103.5), "D2": (158.1, 106.2),
    "J2": (159.1, 97.5),
    "H1": (131.2, 92.7), "H2": (159.8, 92.7), "H3": (131.2, 110.3), "H4": (159.8, 110.3),
}
OLD_NETS = ["", "+3V3", "/CC1", "/CC2", "/LED_A", "/LED_CTL", "/UPDI", "/VBUS_F", "GND", "VBUS"] + [
    f"unconnected-(U1-{pin}-Pad{n})" for pin, n in (
        ("PA1", 20), ("PA2", 1), ("PA3", 2), ("PA4", 5), ("PA5", 6), ("PA6", 7), ("PB0", 14), ("PB1", 13),
        ("PB2", 12), ("PB3", 11), ("PB4", 10), ("PB5", 9), ("PC0", 15), ("PC1", 16), ("PC2", 17), ("PC3", 18))]
OLD_EDGE_BOX = (128.5, 90.0, 162.5, 113.0)

# The old asset: sha256 of each file's content with CRLF read as LF.
OLD_ASSET_SHA256 = {
    "834f2d160237b72d6662caacda31443349b95e638bbb25935f59fdae1f3d87b1",  # the symbol library
    "6001531dc39cae8fe08a5cc9dadb22a3e456eb6086bf181ac4e4939ebf975f53",  # the footprint
    "5fd45d69e7a77ddf97f7bec2cb8935a4d69199f83fef119d4ab4416f68e6eba3",  # the STEP model
    "bf6d4166caafb3665f32eb13bd54d077d426a193aa32625eba2e656ec7304c8f",  # the VRML model
}
# Names the old asset used. Not a bare LCSC id: that stays (BOM, schematic property) as the
# purchasing identifier of the part.
OLD_ASSET_STRINGS = ["C456012:", "C456012/", "C456012.", "TYPE-C-SMD_TYPE-C-6P_1", "TYPE-C6P", "6P-L8.9-W6.8",
                     "easyeda2kicad:", "uPesy"]
# The manufacturer's datasheet PDFs are third-party material kept for reading only and left out of the
# public export (publish-prep ticket 03); their file names carry the part's purchasing id.
DATASHEET_DIRS = ("evals/rung1/datasheets", "tests/fixtures/datasheets")
# Hidden directories (.git, process records, virtual environments, caches) and bytecode are not scanned.


def _load_asset_script():
    spec = importlib.util.spec_from_file_location("build_usbc_asset", RUNG1 / "build_usbc_asset.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------------- raw readers

def _nums(node) -> list[float]:
    return [float(v) for v in node.get_atoms()]


def _pad_rows(footprint_node) -> list[tuple]:
    """(number, type, shape, x, y, w, h, layers, drill, angle, net) of each raw (pad ...)."""
    rows = []
    for pad in footprint_node.find_all("pad"):
        atoms = pad.get_atoms()
        at = _nums(pad.find_child("at"))
        size = _nums(pad.find_child("size"))
        layers = tuple(sorted(str(v) for v in pad.find_child("layers").get_atoms()))
        drill = pad.find_child("drill")
        net = pad.find_child("net")
        rows.append((str(atoms[0]), str(atoms[1]), str(atoms[2]), at[0], at[1], size[0], size[1], layers,
                     None if drill is None else tuple(float(v) for v in drill.get_atoms()[1:]),
                     at[2] if len(at) > 2 else 0.0,
                     None if net is None else str(net.get_atoms()[-1])))
    return rows


def _reference(footprint_node) -> str | None:
    for prop in footprint_node.find_all("property"):
        if prop.get_string(0) == "Reference":
            return prop.get_string(1)
    for text in footprint_node.find_all("fp_text"):
        atoms = text.get_atoms()
        if str(atoms[0]) == "reference":
            return str(atoms[1])
    return None


def _board_footprint(board: Path, ref: str):
    root = write_layer.load(board)
    return next(fp for fp in root.find_all("footprint") if _reference(fp) == ref)


def _compare(old_rows, new_rows, tol=TOLERANCE_MM) -> list[str]:
    """Differences between two pad tables (the golden rows have 9 fields, the read rows 9+)."""
    def key(r):
        return (r[0], round(r[3], 3), round(r[4], 3))

    old, new = sorted(old_rows, key=key), sorted(new_rows, key=key)
    if len(old) != len(new):
        return [f"{len(old)} pads against {len(new)}"]
    out = []
    for o, n in zip(old, new, strict=True):
        who = f"pad {o[0]} at ({o[3]}, {o[4]})"
        if o[:3] != n[:3]:
            out.append(f"{who}: number/type/shape {o[:3]} against {n[:3]}")
        for label, a, b in (("x", o[3], n[3]), ("y", o[4], n[4]), ("width", o[5], n[5]), ("height", o[6], n[6])):
            if abs(a - b) > tol:
                out.append(f"{who}: {label} {a} against {b}")
        if tuple(sorted(o[7])) != tuple(sorted(n[7])):
            out.append(f"{who}: layers {o[7]} against {n[7]}")
        if (o[8] is None) != (n[8] is None) or (o[8] and any(abs(a - b) > tol for a, b in zip(o[8], n[8], strict=True))):
            out.append(f"{who}: drill {o[8]} against {n[8]}")
    return out


# --------------------------------------------------------------------------------- the pad table

def test_the_new_footprint_has_the_old_pad_table_within_0_001_mm():
    root = write_layer.load(ASSET_FOOTPRINT)
    assert root.name == "footprint" and root.get_string(0) == FOOTPRINT_NAME
    rows = _pad_rows(root)
    assert len(rows) == 10
    assert _compare(OLD_PADS, rows) == []
    assert all(r[9] == 0.0 for r in rows)  # no pad angle: nothing board-absolute to go stale


@pytest.mark.parametrize("change", ["x", "y", "width", "height", "drill", "layers", "number", "shape", "drop"])
def test_the_pad_comparison_fails_on_each_kind_of_difference(change):
    root = write_layer.load(ASSET_FOOTPRINT)
    rows = [list(r) for r in _pad_rows(root)]
    rows.sort(key=lambda r: (r[0], r[3], r[4]))
    row = next(r for r in rows if r[0] == "A5")
    tab = next(r for r in rows if r[0] == "7")
    if change == "x":
        row[3] += 0.0015
    elif change == "y":
        row[4] -= 0.0015
    elif change == "width":
        row[5] += 0.002
    elif change == "height":
        row[6] -= 0.1
    elif change == "drill":
        tab[8] = (tab[8][0], tab[8][1] + 0.1)
    elif change == "layers":
        row[7] = ("F.Cu", "F.Mask")
    elif change == "number":
        row[0] = "A6"
    elif change == "shape":
        row[2] = "roundrect"
    elif change == "drop":
        rows.remove(row)
    assert _compare(OLD_PADS, [tuple(r) for r in rows]) != []


def test_a_change_inside_the_tolerance_is_not_a_difference():
    rows = [list(r) for r in _pad_rows(write_layer.load(ASSET_FOOTPRINT))]
    rows[0][3] += 0.0009
    assert _compare(OLD_PADS, [tuple(r) for r in rows]) == []


def test_every_committed_board_stage_carries_the_old_pad_table_in_j1():
    boards = [RUNG1 / "layout_run" / s / PCB for s in STAGES]
    for board in boards:
        j1 = _board_footprint(board, "J1")
        assert j1.get_string(0) == f"{LIB}:{FOOTPRINT_NAME}", board
        rows = _pad_rows(j1)
        assert _compare(OLD_PADS, rows) == [], board
        assert all(r[9] == 0.0 for r in rows)  # board-absolute angle = footprint angle (0) + pad angle (0)
        fp_at = _nums(j1.find_child("at"))
        assert len(fp_at) == 2 or fp_at[2] == 0.0  # J1 is not rotated
        for r in rows:
            assert r[10] == J1_NETS[r[0]], (board, r[0])


# --------------------------------------------------------------------------------- the board

def test_the_poured_stage_keeps_every_footprint_position_net_outline_and_rule_of_the_old_build():
    board = RUNG1 / "layout_run" / "07_poured" / PCB
    root = write_layer.load(board)
    positions = {}
    for fp in root.find_all("footprint"):
        at = _nums(fp.find_child("at"))
        # Build ticket 114 moved C3 (and turned it by 180 degrees); every other footprint is unrotated.
        assert len(at) == 2 or at[2] == (180.0 if _reference(fp) == "C3" else 0.0)
        positions[_reference(fp)] = (round(at[0], 3), round(at[1], 3))
    assert positions.pop("C3") == (149.293, 108.239)  # see tests/test_rung1_decoupling.py
    assert positions == {ref: xy for ref, xy in OLD_POSITIONS.items() if ref != "C3"}
    assert sorted(str(n.get_atoms()[-1]) for n in root.children if n.name == "net") == sorted(OLD_NETS)
    xs, ys = [], []
    for shape in root.find_all("gr_line"):
        if str(shape.find_child("layer").get_atoms()[0]) == "Edge.Cuts":
            for end in ("start", "end"):
                x, y = _nums(shape.find_child(end))
                xs.append(x)
                ys.append(y)
    assert (min(xs), min(ys), max(xs), max(ys)) == OLD_EDGE_BOX
    zones = root.find_all("zone")
    assert len(zones) == 5  # four mounting-hole keepouts and the GND pour


def test_the_poured_stage_keeps_the_rules_of_the_old_build():
    pro = json.loads((RUNG1 / "layout_run" / "07_poured" / f"{NAME}.kicad_pro").read_text(encoding="utf-8"))
    rules = pro["board"]["design_settings"]["rules"]
    assert (rules["min_clearance"], rules["min_track_width"], rules["min_via_diameter"],
            rules["min_through_hole_diameter"]) == (0.127, 0.127, 0.45, 0.2)
    classes = {c["name"]: c for c in pro["net_settings"]["classes"]}
    assert {k: classes["Default"][k] for k in ("clearance", "track_width", "via_diameter", "via_drill")} == {
        "clearance": 0.127, "track_width": 0.127, "via_diameter": 0.45, "via_drill": 0.2}
    assert {k: classes["Power"][k] for k in ("clearance", "track_width", "via_diameter", "via_drill")} == {
        "clearance": 0.2, "track_width": 0.4, "via_diameter": 0.6, "via_drill": 0.3}
    assert [(p["netclass"], p["pattern"]) for p in pro["net_settings"]["netclass_patterns"]] == [
        ("Power", "VBUS"), ("Power", "/VBUS_F"), ("Power", "+3V3")]


# --------------------------------------------------------------------------------- the symbol

def test_the_symbol_pins_are_typed_and_none_is_unspecified():
    root = write_layer.load(ASSET_SYMBOL)
    pins = {}
    for pin in root.find_all("pin"):
        number = str(pin.find_child("number").get_atoms()[0])
        pins[number] = (str(pin.find_child("name").get_atoms()[0]), str(pin.get_atoms()[0]))
    assert pins == {
        "A12": ("GND", "passive"), "B12": ("GND", "passive"), "A9": ("VBUS", "passive"),
        "B9": ("VBUS", "passive"), "A5": ("CC1", "bidirectional"), "B5": ("CC2", "bidirectional"),
        "7": ("SHIELD", "passive"),
    }
    assert "unspecified" not in ASSET_SYMBOL.read_text(encoding="utf-8")


def test_erc_of_the_rung1_project_has_no_violation(tmp_path):
    project = tmp_path / "project"
    shutil.copytree(RUNG1 / "project", project)
    report = kicad_cli.run_erc(project / f"{NAME}.kicad_sch")
    violations = [v for sheet in report.get("sheets", []) for v in sheet.get("violations", [])]
    assert violations == []
    committed = json.loads((RUNG1 / "erc_report.json").read_text(encoding="utf-8"))
    assert committed["violation_count"] == 0 and committed["violations"] == []


# --------------------------------------------------------------------------------- the library files

def _lf(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def test_the_committed_asset_is_what_the_builder_writes():
    mod = _load_asset_script()
    assert _lf(ASSET_FOOTPRINT) == (write_layer.serialise(mod.footprint_tree()) + "\n").encode("utf-8")
    assert _lf(ASSET_SYMBOL) == (write_layer.serialise(mod.symbol_tree()) + "\n").encode("utf-8")


def test_every_stage_and_the_project_hold_the_own_library_and_no_model():
    for base in [RUNG1 / "project", RUNG1 / "layout_run" / "src", *(RUNG1 / "layout_run" / s for s in STAGES)]:
        lib = base / "lib" / LIB
        assert _lf(lib / f"{LIB}.kicad_sym") == _lf(ASSET_SYMBOL), base
        assert _lf(lib / f"{LIB}.pretty" / f"{FOOTPRINT_NAME}.kicad_mod") == _lf(ASSET_FOOTPRINT), base
        assert sorted(p.name for p in (base / "lib").iterdir()) == [LIB], base
        assert sorted(p.name for p in lib.iterdir()) == [f"{LIB}.kicad_sym", f"{LIB}.pretty"], base
    assert "(model" not in ASSET_FOOTPRINT.read_text(encoding="utf-8")
    assert not [p for p in RUNG1.rglob("*") if p.suffix.lower() in (".step", ".stp", ".wrl")]


def test_the_footprint_names_its_source_by_manufacturer_and_part_number():
    text = ASSET_FOOTPRINT.read_text(encoding="utf-8")
    assert "SHOU HAN" in text and "TYPE-C 6P" in text and "recommended P.C.B. layout" in text
    assert "no third-party library data" in text


# --------------------------------------------------------------------------------- the self-check

def test_the_asset_writers_self_check_fires_on_a_corrupted_write(tmp_path):
    mod = _load_asset_script()
    target = tmp_path / "fp.kicad_mod"
    write_layer.save(mod.footprint_tree(), target, verify=mod.verify_footprint)
    good = target.read_bytes()
    tree = mod.footprint_tree()
    pad = next(p for p in tree.find_all("pad") if p.get_string(0) == "A5")
    write_layer.set_number(pad.find_child("size"), 0, 0.9)  # one pad 0.2 mm too wide
    with pytest.raises(mod.AssetMismatch):
        write_layer.save(tree, target, verify=mod.verify_footprint)
    assert target.read_bytes() == good  # the original is untouched
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fp.kicad_mod"]  # and no temporary file is left


def test_the_symbol_self_check_fires_on_a_wrong_pin_type(tmp_path):
    mod = _load_asset_script()
    target = tmp_path / "sym.kicad_sym"
    write_layer.save(mod.symbol_tree(), target, verify=mod.verify_symbol)
    good = target.read_bytes()
    tree = mod.symbol_tree()
    pin = next(p for p in tree.find_all("pin") if p.find_child("number").get_atoms()[0] == "A5")
    write_layer.set_symbol(pin, 0, "unspecified")
    with pytest.raises(mod.AssetMismatch):
        write_layer.save(tree, target, verify=mod.verify_symbol)
    assert target.read_bytes() == good


# --------------------------------------------------------------------------------- the old asset stays out

def _walk_repo():
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if any(part.startswith(".") or part in ("__pycache__", "node_modules") for part in rel.parts):
            continue
        yield path


def _name_hit(path: Path) -> bool:
    return "c456012" in path.name.lower()


def _content_hits(data: bytes) -> list[str]:
    """Why a file's bytes are the old asset (or name its libraries): its content hash, or a library name."""
    data = data.replace(b"\r\n", b"\n")
    hits = []
    if hashlib.sha256(data).hexdigest() in OLD_ASSET_SHA256:
        hits.append("old asset content")
    text = data.decode("utf-8", errors="ignore")
    hits += [f"names {needle!r}" for needle in OLD_ASSET_STRINGS if needle in text]
    return hits


def test_no_file_or_directory_named_for_the_old_asset_exists_outside_scratch():
    named = [p.relative_to(ROOT).as_posix() for p in _walk_repo() if _name_hit(p)]
    assert [n for n in named if not n.startswith(DATASHEET_DIRS)] == []


def test_no_file_under_evals_has_the_old_assets_content_or_library_names():
    seen = 0
    for path in (ROOT / "evals").rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        seen += 1
        assert _content_hits(path.read_bytes()) == [], path
    assert seen > 50  # the scan really walked the rung inputs


def test_the_old_asset_scan_catches_a_reappearing_copy():
    # The two scans above are not vacuous: each predicate fires on a file of the old shape.
    assert _name_hit(Path("evals/rung1/assets/C456012/C456012.kicad_sym"))
    assert _name_hit(Path("lib/c456012"))
    assert not _name_hit(Path("evals/rung1/assets/Rung1_USBC"))
    assert _content_hits(b'(footprint "C456012:TYPE-C-SMD_TYPE-C-6P_1")')
    assert _content_hits(b"(module easyeda2kicad:X)")
    assert _content_hits(b'(model "x/C456012.3dshapes/a.wrl")')
    # a renamed byte-identical copy is caught by its hash (the real files are gone, so test the rule
    # with one of the pinned hashes by monkeypatching nothing: the rule is "hash in the set")
    assert not _content_hits(b"unrelated")
    assert "834f2d160237b72d6662caacda31443349b95e638bbb25935f59fdae1f3d87b1" in OLD_ASSET_SHA256
