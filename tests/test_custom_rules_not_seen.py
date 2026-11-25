"""Custom .kicad_dru rules the router cannot see are reported, never dropped silently (issue 236)."""

from __future__ import annotations

from boardproof import dru, write_layer
from boardproof.toolsets import routing as routing_toolset

CUSTOM = (
    '(version 1)\n'
    '(rule "Mains to Sig"\n\t(condition "A.NetClass == \'Mains\' && B.NetClass == \'Sig\'")\n'
    '\t(constraint clearance (min 3mm)))\n'
    '(rule "Hole"\n\t(constraint hole_clearance (min 0.5mm)))\n'
)


def _put(pcb, data: bytes) -> None:
    write_layer.save_bytes(data, pcb.with_suffix(".kicad_dru"), verify=lambda _p: None)


def _pcb(tmp_path, dru_text):
    pcb = tmp_path / "b.kicad_pcb"  # never created: only the sibling rules path is used
    if dru_text is not None:
        _put(pcb, dru_text.encode("utf-8"))
    return pcb


def test_no_rules_file_means_nothing_unseen(tmp_path):
    assert routing_toolset.custom_rules_not_seen(_pcb(tmp_path, None)) == []


def test_hand_written_rules_are_listed_with_name_condition_and_constraints(tmp_path):
    unseen = routing_toolset.custom_rules_not_seen(_pcb(tmp_path, CUSTOM))
    assert [r["name"] for r in unseen] == ["Mains to Sig", "Hole"]
    assert unseen[0]["condition"] == "A.NetClass == 'Mains' && B.NetClass == 'Sig'"
    assert unseen[0]["constraints"] == [{"kind": "clearance", "min_mm": 3.0}]
    assert unseen[1]["condition"] is None


def test_managed_block_rules_are_not_listed_as_unseen(tmp_path):
    text = CUSTOM + dru.render_block({("Gnd", "Sig"): 0.3})
    unseen = routing_toolset.custom_rules_not_seen(_pcb(tmp_path, text))
    assert [r["name"] for r in unseen] == ["Mains to Sig", "Hole"]


def test_an_unreadable_rules_file_is_reported_not_ignored(tmp_path):
    pcb = _pcb(tmp_path, None)
    _put(pcb, bytes([0xFF, 0xFE]) + b"(rule")
    unseen = routing_toolset.custom_rules_not_seen(pcb)
    assert unseen and unseen[0]["name"] == "?" and "not UTF-8" in unseen[0]["reason"]


def test_the_note_names_the_unseen_rules():
    note = routing_toolset.pair_rules_note(
        {"applied_pair_rules": [], "pairs_not_applied": [], "problems": [],
         "custom_rules_not_seen": [{"name": "Mains to Sig"}]})
    assert note and "Mains to Sig" in note and "does not see" in note


def test_a_hand_written_rule_with_a_managed_name_is_still_listed(tmp_path):
    clash = ('(rule "boardproof pair clearance: Gnd / Sig"\n\t(condition "A.NetClass == \'Gnd\'")\n'
             '\t(constraint clearance (min 0.05mm)))\n')
    text = "(version 1)\n" + clash + dru.render_block({("Gnd", "Sig"): 0.3})
    unseen = routing_toolset.custom_rules_not_seen(_pcb(tmp_path, text))
    assert [r["name"] for r in unseen] == ["boardproof pair clearance: Gnd / Sig"]
    assert unseen[0]["condition"] == "A.NetClass == 'Gnd'"


def test_a_file_defect_is_carried_as_an_entry_not_dropped(tmp_path):
    unbalanced = '(rule "A"\n\t(constraint clearance (min 1mm))\n(rule "B"\n\t(constraint clearance (min 2mm)))\n'
    unseen = routing_toolset.custom_rules_not_seen(_pcb(tmp_path, "(version 1)\n" + unbalanced))
    assert any(r["name"] == "?" and r["reason"] for r in unseen)
    unseen = routing_toolset.custom_rules_not_seen(_pcb(tmp_path, '(rule "A" (constraint clearance (min 1mm)))\n'))
    assert any("version" in r["reason"] for r in unseen) and any(r["name"] == "A" for r in unseen)


def test_a_hand_edited_block_leaves_every_rule_unseen(tmp_path):
    text = dru.render_block({("Gnd", "Sig"): 0.3}).replace("0.3mm", "0.31mm\n\t(constraint track_width (min 1mm))")
    unseen = routing_toolset.custom_rules_not_seen(_pcb(tmp_path, "(version 1)\n" + text))
    assert [r["name"] for r in unseen] == ["boardproof pair clearance: Gnd / Sig"]


def test_a_clean_file_with_no_version_line_lists_nothing(tmp_path):
    assert routing_toolset.custom_rules_not_seen(_pcb(tmp_path, "")) == []
    assert routing_toolset.custom_rules_not_seen(_pcb(tmp_path, dru.render_block({("Gnd", "Sig"): 0.3}))) == []


def test_a_rule_without_a_version_line_is_listed_with_honest_wording(tmp_path):
    unseen = routing_toolset.custom_rules_not_seen(_pcb(tmp_path, '(rule "A" (constraint clearance (min 1mm)))\n'))
    assert [r["name"] for r in unseen] == ["A", "?"]
    assert "version" in unseen[1]["reason"] and "may be missing" not in unseen[1]["reason"]
