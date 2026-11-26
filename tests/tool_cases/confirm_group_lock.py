"""confirm_group_lock: registration for the write-guard test (issue 147 contract, issue 254)."""

TOOL = "confirm_group_lock"
WRITES = True


def scenario(h):
    def make(tmp_path):
        from boardproof import group_lock
        from tests.test_group_lock import board as make_board

        board = make_board.__wrapped__(tmp_path)
        found = group_lock.propose_group_lock(board, ["pair"], True, tmp_path / "out")
        assert found["status"] == "proposed", found
        return h.Scenario(board, lambda: group_lock.confirm_group_lock(board, found["revision_token"]))

    return make
