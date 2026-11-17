"""list_part_filters: registration for the write-guard test (issue 147 contract, issue 199 tool)."""

TOOL = "list_part_filters"
WRITES = False
NOT_WRITING_REASON = "read-only: opens the local parts index read-only and immutable, writes nothing"
