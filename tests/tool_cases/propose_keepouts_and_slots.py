"""propose_keepouts_and_slots: registration for the write-guard test (issue 147 contract, issue 132 tool)."""

TOOL = "propose_keepouts_and_slots"
WRITES = False
NOT_WRITING_REASON = "dry run: reads the board and writes nothing (the revision is built only by the confirm)"
