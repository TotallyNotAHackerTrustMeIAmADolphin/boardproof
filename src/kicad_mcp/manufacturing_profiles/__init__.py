"""Local manufacturer fab-capability profiles + a board's own design rules.

Two halves of archived backlog P1-21 / P1-18, deliberately kept apart because they
are different kinds of claim:

* :mod:`~kicad_mcp.manufacturing_profiles.jlcpcb` / ``registry`` — what a fab
  can physically build, as a **static local snapshot** of the fab's own
  published capabilities page. Static by explicit project requirement: these
  are published, largely-static specs, not live pricing or stock, so they are
  never fetched per call. Each profile records the exact URL and the date it
  was read, and any value the page does not state stays ``None`` rather than
  becoming a plausible guess.
* :mod:`~kicad_mcp.manufacturing_profiles.board_rules` — what *this design*
  actually declares, read from its own ``.kicad_pro`` netclasses/constraints
  and its ``.kicad_dru`` custom rules.

:mod:`~kicad_mcp.manufacturing_profiles.compare` holds the precedence rule
between them: **the board's own rules are authoritative for what KiCad
checks; the manufacturer profile is a capability floor**. A design can be
DRC-clean against its own rules and still be unmanufacturable at a given fab.
The two are reported side by side, never merged, and every number carries the
source it came from.

Relationship to ``kicad_tools.manufacturers`` (finding, 2026-09-04): upstream
already ships JLCPCB/PCBWay/OSHPark/Seeed profiles, so this package is not
filling an empty hole — it exists because upstream's JLCPCB numbers disagree
materially with what JLCPCB publishes today (including one value that is
*looser* than the fab's real floor) and carry no per-field provenance. See
``jlcpcb.py``'s module docstring for the full field-by-field table, and
``registry.to_kicad_tools_design_rules`` for the bridge back to their
dataclass so their ``.kicad_dru``/project generators can be driven from these
verified numbers.
"""

from .board_rules import (
    BUILTIN_DEFAULTS,
    find_rule_files,
    read_board_design_rules,
    read_custom_rules,
    read_project_rules,
    resolve_effective_rules,
)
from .compare import VERDICTS, compare_to_profile, resolve_check_rules
from .jlcpcb import JLCPCB_PROFILE
from .models import (
    CANONICAL_FROM_FAB_FIELD,
    FabDesignRules,
    ManufacturerProfile,
    UnknownManufacturerError,
    UnsupportedConfigurationError,
)
from .registry import (
    get_design_rules,
    get_profile,
    list_accepted_names,
    list_manufacturer_ids,
    list_profiles,
    to_kicad_tools_design_rules,
)

__all__ = [
    "BUILTIN_DEFAULTS",
    "CANONICAL_FROM_FAB_FIELD",
    "JLCPCB_PROFILE",
    "VERDICTS",
    "FabDesignRules",
    "ManufacturerProfile",
    "UnknownManufacturerError",
    "UnsupportedConfigurationError",
    "compare_to_profile",
    "find_rule_files",
    "get_design_rules",
    "get_profile",
    "list_accepted_names",
    "list_manufacturer_ids",
    "list_profiles",
    "read_board_design_rules",
    "read_custom_rules",
    "read_project_rules",
    "resolve_check_rules",
    "resolve_effective_rules",
    "to_kicad_tools_design_rules",
]
