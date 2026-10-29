"""Tool and toolset descriptions never claim a board is complete (build ticket 12).

Descriptions are text an LLM reads at runtime (playbook P7: critical class). The gate-field
sweep in test_gate_field.py covers tool *outputs*; this file covers the descriptions, through
the real server's listing (``build_server`` + MCP ``Client``) with every toolset loaded.

Rules, each a reviewed exception list rather than a judgement call:

1. No description other than ``verify_board``'s calls a check "authoritative". Only the
   negative forms pass: "non-authoritative" and the output field "authoritative: false".
2. Claim words appear only in a sentence that points to the gate, in the gate's own
   description, or in a phrase listed in ``_CLAIM_WORD_ALLOWED``. Claim words: routed, done,
   complete, finished, clean, definitive, verified, ready (not "unrouted", "incomplete",
   "unverified", "already"; "JLCPCB-ready" and "DRC-clean" count), "fully connected",
   "real DRC", "passes" and "is/are connected" (build ticket 60, ``PASSES_OR_CONNECTED``).
   A "ready symbol/footprint/asset" in sourcing is not a board claim and passes. An
   allow-listed phrase is cut out of its sentence before the search, so it never shields a
   second claim word in the same sentence; an entry that no longer matches fails.
   A sentence points to the gate only when it names ``verify_board`` together with ``PASS``,
   ``only`` or ``verdict``: a mere mention ("so verify_board can be skipped") does not count.
3. Every tool that checks, exports, renders or routes, and every toolset that holds such
   tools, names ``verify_board``.

Every toolset must actually load (``loaded`` or ``already_loaded``), so a toolset that fails
to build cannot drop its descriptions out of the sweep.
"""

from __future__ import annotations

import json
import re

import pytest
from mcp.client.client import Client

from kicad_mcp.server import build_server

_GATE = "verify_board"

_AUTHORITATIVE = re.compile(r"(?<!non-)\bauthoritative\b(?!:\s*false)", re.IGNORECASE)
# Build ticket 60: "the board passes" and "every net is connected" (also "is now/all/fully/
# then connected"). test_output_gate_wording.py reuses it, through ``has_passes_or_connected``.
# (Classes spell A-Z only: the regexes are compiled with IGNORECASE, so a-z was dead, fix round 4.)
# Grammar, not allow-list entries; each exclusion is anchored to the one text it was reviewed
# for (fix round 1), so the same words anywhere else are still a claim:
# * the noun "passes", each only in the text it was reviewed for: "with more passes" and
#   "so more passes" (routing.py), "counted as passes" (rung_runner.py), and "max_passes";
# * "the result and passes it as `baseline`": pin_rule_baseline hands a value over;
# * "KiCad is connected, but no PCB|schematic document is open" at the very start of the
#   text, case-sensitive: ipc/connection.py's message about the IPC session, not a net;
# * the question "whether <one or two words> is/are connected" (``_QUESTION``, cut out
#   before the search): pour_step.py hands that question to kicad-cli. A question that is
#   answered right away (": yes", ", true", " - it is") is not cut: that is a claim.
PASSES_OR_CONNECTED = (
    r"(?<![A-Z_])(?<!with more )(?<!so more )(?<!counted as )"
    r"(?:(?<!the result and )|(?!passes it as `baseline`))passes(?![A-Z_])"
    r"|(?:(?<!^(?-i:KiCad) )|(?!(?-i:is connected, but no (?:PCB|schematic) document is open)))"
    r"\b(?:is|are)\s+(?:(?:now|all|fully|then)\s+)?connected\b"
)
_QUESTION = re.compile(r"\bwhether\s+(?!or\b)\w+(?:\s+(?!or\b)\w+)?\s+(?:is|are)\s+connected\b"
    r"(?!\s*[:;,\u2013\u2014-]+\s*(?:yes|true|it is|they are|so)\b)",
    re.IGNORECASE,
)
_PASSES_OR_CONNECTED = re.compile(PASSES_OR_CONNECTED, re.IGNORECASE)
_CLAIM_WORD = re.compile(
    r"(?<![A-Za-z])(?:routed|done|complete|finished|clean|definitive|verified)(?![A-Za-z])"
    r"|\bfully connected\b|\breal DRC\b"
    r"|(?<![A-Za-z])ready(?![A-Za-z])(?!\s+(?:KiCad\s+)?(?:symbol|footprint|asset))"
    r"|" + PASSES_OR_CONNECTED,
    re.IGNORECASE,
)


def has_claim(text: str) -> bool:
    """A claim word in ``text``, once the reviewed "whether ... connected" questions are cut out."""
    return bool(_CLAIM_WORD.search(_QUESTION.sub(" ", text)))


def has_passes_or_connected(text: str) -> bool:
    """The build-ticket-60 words alone, with the same question cut (the output sweep's rule 3)."""
    return bool(_PASSES_OR_CONNECTED.search(_QUESTION.sub(" ", text)))


_GATE_RULE = re.compile(r"\bPASS\b|\bonly\b|\bverdict\b")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n\s*\n")

# (tool or toolset name, exact phrase containing the claim word) -> why it is not a claim that
# the board is done. Reviewed one by one; a new entry needs the same review.
_WRITE_CHECK = "verified by a fresh reload"
_NOT_CLEAN = "is then NOT a clean baseline"
_CLAIM_WORD_ALLOWED = {
    ("confirm_pour_step", "It does NOT prove the board is connected or complete"): "negation",
    ("confirm_schematic_import", _WRITE_CHECK): "the tool's own write check, not a board verdict",
    ("confirm_board_rules", _WRITE_CHECK): "the tool's own write check, not a board verdict",
    ("confirm_board_outline", _WRITE_CHECK): "the tool's own write check, not a board verdict",
    ("confirm_silkscreen_minimums", _WRITE_CHECK): "the tool's own write check, not a board verdict",
    ("propose_input_preparation", _NOT_CLEAN): "negation",
    ("confirm_input_preparation", "the floor is then NOT a clean baseline"): "negation",
    ("pcb_run_drc", "an agent reading a clean result from it would reasonably believe"):
        "history of the P1-14 mislabel: a clean result was misread",
    ("pcb_run_drc", 'see exactly what "clean" was clean against'): "names which rules a run used",
    ("pcb_run_drc", "a board can be fully DRC-clean against its own rules and still be below"):
        "says DRC-clean is not enough",
    ("pcb_get_design_rules", '"clean according to whose numbers?"'): "says DRC-clean is relative",
    ("pcb_check_manufacturability", "a fully DRC-clean board is still unbuildable"):
        "says DRC-clean is not enough",
    ("list_manufacturer_profiles", "the date it was verified"): "a fab profile's source date",
    ("select_component", "a datasheet-verified leader"): "part selection, not the board",
    ("panelize_board", "it never passes an un-panelized board off as a panel"): "negation",
}


def _points_to_the_gate(sentence: str) -> bool:
    return _GATE in sentence and bool(_GATE_RULE.search(sentence))

# Tools that check, export, render or route, and the toolsets holding them: each must name
# the gate. pcb_run_drc, pcb_check_placement and pcb_check_collisions are covered by
# test_gate_field.py as well.
_MUST_NAME_THE_GATE = {
    # diagnostics
    "render_board_diagnostic",
    "render_schematic_diagnostic",
    # manufacturing
    "export_manufacturing_package",
    "panelize_board",
    # routing
    "add_traces",
    "get_unrouted_nets",
    "propose_route_with_freerouting",
    "propose_route_with_retry",
    "confirm_route",
    # placement repair through a router
    "propose_placement_optimization",
    "confirm_placement_optimization",
    # ERC and schematic checks
    "sch_run_erc",
    "check_connectivity",
    # verification
    "pcb_run_drc",
    "pcb_check_placement",
    "pcb_check_collisions",
    "pcb_validate_proposed_placements",
    "pcb_check_manufacturability",
}
_TOOLSETS_MUST_NAME_THE_GATE = {"verification", "diagnostics", "manufacturing", "routing"}


async def _descriptions() -> dict[str, str]:
    """{name: description} for every tool (all toolsets loaded) and every toolset."""
    async with Client(build_server(), mode="auto") as client:
        listed = await client.call_tool("list_toolsets", {})
        toolsets = listed.structured_content["result"] if listed.structured_content is not None \
            else json.loads(listed.content[0].text)["result"]
        for toolset in toolsets:
            loaded = await client.call_tool("load_toolset", {"name": toolset["name"]})
            status = (loaded.structured_content or json.loads(loaded.content[0].text)).get("status")
            assert status in ("loaded", "already_loaded"), (toolset["name"], loaded)
        tools = (await client.list_tools()).tools
    out = {f"toolset:{t['name']}": t["description"] for t in toolsets}
    out.update({t.name: t.description or "" for t in tools})
    return out


def _bare(name: str) -> str:
    return name.removeprefix("toolset:")


@pytest.mark.asyncio
async def test_no_description_but_the_gates_calls_a_check_authoritative() -> None:
    offenders = {
        name: match.group(0)
        for name, text in (await _descriptions()).items()
        if name != _GATE and (match := _AUTHORITATIVE.search(text))
    }
    assert not offenders, offenders


def _claim_offenders(descriptions: dict[str, str]) -> list[str]:
    """Every sentence with a claim word that neither points to the gate nor sits inside an
    allow-listed phrase. An allow-listed phrase is cut out of the sentence before the search,
    so it never shields a second claim word elsewhere in the same sentence (build ticket 60)."""
    offenders: list[str] = []
    for name, text in descriptions.items():
        if name == _GATE:
            continue
        for sentence in (" ".join(s.split()) for s in _SENTENCE_END.split(text)):
            if _points_to_the_gate(sentence):
                continue
            rest = sentence
            for tool, phrase in _CLAIM_WORD_ALLOWED:
                if _bare(name) == tool:
                    rest = rest.replace(phrase, " ")
            if has_claim(rest):
                offenders.append(f"{name}: {sentence!r}")
    return offenders


@pytest.mark.asyncio
async def test_no_description_claims_a_board_is_complete_or_ready() -> None:
    offenders = _claim_offenders(await _descriptions())
    assert not offenders, "\n".join(offenders)


@pytest.mark.asyncio
async def test_every_allow_list_entry_still_matches_its_description() -> None:
    # A stale entry would quietly allow the phrase again if it came back.
    descriptions = {_bare(name): " ".join(text.split()) for name, text in (await _descriptions()).items()}
    stale = [entry for entry in _CLAIM_WORD_ALLOWED if entry[1] not in descriptions.get(entry[0], "")]
    assert not stale, stale


@pytest.mark.asyncio
async def test_the_sweep_fails_on_the_mutants() -> None:
    # Each mutant, appended to a real description, must make the sweep fail; also behind an
    # allow-listed phrase in the same sentence.
    descriptions = await _descriptions()
    for name, mutant in [
        ("get_unrouted_nets", "After this the board passes."),
        ("confirm_route", "Every net is connected after this."),
        ("confirm_route", "Afterwards every net of the board in KiCad is connected."),
        ("propose_route_with_retry", "The board is connected whether or not the zones are filled."),
        ("confirm_placement_optimization", "All nets are now connected."),
        ("confirm_pour_step", "It does NOT prove the board is connected or complete, but the board passes."),
        ("confirm_board_rules", "The rules are verified by a fresh reload and every net is connected."),
    ]:
        mutated = {**descriptions, name: descriptions[name] + " " + mutant}
        assert _claim_offenders(mutated) == [f"{name}: {mutant!r}"], (name, mutant)


@pytest.mark.asyncio
async def test_checking_exporting_and_routing_descriptions_name_the_gate() -> None:
    descriptions = await _descriptions()
    missing_tools = {name for name in _MUST_NAME_THE_GATE if _GATE not in descriptions[name]}
    missing_toolsets = {
        name for name in _TOOLSETS_MUST_NAME_THE_GATE if _GATE not in descriptions[f"toolset:{name}"]
    }
    assert not missing_tools, sorted(missing_tools)
    assert not missing_toolsets, sorted(missing_toolsets)


def test_the_patterns_catch_what_they_are_for() -> None:
    assert _AUTHORITATIVE.search("sch_run_erc is the authoritative check")
    assert _AUTHORITATIVE.search("the AUTHORITATIVE engine")
    assert not _AUTHORITATIVE.search("a non-authoritative render")
    assert not _AUTHORITATIVE.search("Always returns authoritative: false")
    for claim in ("Export a complete package", "JLCPCB-ready BOM", "board is DONE", "fully routed",
                  "ready to fab", "when finished", "the board is ready", "fab ready and verified",
                  "DRC-clean and fully connected", "the definitive check", "checked by real DRC",
                  "the board passes", "Every net is connected after this", "it PASSES DRC",
                  "all nets are connected", "the net is now connected", "VCC is fully connected",
                  "GND is then connected", "passes: all checks"):
        assert has_claim(claim), claim
    for fine in ("unrouted nets", "status incomplete", "routes the board", "already",
                 "a ready KiCad symbol", "no ready asset", "ready symbol/footprint/3D assets",
                 "an unverified gap", "cleaned up",
                 "retry with more passes", "max_passes", "never counted as passes",
                 "KiCad is connected, but no PCB document is open", "is not connected",
                 "disconnected", "is connector J1", "bypasses", "compasses",
                 "the caller stores the result and passes it as `baseline`"):
        assert not has_claim(fine), fine
    assert _points_to_the_gate("only a verify_board PASS may be reported as routed or done")
    assert not _points_to_the_gate("DRC-clean and fully connected, so verify_board can be skipped")


def test_the_grammar_exclusions_hold_only_in_their_reviewed_context() -> None:
    # Fix round 1 of build ticket 60: each exclusion is anchored to the one text it was
    # reviewed for, so the same words elsewhere are still a claim.
    for reviewed in (
        "KiCad is connected, but no PCB document is open in it",
        "KiCad is connected, but no schematic document is open in it",
        "Read-only: the caller stores the result and passes it as `baseline`.",
        "fill_fraction counts every fill polygon, islands included; whether pads are connected "
        "is decided separately by kicad-cli (connectivity)",
        "whether the nets are connected is for kicad-cli to say",
    ):
        assert not has_claim(reviewed), reviewed
    for claim in (
        "the board in KiCad is connected",
        "Afterwards every net of the board in KiCad is connected.",
        "kicad is connected, but no PCB document is open in it",
        "Now KiCad is connected and every net too",
        "KiCad is connected",
        "kicad-cli passes it as clean",
        "the board passes it as `baseline`",
        "the board passes them to fab",
        "the board is connected whether or not the zones are filled",
        "whether or not the zones are filled, the board is connected",
        "whether the board is connected, every net is connected",
        "we checked whether it is connected: the board passes",
        # the two-word bound of _QUESTION: a wider cut would swallow these claims
        "we checked whether the run finished and the board is connected",
        "whether the zones are filled and every net is connected",
        "whether the check ran and the board is connected",
        # an answered question is a claim, not a question
        "kicad-cli reports whether every net is connected: yes",
        "whether the nets are connected, true",
        "whether every net is connected — it is",
        # the noun exclusions elsewhere
        # each of these holds exactly one candidate match, so each anchor of the
        # noun exclusions is the only thing between it and a claim
        "the check reported as passes",
        "the board has more passes",
        "the run has so many passes",
        # the exact edge of _QUESTION: a three-word subject is not cut, so it is a claim
        "whether the whole board is connected",
        "whether the whole board is connected, every net is fine",
    ):
        assert has_claim(claim), claim
