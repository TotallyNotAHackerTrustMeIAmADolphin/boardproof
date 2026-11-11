"""refresh_lib_symbols: registration for the write-guard test (issue 147 contract, issue 115 tool)."""

TOOL = "refresh_lib_symbols"
WRITES = True


def scenario(h):
    return h._editing(TOOL, lambda p: ())


WORDING_ALLOWED = {
    "(write_not_verified, the sheet unchanged)": "the name of an error code on a refusal, not a claim",
}
