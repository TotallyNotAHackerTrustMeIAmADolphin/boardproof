"""confirm_create_group: registration for the write-guard test (issue 147 contract, issue 255)."""

TOOL = "confirm_create_group"
WRITES = True


def scenario(h):
    def make(tmp_path):
        from boardproof import group_edit
        from tests.test_group_edit import board as make_board

        board = make_board.__wrapped__(tmp_path)
        found = group_edit.propose_create_group(board, "new", ["J1"], tmp_path / "out")
        assert found["status"] == "proposed", found
        return h.Scenario(board, lambda: group_edit.confirm_create_group(board, found["revision_token"]))

    return make
