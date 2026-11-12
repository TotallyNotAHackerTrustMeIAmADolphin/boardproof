"""delete_text: registration for the write-guard test (issue 147 contract, issue 198 tool)."""

TOOL = "delete_text"
WRITES = True


def scenario(h):
    """The one-resistor sheet with a note on it (add_text); the tool then works on the note."""
    def make(tmp_path):
        project = h._schematic_project(tmp_path)
        tools = h._tools(h.schematic_toolset)
        assert tools["add_text"](str(project["sch"]), [{"x": 25.4, "y": 30.48, "text": "NOTE"}])["status"] == "ok"
        return h.Scenario(project["sch"], lambda: tools[TOOL](str(project["sch"]), [{"pattern": "NOTE"}]))
    return make


WORDING_ALLOWED = {
    "(connectivity_changed otherwise; there is no override)": "the name of an error code on a refusal, not a claim",
    "its ERC census must be the same too (write_not_verified, check erc_unchanged)": "the name of an error code on a refusal, not a claim",
}
