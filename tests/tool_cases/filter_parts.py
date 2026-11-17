"""filter_parts: registration for the write-guard test (issue 147 contract, issue 199 tool)."""

TOOL = "filter_parts"
WRITES = False
NOT_WRITING_REASON = "read-only: opens the local parts index read-only and immutable, writes nothing"
