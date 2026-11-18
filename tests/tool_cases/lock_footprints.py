"""lock_footprints: registration for the write-guard test (issue 147 contract, issue 133 tool)."""

TOOL = "lock_footprints"
WRITES = True


def scenario(h):
    def make(tmp_path):
        pcb = h._led(tmp_path)
        tools = h._tools(h.placement_toolset)
        return h.Scenario(pcb, lambda: tools[TOOL](str(pcb), ["R1"]))

    return make
