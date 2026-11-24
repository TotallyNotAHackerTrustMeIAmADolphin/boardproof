"""A small hand-built index in the jlcparts cache schema (issue 199). No network, no real data.

The schema is the one read from the jlcparts source (docs/ARCHITECTURE.md, "jlcparts cache format"):
components / categories / manufacturers, lcsc stored as an integer without the C, price a JSON list of
{qFrom, qTo, price}, extra and jlc_extra JSON text holding {"attributes": {name: text}}. The part values are
invented. 3 categories with 4 subcategories, 36 parts, one part without attributes and one with a list
where jlcparts could hold one (extra = []).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

FIXTURE_DATE = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)
NEWEST = int(FIXTURE_DATE.timestamp())

SCHEMA = """
CREATE TABLE components (
    lcsc INTEGER PRIMARY KEY NOT NULL, category_id INTEGER NOT NULL, mfr TEXT NOT NULL, package TEXT NOT NULL,
    joints INTEGER NOT NULL, manufacturer_id INTEGER NOT NULL, basic INTEGER NOT NULL,
    preferred INTEGER NOT NULL DEFAULT 0, description TEXT NOT NULL, datasheet TEXT NOT NULL,
    stock INTEGER NOT NULL, price TEXT NOT NULL, last_update INTEGER NOT NULL, extra TEXT,
    flag INTEGER NOT NULL DEFAULT 0, last_on_stock INTEGER NOT NULL DEFAULT 0,
    jlc_extra TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE manufacturers (id INTEGER PRIMARY KEY NOT NULL, name TEXT NOT NULL, UNIQUE (id, name));
CREATE TABLE categories (id INTEGER PRIMARY KEY NOT NULL, category TEXT NOT NULL, subcategory TEXT NOT NULL,
    UNIQUE (id, category, subcategory));
CREATE INDEX components_category ON components (category_id);
"""

CATEGORIES = {
    1: ("Resistors", "Chip Resistor - Surface Mount"),
    2: ("Capacitors", "Multilayer Ceramic Capacitors MLCC - SMD/SMT"),
    3: ("Capacitors", "Aluminum Electrolytic Capacitors - SMD"),
    4: ("Diodes", "Schottky Diodes"),
}
MANUFACTURERS = {1: "UNI-ROYAL(Uniroyal Elec)", 2: "Samsung Electro-Mechanics", 3: "YAGEO", 4: "Nexperia", 5: "Panasonic"}


def _price(*tiers: tuple[int, float]) -> str:
    out = []
    for i, (q, p) in enumerate(tiers):
        q_to = tiers[i + 1][0] - 1 if i + 1 < len(tiers) else None
        out.append({"qFrom": q, "qTo": q_to, "price": p})
    return json.dumps(out)


def _attrs(**kw: str) -> str:
    return json.dumps({"attributes": {k.replace("_", " "): v for k, v in kw.items()}})


# lcsc, category, mfr, package, mfr_id, basic, preferred, stock, price, attributes (extra text), description
def _rows() -> list[tuple]:
    R = "Ω"
    rows: list[tuple] = []
    resistors = [
        (1001, "0402WGF1002TCE", "0402", 1, 1, 0, 120000, (1, 0.002), ("10k" + R, "±1%", "62.5mW")),
        (1002, "0402WGF4701TCE", "0402", 1, 1, 0, 80000, (1, 0.003), ("4.7k" + R, "±1%", "62.5mW")),
        (1003, "0603WAF1002T5E", "0603", 1, 1, 0, 300000, (1, 0.0015), ("10k" + R, "±1%", "100mW")),
        (1004, "0603WAF1003T5E", "0603", 1, 1, 0, 0, (1, 0.002), ("100k" + R, "±1%", "100mW")),
        (1005, "0603WAJ0102T5E", "0603", 1, 0, 0, 5000, (1, 0.004), ("1k" + R, "±5%", "100mW")),
        (1006, "RC0603FR-0710KL", "0603", 3, 0, 1, 40000, (1, 0.01), ("10k" + R, "±1%", "100mW")),
        (1007, "0805W8F1001T5E", "0805", 1, 1, 0, 90000, (1, 0.003), ("1k" + R, "±1%", "125mW")),
        (1008, "0603WAF2201T5E", "0603", 1, 1, 0, 70, (1, 0.006), ("2.2k" + R, "±1%", "100mW")),
        (1009, "RC0402JR-070RL", "0402", 3, 0, 0, 25000, (1, 0.005), ("0" + R, "±5%", "62.5mW")),
        (1010, "0603WAF1M00T5E", "0603", 1, 1, 0, 15000, (1, 0.008), ("1M" + R, "±1%", "100mW")),
        (1011, "0603WAF5100T5E", "0603", 1, 0, 0, 12000, (1, 0.007), ("510" + R, "±1%", "100mW")),
        (1012, "0603WAF100KT5E", "0603", 1, 1, 0, 33000, (1, 0.0016), ("100k" + R, "±5%", "100mW")),
    ]
    for lcsc, mfr, pkg, mid, basic, pref, stock, tier, (res, tol, pw) in resistors:
        price = _price((1, tier[1]), (100, tier[1] * 0.8), (1000, tier[1] * 0.6))
        rows.append((lcsc, 1, mfr, pkg, mid, basic, pref, stock, price,
                     _attrs(Resistance=res, Tolerance=tol, Power_Watts=pw), f"{res} {tol} {pw} {pkg} Chip Resistor"))
    caps = [
        (2001, "CL05B104KB54PNC", "0402", 2, 1, 0, 900000, 0.0012, ("100nF", "50V", "X7R")),
        (2002, "CL10B104KB8NNNC", "0603", 2, 1, 0, 1500000, 0.0015, ("100nF", "50V", "X7R")),
        (2003, "CL10A106KP8NNNC", "0603", 2, 1, 0, 400000, 0.011, ("10µF", "10V", "X5R")),
        (2004, "CL21A106KOQNNNE", "0805", 2, 1, 0, 250000, 0.02, ("10µF", "16V", "X5R")),
        (2005, "CL10B105KO8NNNC", "0603", 2, 1, 0, 600000, 0.006, ("1µF", "16V", "X7R")),
        (2006, "CL05C220JB5NNNC", "0402", 2, 1, 0, 700000, 0.001, ("22pF", "50V", "C0G")),
        (2007, "CC0603KRX7R9BB104", "0603", 3, 0, 1, 30000, 0.02, ("100nF", "50V", "X7R")),
        (2008, "CL10B103KB8NNNC", "0603", 2, 1, 0, 800000, 0.002, ("10nF", "50V", "X7R")),
        (2009, "CL31B225KBHNNNE", "1206", 2, 0, 0, 12000, 0.03, ("2.2µF", "50V", "X7R")),
        (2010, "CL03A104KQ3NNNC", "0201", 2, 0, 0, 0, 0.004, ("100nF", "6.3V", "X5R")),
    ]
    for lcsc, mfr, pkg, mid, basic, pref, stock, p, (cap, volt, diel) in caps:
        price = _price((1, p), (500, p * 0.7))
        rows.append((lcsc, 2, mfr, pkg, mid, basic, pref, stock, price,
                     _attrs(Capacitance=cap, Voltage_Rated=volt, Temperature_Coefficient=diel), f"{cap} {volt} {diel} {pkg} MLCC"))
    elec = [
        (3001, "EEE-FK1V101P", "SMD,D6.3xL5.8mm", 5, 0, 0, 3000, 0.25, ("100µF", "35V")),
        (3002, "EEEFK1C102AP", "SMD,D10xL10mm", 5, 0, 1, 1800, 0.45, ("1000µF", "16V")),
        (3003, "EEE-FT1V470AP", "SMD,D6.3xL5.8mm", 5, 0, 0, 0, 0.3, ("47µF", "35V")),
        (3004, "EEEHAV331M", "SMD,D8xL10mm", 5, 0, 0, 900, 0.4, ("330µF", "25V")),
    ]
    for lcsc, mfr, pkg, mid, basic, pref, stock, p, (cap, volt) in elec:
        rows.append((lcsc, 3, mfr, pkg, mid, basic, pref, stock, _price((1, p), (100, p * 0.85)),
                     _attrs(Capacitance=cap, Voltage_Rated=volt), f"{cap} {volt} Aluminum Electrolytic"))
    diodes = [
        (4001, "BAT54S,215", "SOT-23", 4, 1, 0, 200000, 0.02, ("30V", "200mA")),
        (4002, "PMEG4010CEH,115", "SOD-123F", 4, 0, 0, 15000, 0.05, ("40V", "1A")),
        (4003, "SS34", "SMA", 3, 1, 0, 90000, 0.03, ("40V", "3A")),
        (4004, "1N5819HW-7-F", "SOD-123", 3, 0, 0, 40000, 0.04, ("40V", "1A")),
    ]
    for lcsc, mfr, pkg, mid, basic, pref, stock, p, (volt, amp) in diodes:
        rows.append((lcsc, 4, mfr, pkg, mid, basic, pref, stock, _price((1, p)),
                     _attrs(Reverse_Voltage=volt, Forward_Current=amp), f"{volt} {amp} Schottky Diode"))
    # one part without any parameter data, one whose extra is a list (jlcparts handles that shape), one
    # with the JLCPCB-side attributes overriding the LCSC-side ones.
    rows.append((4005, 4, "NOATTR-1", "SOT-23", 4, 0, 0, 50, _price((1, 0.5)), "{}", "Schottky Diode without data"))
    rows.append((4006, 4, "LISTEXTRA-1", "SOT-23", 4, 0, 0, 60, _price((1, 0.6)), "[]", "Schottky Diode with list extra"))
    rows.append((4007, 4, "NOPRICE-1", "SOT-23", 4, 0, 0, 70, "[]", _attrs(Reverse_Voltage="60V", Forward_Current="2A"),
                 "Schottky Diode without price"))
    rows.append((1013, 1, "OVERRIDE-1", "0603", 1, 0, 0, 10, _price((1, 0.01)), _attrs(Resistance="-", Tolerance="±2%"),
                 "resistor with an LCSC dash and a JLCPCB value"))
    return rows


JLC_OVERRIDES = {1013: {"attributes": {"Resistance": "33k" + "Ω", "Tolerance": "-"}}}


def build_fixture(path: Path, *, newest: int = NEWEST, with_optional_columns: bool = True) -> Path:
    """Write the fixture index at ``path`` (a tmp_path file) and return it."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO manufacturers VALUES (?, ?)", MANUFACTURERS.items())
        conn.executemany("INSERT INTO categories VALUES (?, ?, ?)", [(i, c, s) for i, (c, s) in CATEGORIES.items()])
        rows = _rows()
        for k, (lcsc, cat, mfr, pkg, mid, basic, pref, stock, price, extra, desc) in enumerate(rows):
            last = newest if k == 0 else newest - 86400 * (k % 5)
            conn.execute(
                "INSERT INTO components (lcsc, category_id, mfr, package, joints, manufacturer_id, basic, preferred, "
                "description, datasheet, stock, price, last_update, extra, jlc_extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lcsc, cat, mfr, pkg, 2, mid, basic, pref, desc, f"https://example.invalid/ds/{lcsc}.pdf", stock, price, last,
                 extra, json.dumps(JLC_OVERRIDES.get(lcsc, {}))),
            )
        if not with_optional_columns:
            conn.executescript("ALTER TABLE components DROP COLUMN preferred; "
                               "ALTER TABLE components DROP COLUMN jlc_extra; ALTER TABLE components DROP COLUMN extra;")
        conn.commit()
    finally:
        conn.close()
    return path


def ignore_ancestors_of(monkeypatch, tmp_path: Path) -> None:
    """Issue 235: the target check scans every parent folder for KiCad files, so a stray board in the system
    temp folder refused every test. This patches ``Path.iterdir`` (through ``monkeypatch``, undone after the
    test) so that the real parent folders of tmp_path list as empty. It hides them from ALL code that runs in
    the test, not only the target check; tmp_path and below stay real."""
    real = Path.iterdir
    above = {p for p in tmp_path.resolve().parents}

    def iterdir(self):
        return iter(()) if self in above else real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
