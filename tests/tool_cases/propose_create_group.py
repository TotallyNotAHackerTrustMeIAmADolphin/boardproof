"""propose_create_group: registration for the write-guard test (issue 147 contract, issue 255)."""

TOOL = "propose_create_group"
WRITES = False
NOT_WRITING_REASON = "dry run: reads the board and writes nothing (the revision is built only by the confirm)"
