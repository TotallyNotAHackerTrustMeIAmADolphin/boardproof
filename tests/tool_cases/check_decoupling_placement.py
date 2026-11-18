"""check_decoupling_placement: registration for the write-guard test (issue 147 contract, issue 26 tool)."""

TOOL = "check_decoupling_placement"
WRITES = False
NOT_WRITING_REASON = (
    "read-only: it loads the board with write_layer.load and reads raw pad tokens; it saves nothing and opens no "
    "scratch copy (tests/test_decoupling_check.py::test_the_check_never_writes)"
)
