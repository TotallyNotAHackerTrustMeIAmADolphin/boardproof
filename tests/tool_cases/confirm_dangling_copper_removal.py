"""confirm_dangling_copper_removal: registration for the write-guard test (issue 147 contract, issue 246)."""

TOOL = "confirm_dangling_copper_removal"
WRITES = True


def scenario(h):
    def make(tmp_path):
        from boardproof import dangling_copper
        from tests.test_dangling_copper import _add, _seg
        from tests.test_orphan_footprints import make_board

        board = make_board(tmp_path)
        _add(board, _seg(1, (1, 1), (2, 1)))
        found = dangling_copper.propose_dangling_copper_removal(board, output_dir=tmp_path / "out")
        assert found["status"] == "proposed", found
        return h.Scenario(board, lambda: dangling_copper.confirm_dangling_copper_removal(board, found["revision_token"]))

    return make
