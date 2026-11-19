# Review rules: what a finding blocks, and the stop rule

Project rules of the adversarial review (playbook section 5, project values). CLAUDE.md names this
file; the reviewer and the coordinator follow it. Why the rules exist: `docs/HISTORY.md`
(incidents 2, 3 and 6).

- **What a finding blocks (owner, 2026-10-21; incident 6 in `docs/HISTORY.md`):** a review classes every finding
  BLOCKS or FOLLOW-UP. CLEAN means no BLOCKS finding; it does not mean no finding at all.
  - **BLOCKS:**
    - a BLOCKS finding from an earlier round that is not closed; a FOLLOW-UP never blocks;
    - an acceptance item not met: not built, or wrong for an input the item names or the ticket's
      tests cover. An input the code refuses with a true reason meets the item unless the item
      requires that input to succeed. Shrinking a claim never closes one. Only the coordinator,
      with the owner, moves an item to another ticket, and records the move in the ticket;
    - a regression the ticket's commits cause: behaviour that was right at the base or at the
      previous reviewed head and is wrong now, wherever it shows;
    - a fail-open path in code the ticket added or changed: an input it does not refuse that
      yields pass, ok or success instead of incomplete or refused (incident 3). Refusing that input
      with a true reason closes the finding; handling it fully is a FOLLOW-UP. An evasion form of a
      static scan or ratchet (FOLLOW-UP list) is not a fail-open path; a runtime guard has no such
      exemption;
    - a false claim in the lines the ticket wrote or changed, or in a verdict or value its code
      returns. Shrinking the claim to what the code proves closes it as well as widening the code
      does;
    - a guard or acceptance test that stays green when the key line it guards is broken, or an
      existing test weakened;
    - a breach of a CLAUDE.md hard rule or of a contract in the STANDARDS list of
      `docs/dispatch/reviewer.md`.

    A second fail-open or wrong value of the same family makes the design itself the finding (a
    denylist, a special case per input), and only refusing everything not proven closes it (build
    ticket 67, round 3).
  - **FOLLOW-UP:**
    - an edge case outside the ticket's acceptance items that is not a fail-open path in its code;
    - a defect in code the delta did not touch, unless the delta causes it;
    - an over-refusal whose reason is true;
    - evasion of a static scan or ratchet: a form only an author aiming past the scan writes (a
      name built from strings, `getattr` on a computed string, `exec`, dunder or frame access).
      Forms ordinary code uses (aliases, lazy, local or class-body imports, wrappers, re-exports)
      are not evasion. The scan's docstring says it does not defend against evasion and names
      these classes; until it does, the claim is false and BLOCKS. A form in a named class is a
      FOLLOW-UP.
  - A delta review judges the delta and the findings it answers. It opens no new axis of attack on
    unchanged code; what it finds there is a FOLLOW-UP unless it is a regression the delta causes.
  - A finding that fits both lists BLOCKS, and so does one the reviewer and the coordinator class
    differently.
  - Builders fix BLOCKS findings, and a fix a reviewer finds lands in the same ticket (owner,
    2026-11-11: "fixes die ein reviewer findet sollten im selben ticket landen"). A FOLLOW-UP
    that is a pure fix (a test gap, a wording fix, a small hardening of code the ticket touched) is
    fixed on the ticket's branch before the merge, in the same fix round as the BLOCKS findings. It
    is not a ticket and not a comment list. Re-class before you file: a guard in code the ticket
    touched whose test survives its mutant is BLOCKS, not a FOLLOW-UP.
  - A FOLLOW-UP-only fix of 30 lines or fewer without new logic (counted as added plus removed lines
    of `git diff --stat`, tests included) needs no new review round: the coordinator reads the diff
    or asks the same reviewer, and it does not count as a delta review for the stop rule. A fix
    mixed into a BLOCKS round is reviewed in that round's delta. A larger or logic-changing fix is
    new code under review.
  - A new ticket is opened only for new knowledge, with the reason in one line: a production bug
    (a wrong value, or ok/success the user sees) in code the ticket did not touch, a measured fact,
    a design question, a scope decision or a feature request. A question only the owner can settle
    goes to the owner (label needs-owner), not into a build ticket.
  - "No merge without CLEAN" and the stop rule below stay as they are. Findings from rounds before
    2026-10-21 are classed by the ticket's next review.
- **Each BLOCKS finding becomes a failing test before its fix; a FOLLOW-UP's test is written in the same fix round.** The reviewer's report gives each finding as the failing test that should exist.
- **A stop rule for deep review loops (section 5):** after the third delta review of one module,
  no reviewer is dispatched again without the owner. The module's claims shrink to what it proves
  (per-item `proven | unproven`, a `proves` field), and the authority stays with the gate. The
  coordinator checks the latest diff against the open BLOCKS findings and escalates to the owner.
  The owner may grant one more fix round, or move the open findings into a ticket by scope decision (an
  exception to the FOLLOW-UP rule above; never a hard-rule breach); either way one more delta review judges what is left and gives the verdict.
  See incidents 2 and 6. The stop rule never permits a merge without a CLEAN verdict: it
  escalates, it does not wave through.
- **A stop rule for deep review loops (section 5):** after the third delta review of one module,
  no reviewer is dispatched again without the owner. The module's claims shrink to what it proves
  (per-item `proven | unproven`, a `proves` field), and the authority stays with the gate. The
  coordinator checks the latest diff against the open BLOCKS findings and escalates to the owner.
  The owner may grant one more fix round, or move the open findings into a ticket by scope decision (an
  exception to the FOLLOW-UP rule above; never a hard-rule breach); either way one more delta review judges what is left and gives the verdict.
  See incidents 2 and 6 in `docs/HISTORY.md`. The stop rule never permits a merge without a CLEAN verdict: it
  escalates, it does not wave through.
