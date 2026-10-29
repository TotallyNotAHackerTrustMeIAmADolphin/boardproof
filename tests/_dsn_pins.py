"""Where Freerouting places each pin of a Specctra DSN, compared with KiCad's own pads.

The method of ``paradigm-review-2`` ticket 16 (``pinpos.py``), used by build ticket 46's
reproduction. Freerouting 2.5.0 (``board/model/items/Pin.java`` ``relativeLocation``,
``IntVector.turn90Degree``): take the image pin offset, mirror x if the component is placed
``back``, rotate counter-clockwise by the placement rotation in y-up math, add the placement.
It does not flip y on read, so a DSN that negated y (KiCad's exporter) maps back with
``y_sign = -1``, one that did not (kicad_tools) with ``+1``.

The oracle is KiCad's own pad position (``pads`` of the KiCad runner), never a DSN reader.
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path


def _tokens(text: str):
    i, n = 0, len(text)
    quote_next = False
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif quote_next:  # (string_quote ") names the quote character itself
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            yield text[i:j]
            quote_next = False
            i = j
        elif c in "()":
            yield c
            i += 1
        elif c == '"':
            j = text.index('"', i + 1)
            yield ("str", text[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            atom = text[i:j]
            yield atom
            quote_next = atom == "string_quote"
            i = j


def parse(text: str) -> list:
    stack: list[list] = [[]]
    for tok in _tokens(text):
        if tok == "(":
            stack.append([])
        elif tok == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(tok[1] if isinstance(tok, tuple) else tok)
    assert len(stack) == 1, "unbalanced DSN"
    return stack[0][0]


def kids(node: list, name: str) -> list:
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == name]


def one(node: list, name: str) -> list:
    found = kids(node, name)
    assert len(found) == 1, f"expected one ({name} ...), found {len(found)}"
    return found[0]


def misplaced_pins(dsn_path: Path, pads: list[dict], y_sign: float, tolerance_mm: float = 0.01) -> dict:
    """``{"pins": n, "misplaced": [(ref, pad, error_mm, class)], "unmatched": n}`` for every
    pin of every placed component of the DSN, against KiCad's pad positions."""
    dsn = parse(Path(dsn_path).read_text(encoding="utf-8", errors="replace"))
    unit_scale = 1000.0  # (unit um): DSN coordinates are micrometres
    truth: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for p in pads:
        truth[(p["ref"], str(p["pad"]))].append(p)
    images = {img[1]: img for img in kids(one(dsn, "library"), "image")}
    pins = 0
    unmatched = 0
    misplaced = []
    for component in kids(one(dsn, "placement"), "component"):
        image = images[component[1]]
        for place in kids(component, "place"):
            ref, px, py, side, rot = place[1], float(place[2]), float(place[3]), place[4], float(place[5])
            for pin in kids(image, "pin"):
                rest = pin[2:]
                if isinstance(rest[0], list) and rest[0][0] == "rotate":
                    rest = rest[1:]
                pin_id, x, y = rest[0], float(rest[1]), float(rest[2])
                number = pin_id.split("@")[0]
                if side == "back":
                    x = -x
                a = math.radians(rot)
                rx, ry = x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)
                ax, ay = (px + rx) / unit_scale, y_sign * (py + ry) / unit_scale
                candidates = truth.get((ref, number))
                if not candidates:
                    unmatched += 1
                    continue
                pins += 1
                error = min(math.hypot(ax - c["x_mm"], ay - c["y_mm"]) for c in candidates)
                if error > tolerance_mm:
                    c = candidates[0]
                    misplaced.append((ref, number, round(error, 3), f"{c['side']} rot {c['fp_rotation']:g}"))
    return {"pins": pins, "misplaced": sorted(misplaced), "unmatched": unmatched}
