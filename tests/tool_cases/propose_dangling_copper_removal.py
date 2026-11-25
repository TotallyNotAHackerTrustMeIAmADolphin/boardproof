"""propose_dangling_copper_removal: registration for the write-guard test (issue 147 contract, issue 246)."""

TOOL = "propose_dangling_copper_removal"
WRITES = False
NOT_WRITING_REASON = "dry run: reads the board and one kicad-cli DRC report and writes nothing (the revision is built only by the confirm)"
