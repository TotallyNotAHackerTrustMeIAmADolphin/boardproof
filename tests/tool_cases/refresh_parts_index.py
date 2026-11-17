"""refresh_parts_index: registration for the write-guard test (issue 147 contract, issue 200 tool)."""

TOOL = "refresh_parts_index"
WRITES = False
NOT_WRITING_REASON = (
    "writes no KiCad design file or library: downloads the jlcparts cache and replaces the local parts index "
    "(a SQLite file in a user data directory, never a project or repository) after checking it"
)
