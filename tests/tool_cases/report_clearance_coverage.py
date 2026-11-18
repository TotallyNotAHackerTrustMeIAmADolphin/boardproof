"""report_clearance_coverage: registration for the write-guard test (issue 147 contract, issue 131 tool)."""

TOOL = "report_clearance_coverage"
WRITES = False
NOT_WRITING_REASON = (
    "read-only: it reads the board, its project and its rules file; the load probe saves only into its own scratch copy, "
    "removed afterwards (tests/test_clearance_coverage.py::test_the_report_never_writes)"
)
