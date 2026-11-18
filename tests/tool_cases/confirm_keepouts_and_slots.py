"""confirm_keepouts_and_slots: registration for the write-guard test (issue 147 contract, issue 132 tool)."""

TOOL = "confirm_keepouts_and_slots"
WRITES = True


def scenario(h):
    def make(tmp_path):
        import shutil

        from boardproof import isolation

        shutil.copytree(h.t_outline.GOLDEN_DIR, tmp_path / "proj")
        board = tmp_path / "proj" / h.t_outline.GOLDEN_NAME
        slot = {"x1": 140, "y1": 112, "x2": 156, "y2": 112, "width_mm": 1.5, "ends": "round"}
        proposal = isolation.propose_keepouts_and_slots(board, slots=[slot], output_dir=tmp_path / "out")
        assert proposal["status"] == "proposed", proposal
        return h.Scenario(board, lambda: isolation.confirm_keepouts_and_slots(
            board, proposal["revision_token"], drc=lambda p, **_k: {"violations": [], "unconnected_items": []}))

    return make

WORDING_ALLOWED = {"verified by a fresh reload": "the tool's own write check, not a board verdict"}
