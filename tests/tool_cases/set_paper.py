"""set_paper: registration for the write-guard test (issue 147 contract, issue 197 tool)."""

TOOL = "set_paper"
WRITES = True


def scenario(h):
    """The one-resistor sheet; the tool turns its paper to A3."""
    def make(tmp_path):
        project = h._schematic_project(tmp_path)
        tools = h._tools(h.schematic_toolset)
        return h.Scenario(project["sch"], lambda: tools[TOOL](str(project["sch"]), [{"size": "A3"}]))
    return make

WORDING_ALLOWED = {
    "or the call is refused with write_not_verified (nothing written)": "the name of an error code on a refusal, not a claim",
}
