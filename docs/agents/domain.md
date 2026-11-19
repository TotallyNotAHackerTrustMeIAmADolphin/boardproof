# Domain docs

How the engineering skills consume this repository's domain documentation. Single-context.

## Before exploring, read these

- **`docs/CONTEXT.md`**: the glossary (the project's `GLOSSARY.md`; this repository keeps it
  under that name).
- **`docs/ARCHITECTURE.md`**: the design, its numbered decisions (the decisions stand in for ADRs;
  there is no `docs/adr/`) and "Known facts and gotchas".
- **`docs/ROADMAP.md`** (the acceptance ladder and non-goals) and **`docs/TESTING.md`** (the
  acceptance gate and the test suite).

## Use the glossary's vocabulary

Use a term as `docs/CONTEXT.md` defines it, and not a synonym it avoids. A concept the glossary
lacks is a gap to record, not language to invent.

## Flag decision conflicts

If your output contradicts a numbered decision of `docs/ARCHITECTURE.md`, say so explicitly
instead of silently overriding it.
