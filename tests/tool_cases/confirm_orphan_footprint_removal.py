"""confirm_orphan_footprint_removal: registration for the write-guard test (issue 147 contract, ticket 227)."""
from tests._fixture_copy import copy_fixture

TOOL = "confirm_orphan_footprint_removal"
WRITES = True


def scenario(h):
    def make(tmp_path):

        from boardproof import orphan_footprints, schematic_import, write_layer
        from tests.test_orphan_footprints import NAME, RUNG1

        proj = tmp_path / "proj"
        copy_fixture(RUNG1 / "project", proj)
        first = tmp_path / "first"
        proposal = schematic_import.propose_schematic_import(proj / f"{NAME}.kicad_pcb", output_dir=first)
        assert schematic_import.confirm_schematic_import(proj / f"{NAME}.kicad_pcb", proposal["revision_token"])["status"] == "imported"
        board = first / f"{NAME}.kicad_pcb"
        root = write_layer.load(board)
        fp = next(f for f in root.children if f.name == "footprint")
        prop = next(c for c in fp.children if c.name == "property" and str(c.values[0]) == "Reference")
        write_layer.set_string(prop, 1, "ZZ1")  # no symbol ZZ1: an orphan
        write_layer.save(root, board)
        found = orphan_footprints.propose_orphan_footprint_removal(board, output_dir=tmp_path / "out")
        assert found["status"] == "proposed", found
        return h.Scenario(board, lambda: orphan_footprints.confirm_orphan_footprint_removal(board, found["revision_token"]))

    return make
