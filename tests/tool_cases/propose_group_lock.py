"""propose_group_lock: registration for the write-guard test (issue 147 contract, issue 254)."""

TOOL = "propose_group_lock"
WRITES = False
NOT_WRITING_REASON = "dry run: reads the board and writes nothing (the revision is built only by the confirm)"
