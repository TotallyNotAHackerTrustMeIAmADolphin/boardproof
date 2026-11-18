"""place_footprints_at_edge: registration for the write-guard test (issue 147 contract, issue 133 tool)."""

TOOL = "place_footprints_at_edge"
WRITES = True


def scenario(h):
    def make(tmp_path):
        pcb = h._led(tmp_path)
        tools = h._tools(h.placement_toolset)
        spec = [{"reference": "R1", "edge": "bottom", "offset_mm": 1.0, "along_mm": 12.0}]
        return h.Scenario(pcb, lambda: tools[TOOL](str(pcb), spec))

    return make
