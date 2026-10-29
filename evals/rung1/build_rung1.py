"""Rung-1 schematic + BOM build (ticket 09), driven only through this
project's own toolsets (project, library, schematic, sourcing, simulation)
plus kicad-cli for ERC.  Re-runnable: rebuilds evals/rung1/project/.

Run:  .venv/Scripts/python.exe evals/rung1/build_rung1.py

Deliberate gaps (reported, not hidden):
 * LCSC/MPN properties are set through the kicad_tools API because the
   schematic toolset's add_symbols has no custom-property field.
 * Part tiers/stock in PARTS were captured from JLCPCB's public parts API
   on 2026-10-11 (search_parts' LCSC provider is bot-walled); the vendored
   local index is re-queried here as an offline cross-check.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE / "project"
NAME = "rung1_usbc_attiny1616"
SCH = PROJ / f"{NAME}.kicad_sch"
ASSET = HERE / "assets" / "Rung1_USBC"
NGSPICE_FALLBACK = (
    Path.home()
    / "AppData/Local/Autodesk/webdeploy/production/c657666a51cec7a9bbd042e10872bcd9532508a8"
    / "Applications/Electron/LibEagle/ngspice/bin/ngspice_con.exe"
)

from mcp.server.mcpserver import MCPServer  # noqa: E402

from kicad_mcp.kicad_cli import find_kicad_cli  # noqa: E402

# tier/stock: JLCPCB parts API, 2026-10-11.
PARTS = [
    dict(ref="J1", lcsc="C456012", mpn="TYPE-C 6P (SHOU HAN)", value="USB_C_PWR", category="connector", tier="extended",
         lib_id="Rung1_USBC:USB_C_Receptacle_Power_6P", footprint="Rung1_USBC:USB_C_Receptacle_Shouhan_TYPE-C-6P", stock=665006,
         reason="Power-only 6-pin USB-C receptacle (VBUS/GND/CC1/CC2/shield) matches the power-only brief and keeps pad count low. "
                "No Basic or Preferred USB-C receptacle exists in the local tier index or in JLCPCB search (all results Extended); most-stocked Extended chosen.",
         extended_exception="no Basic/Preferred USB-C receptacle exists"),
    dict(ref="F1", lcsc="C20978", mpn="SMD0805P050TF", value="500mA PTC", category="protection", tier="extended",
         lib_id="Device:Polyfuse", footprint="Fuse:Fuse_0805_2012Metric", stock=19191,
         reason="Hold 0.5 A / trip 1 A (USB default 500 mA), Vmax 6 V >= 5.5 V bus, 0805. Every PTC returned by JLCPCB search is Extended "
                "(none Basic/Preferred); most-stocked 0805 chosen over 1206 for size.",
         extended_exception="no Basic/Preferred PTC exists"),
    dict(ref="D1", lcsc="C7420377", mpn="SMBJ6.5CA", value="SMBJ6.5CA", category="protection", tier="basic",
         lib_id="Device:D_TVS", footprint="Diode_SMD:D_SMB", stock=1312521,
         reason="Basic. Standoff 6.5 V > 5.5 V max VBUS, breakdown 7.22 V, clamp 11.2 V (< regulator abs max). "
                "Preferred SMBJ5.0CA/SMAJ5.0A not needed because a Basic part meets the rating."),
    dict(ref="U2", lcsc="C14289", mpn="HT7533-1", value="HT7533-1", category="regulator", tier="basic",
         lib_id="Regulator_Linear:HT75xx-1-SOT89", footprint="Package_TO_SOT_SMD:SOT-89-3", stock=75769,
         reason="Basic. 3.3 V, 100 mA, 30 V Vin max, 2.5 uA Iq: fits the ~15 mA load and the 11 V TVS clamp, no min-load. "
                "Rejected Basic AMS1117-3.3 (C6186: datasheet wants 22 uF tantalum + 5-10 mA min load) and XC6206 (C5446: 6 V recommended, below the TVS clamp). "
                "UNVERIFIED: the HT7533 datasheet could not be fetched (LCSC/Holtek bot walls). Pinout 1=GND 2=VIN 3=VOUT agrees between the KiCad stock "
                "symbol and the JLCPCB library symbol; ratings come from the JLCPCB parametric record only."),
    dict(ref="U1", lcsc="C507118", mpn="ATTINY1616-MNR", value="ATtiny1616-M", category="mcu", tier="extended",
         lib_id="MCU_Microchip_ATtiny:ATtiny1616-M", footprint="Package_DFN_QFN:VQFN-20-1EP_3x3mm_P0.4mm_EP1.7x1.7mm", stock=7501,
         reason="The rung-1 target MCU (ATtiny1616, VQFN-20 3x3, 0.4 mm pitch); only one orderable variant, Extended.",
         extended_exception="the mandated MCU has no Basic/Preferred listing"),
    dict(ref="C1", lcsc="C15850", mpn="CL21A106KAYNNNE", value="10uF/25V", category="passive_rc", tier="basic",
         lib_id="Device:C", footprint="Capacitor_SMD:C_0805_2012Metric", stock=4311824,
         reason="Basic. VBUS_F bulk / LDO input; 25 V gives margin over 5 V and hot-plug ringing (the Basic 0603 10 uF is only 10 V)."),
    dict(ref="C2", lcsc="C15849", mpn="CL10A105KB8NNNC", value="1uF/50V", category="passive_rc", tier="basic",
         lib_id="Device:C", footprint="Capacitor_SMD:C_0603_1608Metric", stock=8607509,
         reason="Basic. 3.3 V LDO output capacitor (1 uF class)."),
    dict(ref="C3", lcsc="C14663", mpn="CC0603KRX7R9BB104", value="100nF/50V", category="passive_rc", tier="basic",
         lib_id="Device:C", footprint="Capacitor_SMD:C_0603_1608Metric", stock=56393013,
         reason="Basic. MCU VCC decoupling."),
    dict(ref="R1", lcsc="C23186", mpn="0603WAF5101T5E", value="5.1k", category="passive_rc", tier="basic",
         lib_id="Device:R", footprint="Resistor_SMD:R_0603_1608Metric", stock=24566690,
         reason="Basic. USB-C CC1 Rd pulldown, 5.1k +/-1%, 75 V rating."),
    dict(ref="R2", lcsc="C23186", mpn="0603WAF5101T5E", value="5.1k", category="passive_rc", tier="basic",
         lib_id="Device:R", footprint="Resistor_SMD:R_0603_1608Metric", stock=24566690,
         reason="Basic. USB-C CC2 Rd pulldown."),
    dict(ref="R3", lcsc="C21190", mpn="0603WAF1001T5E", value="1k", category="passive_rc", tier="basic",
         lib_id="Device:R", footprint="Resistor_SMD:R_0603_1608Metric", stock=26965395,
         reason="Basic. LED series resistor: (3.3 - ~2.0) V / 1k ~ 1.3 mA, visible and well under 20 mA."),
    dict(ref="D2", lcsc="C2286", mpn="KT-0603R", value="LED_RED", category="led", tier="basic",
         lib_id="Device:LED", footprint="LED_SMD:LED_0603_1608Metric", stock=7259832,
         reason="Basic. 0603 red status LED, Vf 1.8-2.4 V, 20 mA max."),
    dict(ref="J2", lcsc="C49257", mpn="2.54-1*3PIN (BOOMELE)", value="UPDI", category="connector", tier="extended",
         lib_id="Connector_Generic:Conn_01x03", footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical", stock=277004,
         reason="1x3 2.54 mm through-hole header (VCC, UPDI, GND). Every 1x3 header on JLCPCB is Extended; through-hole, "
                "intended hand-soldered, so no SMT feeder fee.",
         extended_exception="no Basic/Preferred header exists; through-hole, hand-soldered"),
]

POS = {  # ref -> (x, y) mm, all on the 1.27 mm grid
    "J1": (50.8, 63.5), "R1": (88.9, 50.8), "R2": (101.6, 50.8), "F1": (88.9, 88.9),
    "D1": (114.3, 88.9), "C1": (127.0, 88.9), "U2": (165.1, 88.9), "C2": (190.5, 88.9),
    "U1": (127.0, 152.4), "C3": (101.6, 139.7), "J2": (190.5, 139.7), "R3": (165.1, 152.4),
    "D2": (190.5, 165.1),
}


def tools_from(module) -> dict:
    mcp = MCPServer("rung1")
    module.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}


L, R, U, D = (-1, 0), (1, 0), (0, -1), (0, 1)
STUB = 2.54


def _stubbed(items):
    """items: (kind, text, pin_x, pin_y, direction).  Returns (wires, labels):
    a 2.54 mm wire off each pin end, with the label/power symbol at its tip."""
    wires, labs = [], []
    seen = set()
    for kind, text, x, y, (dx, dy) in items:
        ex, ey = round(x + dx * STUB, 2), round(y + dy * STUB, 2)
        if (x, y, dx, dy) not in seen:  # one stub per pin even if two symbols share its tip
            seen.add((x, y, dx, dy))
            wires.append(dict(p1=[x, y], p2=[ex, ey], route="direct", snap=False))
        rot = {L: 180, R: 0, U: 90, D: 270}[(dx, dy)] if kind == "label" else 0
        labs.append(dict(kind=kind, text=text, x=ex, y=ey, rotation=rot, snap=False))
    return wires, labs


def net_specs():
    # Pin coordinates: sheet x = sym x + px ; sheet y = sym y - py (library y is up).
    P, Lb = "power", "label"
    items = [
        (P, "power:GND", 38.1, 69.85, L), (P, "power:PWR_FLAG", 38.1, 69.85, L),    # J1 A12
        (P, "power:VBUS", 38.1, 67.31, L), (P, "power:PWR_FLAG", 38.1, 67.31, L),   # J1 A9
        (Lb, "CC1", 38.1, 62.23, L), (Lb, "CC2", 38.1, 64.77, L),                   # J1 A5, B5
        (P, "power:VBUS", 38.1, 59.69, L), (P, "power:GND", 38.1, 57.15, L),        # J1 B9, B12
        (P, "power:GND", 63.5, 62.23, R),                                           # J1 shield
        (Lb, "CC1", 88.9, 46.99, U), (P, "power:GND", 88.9, 54.61, D),              # R1
        (Lb, "CC2", 101.6, 46.99, U), (P, "power:GND", 101.6, 54.61, D),            # R2
        (P, "power:VBUS", 88.9, 85.09, U), (Lb, "VBUS_F", 88.9, 92.71, D),          # F1
        (P, "power:PWR_FLAG", 88.9, 92.71, D),
        (Lb, "VBUS_F", 110.49, 88.9, L), (P, "power:GND", 118.11, 88.9, R),         # D1
        (Lb, "VBUS_F", 127.0, 85.09, U), (P, "power:GND", 127.0, 92.71, D),         # C1
        (P, "power:GND", 165.1, 93.98, D), (Lb, "VBUS_F", 157.48, 86.36, L),        # U2
        (P, "power:+3V3", 172.72, 86.36, R),
        (P, "power:+3V3", 190.5, 85.09, U), (P, "power:GND", 190.5, 92.71, D),      # C2
        (P, "power:+3V3", 127.0, 129.54, U), (P, "power:GND", 127.0, 175.26, D),    # U1 VCC/GND(3,21)
        (Lb, "UPDI", 142.24, 134.62, R), (Lb, "LED_CTL", 142.24, 152.4, R),         # U1 PA0, PA7
        (P, "power:+3V3", 101.6, 135.89, U), (P, "power:GND", 101.6, 143.51, D),    # C3
        (P, "power:+3V3", 185.42, 137.16, L), (Lb, "UPDI", 185.42, 139.7, L),       # J2
        (P, "power:GND", 185.42, 142.24, L),
        (Lb, "LED_CTL", 165.1, 148.59, U), (Lb, "LED_A", 165.1, 156.21, D),         # R3
        (P, "power:GND", 186.69, 165.1, L), (Lb, "LED_A", 194.31, 165.1, R),        # D2
    ]
    wires, labs = _stubbed(items)
    # Unused MCU GPIOs get explicit no-connect flags (design decision, see README of results).
    ux, uy = POS["U1"]
    unused = {1: 12.7, 2: 10.16, 5: 7.62, 6: 5.08, 7: 2.54, 9: -17.78, 10: -15.24, 11: -12.7, 12: -10.16,
              13: -7.62, 14: -5.08, 20: 15.24}
    for _, py in unused.items():
        labs.append(dict(kind="no_connect", x=round(ux + 15.24, 2), y=round(uy - py, 2), snap=False))
    for py in (-5.08, -7.62, -10.16, -12.7):  # PC0..PC3, left side
        labs.append(dict(kind="no_connect", x=round(ux - 15.24, 2), y=round(uy - py, 2), snap=False))
    return wires, labs


def _without_local_paths(value):
    """Drop the schematic tools' `backup_dir` (a path on the building machine, no evidence)."""
    if isinstance(value, dict):
        return {k: _without_local_paths(v) for k, v in value.items() if k != "backup_dir"}
    if isinstance(value, list):
        return [_without_local_paths(v) for v in value]
    return value


def main() -> int:
    from kicad_mcp import kicad_cli
    from kicad_mcp.toolsets import library, project, schematic, simulation, sourcing

    src = find_kicad_cli().parent.parent / "share" / "kicad" / "symbols"
    stage = Path(tempfile.gettempdir()) / "rung1_symbols"
    stage.mkdir(exist_ok=True)
    for n in ["Device", "power", "MCU_Microchip_ATtiny", "Regulator_Linear", "Connector_Generic"]:
        shutil.copy(src / f"{n}.kicad_sym", stage)
    shutil.copy(ASSET / "Rung1_USBC.kicad_sym", stage)
    os.environ["KICAD_SYMBOL_DIR"] = str(stage)
    if not os.environ.get("NGSPICE_PATH") and NGSPICE_FALLBACK.is_file():
        os.environ["NGSPICE_PATH"] = str(NGSPICE_FALLBACK)

    steps: dict = {}
    PROJ.mkdir(parents=True, exist_ok=True)
    for old in PROJ.glob(f"{NAME}*"):
        old.unlink()
    for t in ("sym-lib-table", "fp-lib-table"):
        (PROJ / t).unlink(missing_ok=True)
    steps["project_create"] = project._project_create(str(PROJ), NAME, 40, 30, overwrite=True)["status"]

    libdir = PROJ / "lib"
    shutil.rmtree(libdir, ignore_errors=True)
    shutil.copytree(ASSET, libdir / "Rung1_USBC")
    reg = tools_from(library)["register_library_asset"](
        str(PROJ), symbol_lib_path=str(libdir / "Rung1_USBC" / "Rung1_USBC.kicad_sym"),
        footprint_lib_path=str(libdir / "Rung1_USBC" / "Rung1_USBC.pretty"), nickname="Rung1_USBC")
    steps["register_library_asset"] = reg["status"]

    sch = tools_from(schematic)
    p = str(SCH)
    specs = [dict(lib_id=q["lib_id"], x=POS[q["ref"]][0], y=POS[q["ref"]][1], ref=q["ref"], value=q["value"])
             for q in PARTS]
    steps["add_symbols"] = sch["add_symbols"](p, specs)
    wires, labs = net_specs()
    steps["add_wires"] = sch["add_wires"](p, wires)
    steps["add_labels"] = sch["add_labels"](p, labs)
    steps["fix_annotation"] = sch["fix_annotation"](p)
    steps["assign_footprints"] = sch["assign_footprints"](p, {q["ref"]: q["footprint"] for q in PARTS})

    from kicad_tools.schematic.models.schematic import Schematic
    s = Schematic.load(SCH)
    byref = {q["ref"]: q for q in PARTS}
    n = 0
    for inst in s.symbols:
        q = byref.get(inst.reference)
        if q:
            inst.properties["LCSC"] = q["lcsc"]
            inst.properties["MPN"] = q["mpn"]
            n += 1
    s.write(SCH)
    steps["lcsc_properties_set"] = n
    steps["check_connectivity"] = sch["check_connectivity"](p)

    # ERC: kicad-cli, all severities, complete list
    erc = kicad_cli.run_erc(SCH)
    viols = []
    for sheet in erc.get("sheets", []):
        for v in sheet.get("violations", []):
            viols.append({"type": v.get("type"), "severity": v.get("severity"), "description": v.get("description"),
                          "items": [i.get("description") for i in v.get("items", [])],
                          "pos": [i.get("pos") for i in v.get("items", [])]})
    unexplained = len(viols)  # every violation is listed; none is pre-explained
    (HERE / "erc_report.json").write_text(json.dumps(
        {"source": "kicad-cli sch erc --severity-all --format json", "kicad_version": erc.get("kicad_version"),
         "violation_count": len(viols), "violations": viols, "unexplained": unexplained,
         "explanation": "J1 is rung 1's own symbol (evals/rung1/build_usbc_asset.py): VBUS, GND and the shield "
                        "are passive contacts and CC1/CC2 are bidirectional, like KiCad's stock USB-C receptacle symbols, "
                        "so no pin is Unspecified; every violation is listed and counted as unexplained.", "raw": erc},
        indent=1, default=str), encoding="utf-8")
    steps["erc_violations"] = len(viols)

    # Independent connectivity proof: kicad-cli netlist export, nets with their pins
    import re
    import subprocess
    nl = HERE / "spice" / "rung1.net"
    subprocess.run([str(kicad_cli.find_kicad_cli()), "sch", "export", "netlist", "--format", "kicadsexpr",
                    "-o", str(nl), str(SCH)], check=True, capture_output=True)
    body = nl.read_text(encoding="utf-8")
    body = body[body.index("(nets"):]
    nets = {}
    for blk in re.split(r"\n\t\t\(net\n", body)[1:]:
        nm = re.search(r'\(name "([^"]+)"', blk).group(1).lstrip("/")
        nets[nm] = sorted(f"{r}.{pn}" for r, pn in re.findall(r'\(ref "([^"]+)"\)\s*\(pin "([^"]+)"\)', blk))
    (HERE / "nets.json").write_text(json.dumps(nets, indent=1), encoding="utf-8")
    nl.unlink()

    # BOM
    from kicad_tools.schema import Schematic as SchemaSch
    rows = []
    for sym in SchemaSch.load(SCH).iter_symbols():
        if sym.reference.startswith("#"):
            continue
        q = byref[sym.reference]
        rows.append({"Designator": sym.reference, "Comment": sym.value, "Footprint": sym.footprint,
                     "LCSC Part #": q["lcsc"], "MPN": q["mpn"], "Tier": q["tier"]})
    rows.sort(key=lambda d: (d["Designator"][0], int(d["Designator"][1:])))
    with (HERE / "bom.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    cross = tools_from(sourcing)["lookup_jlcpcb_part_tiers"]([q["lcsc"] for q in PARTS])
    cx = {x["identifier"]: {"jlcpcb_class": x.get("jlcpcb_class"), "matched": x.get("matched")} for x in cross["results"]}
    keep = ("ref", "lcsc", "mpn", "category", "tier", "stock", "reason", "footprint")
    log = {"captured": "2026-10-11, JLCPCB parts API (search_parts LCSC provider is bot-walled)",
           "parts": [{**{k: q[k] for k in keep}, **({"extended_exception": q["extended_exception"]} if "extended_exception" in q else {}),
                      "local_index": cx.get(q["lcsc"])} for q in PARTS]}
    (HERE / "selection_log.json").write_text(json.dumps(log, indent=1, ensure_ascii=False), encoding="utf-8")

    # SPICE, reported, never gating
    sim = tools_from(simulation)
    (HERE / "spice").mkdir(exist_ok=True)
    exp = sim["export_spice_netlist"](p, str(HERE / "spice" / "rung1_export.cir"))
    op = sim["run_spice_simulation"](str(HERE / "spice" / "led_branch.cir"), {"type": "op", "probe": ["led_a"]})
    (HERE / "spice_report.json").write_text(json.dumps(
        {"gating": False, "status": op.get("status"), "ngspice": os.environ.get("NGSPICE_PATH"),
         "netlist_export": exp, "led_branch_op": op}, indent=1, default=str), encoding="utf-8")
    steps["spice"] = op.get("status")

    for junk in list(PROJ.glob("*_backup_*.kicad_sch")) + list(PROJ.glob("*-netlist.kicad_net")):
        junk.unlink()  # tool-generated side files, not deliverables
    steps = _without_local_paths(steps)
    (HERE / "results_steps.json").write_text(json.dumps(steps, indent=1, default=str), encoding="utf-8")
    print(json.dumps(steps, indent=1, default=str)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
