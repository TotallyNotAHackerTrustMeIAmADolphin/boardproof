"""One rule for long tool answers (GitHub issue 230): counts and the first items by default, the
full list only when the caller passes ``detail=true``. Safety fields (status, code, revision_token,
not_measured, gate, refusal reasons) never go through here."""

from __future__ import annotations

from typing import Any

LIMIT = 20  # items of an attention list shown without detail
DETAIL_NOTE = "Lists are cut to the first 20 items; pass detail=true for every item."


def listing(name: str, items: list, detail: bool) -> dict[str, Any]:
    """``{name: items, name_count: n}``; without ``detail`` at most LIMIT items, and
    ``name_truncated: true`` when some were cut."""
    out: dict[str, Any] = {name: list(items) if detail else list(items[:LIMIT]), f"{name}_count": len(items)}
    if not detail and len(items) > LIMIT:
        out[f"{name}_truncated"] = True
    return out


def counted(name: str, items: list, detail: bool) -> dict[str, Any]:
    """A list that needs no attention: only ``name_count`` without ``detail``, the list too with it."""
    out: dict[str, Any] = {f"{name}_count": len(items)}
    if detail:
        out[name] = list(items)
    return out
