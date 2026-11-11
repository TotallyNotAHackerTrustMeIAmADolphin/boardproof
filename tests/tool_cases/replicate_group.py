"""replicate_group: registration for the write-guard test (issue 147 contract, issue 94 tool)."""

TOOL = "replicate_group"
WRITES = True


def scenario(h):
    # the one-resistor sheet of the guard fixture: R1 at (50.8, 50.8), its pins at (50.8, 46.99) and (50.8, 54.61)
    region = {"x1": 45.72, "y1": 43.18, "x2": 55.88, "y2": 58.42}
    return h._editing(TOOL, lambda p: (region, 1, {"dx": 25.4, "dy": 0}))


WORDING_ALLOWED = {
    "write_not_verified, check netlist_follows_plan": "the names of an error code and its check on a refusal, not a claim",
}
