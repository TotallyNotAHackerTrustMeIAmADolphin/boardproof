"""rename_ref: registration for the write-guard test (issue 147 contract, issue 96 tool)."""

TOOL = "rename_ref"
WRITES = True
WORDING_ALLOWED = {
    "(write_not_verified, the sheet unchanged)": "the name of an error code on a refusal, not a claim",
}


def scenario(h):
    def make(tmp_path):
        from tests import rename_fixture

        root = rename_fixture.build(tmp_path)
        tools = h._tools(h.schematic_toolset)
        return h.Scenario(root, lambda: tools[TOOL](str(root), [{"old": "R1", "new": "R7"}]))

    return make
