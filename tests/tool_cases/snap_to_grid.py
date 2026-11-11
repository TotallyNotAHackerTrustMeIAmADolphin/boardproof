"""snap_to_grid: registration for the write-guard test (issue 147 contract, issue 95 tool)."""

TOOL = "snap_to_grid"
WRITES = True


def scenario(h):
    return h._editing(TOOL, lambda p: ())


WORDING_ALLOWED = {
    "(write_not_verified, the sheet unchanged)": "the name of an error code on a refusal, not a claim",
}
