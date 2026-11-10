"""set_title_block: registration for the write-guard test (issue 147 contract, issue 97 tool)."""

TOOL = "set_title_block"
WRITES = True


def scenario(h):
    """The one-resistor sheet; the tool sets a title and a two-line comment on it."""
    def make(tmp_path):
        project = h._schematic_project(tmp_path)
        tools = h._tools(h.schematic_toolset)
        return h.Scenario(project["sch"], lambda: tools[TOOL](str(project["sch"]), [{"title": "T", "comments": {"1": "a\nb"}}]))
    return make

WORDING_ALLOWED = {
    "or the call is refused with write_not_verified (nothing written)": "the name of an error code on a refusal, not a claim",
}
