"""confirm_courtyard_rewrite: registration for the write-guard test (issue 147 contract, issue 250)."""

TOOL = "confirm_courtyard_rewrite"
WRITES = True


def scenario(h):
    def make(tmp_path):
        from boardproof import courtyard_rewrite
        from tests.test_courtyard_rewrite import make_project

        board = make_project(tmp_path)
        found = courtyard_rewrite.propose_courtyard_rewrite(board, ["C_1206"], output_dir=tmp_path / "out")
        assert found["status"] == "proposed", found
        return h.Scenario(board, lambda: courtyard_rewrite.confirm_courtyard_rewrite(board, found["revision_token"]))

    return make
