"""rename_net: registration for the write-guard test (issue 147 contract, issue 96 tool)."""

TOOL = "rename_net"
WRITES = True
WORDING_ALLOWED = {
    "refused as write_not_verified by the netlist comparison": "the name of an error code on a refusal, not a claim",
    "(write_not_verified, the sheet unchanged, differences listed)": "the name of an error code on a refusal, not a claim",
}


def scenario(h):
    def make(tmp_path):
        from tests import rename_fixture

        root = rename_fixture.build(tmp_path)
        tools = h._tools(h.schematic_toolset)
        return h.Scenario(root, lambda: tools[TOOL](str(root), [{"old": "/SDA", "new": "DATA"}]))

    return make
