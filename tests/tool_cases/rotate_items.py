"""rotate_items: registration for the write-guard test (issue 147 contract, issue 93 tool)."""

TOOL = "rotate_items"
WRITES = True


def scenario(h):
    return h._editing(TOOL, lambda p: ([{"references": ["R1"], "by": 90}],))


WORDING_ALLOWED = {
    "(write_not_verified, the sheet unchanged)": "the name of an error code on a refusal, not a claim",
}
