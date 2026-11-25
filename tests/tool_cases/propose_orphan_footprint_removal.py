"""propose_orphan_footprint_removal: registration for the write-guard test (issue 147 contract, ticket 227)."""

TOOL = "propose_orphan_footprint_removal"
WRITES = False
NOT_WRITING_REASON = "dry run: reads the board and the schematic netlist and writes nothing (the revision is built only by the confirm)"
