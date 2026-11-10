"""add_text: registration for the write-guard test (issue 147 contract, issue 145 tool)."""

TOOL = "add_text"
WRITES = True


def scenario(h):
    return h._editing(TOOL, lambda p: ([{"x": 25.4, "y": 30.48, "text": "NOTE"}],))


WORDING_ALLOWED = {
    "(write_not_verified, the sheet unchanged)": "the name of an error code on a refusal, not a claim",
}
