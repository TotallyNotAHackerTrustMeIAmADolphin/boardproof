"""``generate_footprint`` through the MCP surface (build ticket 69).

Seam: ``MCPServer.call_tool``, the path every MCP client takes (as test_copilot_toolset.py),
plus ``kicad-cli`` as the load oracle for what the tool wrote. Before this ticket the tool's
signature was ``(package_type, output_path, **kwargs)``; the SDK published that as one
``kwargs`` object, and every call failed with "unexpected keyword argument 'kwargs'". The
tests that called the inner function directly never saw it.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import typing
from pathlib import Path

import pytest
from kicad_tools.library import generators as kt_generators
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from kicad_mcp import write_layer as wl
from kicad_mcp.kicad_cli import find_kicad_cli
from kicad_mcp.sourcing.footprint_gen import list_package_types as inner_package_types
from kicad_mcp.toolsets import sourcing as sourcing_toolset

# One real footprint per supported package type, with parameters the tool must pass through.
SPECS: dict[str, dict] = {
    "chip": {"size": "0603", "prefix": "R"},
    "dip": {"pins": 8},
    "pin_header": {"pins": 6, "rows": 2},
    "qfn": {"pins": 16, "body_size": 3.0, "exposed_pad": 1.7},
    "qfp": {"pins": 48, "body_size": 7.0},
    "soic": {"pins": 8},
    "sot": {"variant": "SOT-23-5"},
}
# What the generator makes of SPECS (kicad_tools 0.24.0, recorded from a run): name, pads.
EXPECTED = {
    "chip": ("R_0603_1608Metric", 2),
    "dip": ("DIP-8_W7.62mm_P2.54mm", 8),
    "pin_header": ("PinHeader_2x3_P2.54mm_Vertical", 6),
    "qfn": ("QFN-16_3.0x3.0mm_P0.5mm_EP", 17),
    "qfp": ("LQFP-48_7.0x7.0mm_P0.5mm", 48),
    "soic": ("SOIC-8_3.9x4.9mm_P1.27mm", 8),
    "sot": ("SOT-23-5", 5),
}
BREAKS = {"lf": "\n", "cr": "\r", "crlf": "\r\n"}


@pytest.fixture
def mcp() -> MCPServer:
    server = MCPServer("test")
    sourcing_toolset.build(server)
    return server


async def _call(mcp: MCPServer, name: str, arguments: dict) -> dict:
    result = await mcp.call_tool(name, arguments)
    return json.loads(result.content[0].text)


async def _tool(mcp: MCPServer, name: str):
    return {t.name: t for t in await mcp.list_tools()}[name]


@pytest.mark.asyncio
async def test_a_chip_footprint_is_generated_through_call_tool(mcp: MCPServer, tmp_path: Path) -> None:
    out = tmp_path / "fp.pretty" / "R_0402.kicad_mod"
    result = await _call(
        mcp,
        "generate_footprint",
        {"package_type": "chip", "output_path": str(out), "params": {"size": "0402", "name": "R_0402_1005Metric"}},
    )
    assert result["status"] == "ok", result
    assert result["footprint_name"] == "R_0402_1005Metric"
    assert result["pad_count"] == 2
    assert out.is_file()


@pytest.mark.asyncio
async def test_the_schema_names_the_real_parameters(mcp: MCPServer) -> None:
    """No catch-all ``kwargs``: the package type is an enum of the supported types, and
    ``params`` lists every generator parameter and admits no other name."""
    schema = (await _tool(mcp, "generate_footprint")).input_schema
    assert set(schema["properties"]) == {"package_type", "output_path", "params"}
    assert set(schema["required"]) == {"package_type", "output_path", "params"}
    assert sorted(schema["properties"]["package_type"]["enum"]) == inner_package_types()
    params = schema["$defs"]["FootprintParams"]
    assert params["additionalProperties"] is False
    every_parameter = {p for names in sourcing_toolset.PACKAGE_PARAMETERS.values() for p in names}
    assert set(params["properties"]) == every_parameter
    assert all(prop.get("description") for prop in params["properties"].values())


@pytest.mark.asyncio
async def test_the_descriptions_say_how_to_call_it(mcp: MCPServer) -> None:
    """The description must tell a client where the parameters go (``params``), with an
    example, and where to find each type's parameters; the old one named neither."""
    description = (await _tool(mcp, "generate_footprint")).description
    assert "params" in description
    assert '{"size": "0603"}' in description
    assert "list_package_types" in description
    assert "Every generator parameter goes inside params" in description
    assert "A name inside params that is unknown" in description
    # The SDK drops a top-level argument the schema does not name; the tool refuses it
    # (fix round 2), and the description says so.
    assert "outside params" in description and "refused" in description
    assert "ignored" not in description
    listing = (await _tool(mcp, "list_package_types")).description
    assert "params" in listing


def _first_pad(path: Path) -> tuple[list[float], float]:
    """Raw ``(size W H)`` and ``(drill D)`` of the first pad, read from the S-expression."""
    pad = next(iter(wl.load(path).find_all("pad")))
    return [float(v) for v in pad.find("size").values], float(pad.find("drill").values[0])


# Each default or example a FootprintParams description states, and a generator run through
# call_tool that shows it (review round 1 found four false ones): (field, phrase in the field's
# description, package_type, params, what the run must show).
STATED = [
    ("row_spacing", "7.62 mm up to 22 pins and 15.24 mm above", "dip", {"pins": 22}, {"footprint_name": "DIP-22_W7.62mm_P2.54mm"}),
    ("row_spacing", "7.62 mm up to 22 pins and 15.24 mm above", "dip", {"pins": 24}, {"footprint_name": "DIP-24_W15.24mm_P2.54mm"}),
    ("exposed_pad", "16 pins on 3.0 mm: 1.7 mm", "qfn", {"pins": 16, "body_size": 3.0}, {"pad_count": 17}),
    ("exposed_pad", "none when body_size is omitted", "qfn", {"pins": 16}, {"pad_count": 16}),
    ("body_size", "qfn 4.0 mm", "qfn", {"pins": 16}, {"footprint_name": "QFN-16_4.0x4.0mm_P0.5mm"}),
    ("body_size", "10.0 mm when there is none", "qfp", {"pins": 40}, {"footprint_name": "LQFP-40_10.0x10.0mm_P0.5mm"}),
    ("metric", "'C_1005Metric' instead of 'C_0402_1005Metric'", "chip", {"size": "0402", "prefix": "C", "metric": True},
     {"footprint_name": "C_1005Metric"}),
    ("metric", "'C_1005Metric' instead of 'C_0402_1005Metric'", "chip", {"size": "0402", "prefix": "C"},
     {"footprint_name": "C_0402_1005Metric"}),
    ("pitch", "pin_header 2.54", "pin_header", {"pins": 4}, {"footprint_name": "PinHeader_1x4_P2.54mm_Vertical"}),
    ("rows", "(default 1)", "pin_header", {"pins": 4}, {"footprint_name": "PinHeader_1x4_P2.54mm_Vertical"}),
    ("pad_diameter", "dip 1.6 mm, pin_header 1.7 mm", "dip", {"pins": 8}, {"pad": ([1.6, 1.6], 0.8)}),
    ("drill", "dip 0.8 mm, pin_header 1.0 mm", "pin_header", {"pins": 4}, {"pad": ([1.7, 1.7], 1.0)}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "phrase", "package_type", "params", "expected"), STATED,
                         ids=[f"{f}-{i}" for i, (f, *_rest) in enumerate(STATED)])
async def test_each_stated_default_holds_against_the_generator(
    mcp: MCPServer, tmp_path: Path, field: str, phrase: str, package_type: str, params: dict, expected: dict
) -> None:
    description = sourcing_toolset.FootprintParams.model_fields[field].description
    assert phrase in description, description
    out = tmp_path / "x.kicad_mod"
    result = await _call(mcp, "generate_footprint", {"package_type": package_type, "output_path": str(out), "params": params})
    assert result["status"] == "ok", result
    for key, value in expected.items():
        if key == "pad":
            assert _first_pad(out) == value
        else:
            assert result[key] == value, result


def test_the_listed_sizes_and_variants_are_the_generators() -> None:
    from kicad_tools.library.generators.standards import CHIP_SIZES, SOT_STANDARDS

    fields = sourcing_toolset.FootprintParams.model_fields
    assert ", ".join(CHIP_SIZES) in fields["size"].description
    assert ", ".join(SOT_STANDARDS) in fields["variant"].description


def test_the_parameter_table_matches_the_kicad_tools_generators() -> None:
    """The table is written out by hand so the schema can name it; this keeps it honest
    against the generators' real signatures (a kicad_tools upgrade that changes one fails
    here, not in a client's call)."""
    assert sorted(sourcing_toolset.PACKAGE_PARAMETERS) == inner_package_types()
    assert sorted(typing.get_args(sourcing_toolset.PackageType)) == inner_package_types()
    for package_type, names in sourcing_toolset.PACKAGE_PARAMETERS.items():
        generator = getattr(kt_generators, f"create_{package_type}")
        signature = inspect.signature(generator).parameters
        assert set(names) == set(signature), package_type
        required = {n for n, p in signature.items() if p.default is inspect.Parameter.empty}
        assert set(sourcing_toolset.REQUIRED_PACKAGE_PARAMETERS[package_type]) == required, package_type


@pytest.mark.asyncio
async def test_an_unknown_parameter_is_refused_by_name(mcp: MCPServer, tmp_path: Path) -> None:
    out = tmp_path / "x.kicad_mod"
    with pytest.raises(ToolError, match="pad_with"):
        await _call(
            mcp, "generate_footprint",
            {"package_type": "soic", "output_path": str(out), "params": {"pins": 8, "pad_with": 0.6}},
        )
    assert not out.exists()


@pytest.mark.asyncio
async def test_a_parameter_the_package_type_does_not_take_is_refused_by_name(mcp: MCPServer, tmp_path: Path) -> None:
    out = tmp_path / "x.kicad_mod"
    result = await _call(
        mcp, "generate_footprint",
        {"package_type": "soic", "output_path": str(out), "params": {"pins": 8, "size": "0603", "rows": 2}},
    )
    assert result["status"] == "invalid_params", result
    assert "rows, size" in result["reason"]
    assert "pad_width" in result["reason"]  # names what soic does take
    assert not out.exists()


@pytest.mark.asyncio
async def test_a_missing_required_parameter_is_named(mcp: MCPServer, tmp_path: Path) -> None:
    out = tmp_path / "x.kicad_mod"
    result = await _call(
        mcp, "generate_footprint", {"package_type": "qfn", "output_path": str(out), "params": {"body_size": 3.0}}
    )
    assert result["status"] == "invalid_params", result
    assert "needs pins" in result["reason"]
    assert not out.exists()


# Combinations where kicad_tools 0.24.0 takes one parameter and silently drops the others
# (review round 1: sot variant over pins/pitch/body, qfp body_size over body_width/length; chip
# name over metric, found reading create_chip). Each must be refused, naming the fields.
OVERRIDDEN = [
    ("sot", {"variant": "SOT-23", "pitch": 2.0, "pins": 5}, "variant", "pins, pitch"),
    ("sot", {"variant": "SOT-23", "body_width": 2.0, "body_length": 3.0}, "variant", "body_length, body_width"),
    ("qfp", {"pins": 48, "body_size": 7.0, "body_width": 12.0, "body_length": 20.0}, "body_size", "body_length, body_width"),
    ("qfp", {"pins": 48, "body_size": 7.0, "body_length": 20.0}, "body_size", "body_length"),
    ("chip", {"size": "0402", "metric": True, "name": "MY"}, "name", "metric"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("package_type", "params", "winner", "dropped"), OVERRIDDEN, ids=[f"{p}-{w}" for p, _, w, _ in OVERRIDDEN])
async def test_a_parameter_the_generator_would_drop_is_refused_by_name(
    mcp: MCPServer, tmp_path: Path, package_type: str, params: dict, winner: str, dropped: str
) -> None:
    out = tmp_path / "x.kicad_mod"
    result = await _call(mcp, "generate_footprint", {"package_type": package_type, "output_path": str(out), "params": params})
    assert result["status"] == "invalid_params", result
    assert f"with {winner}, {package_type} ignores {dropped}" in result["reason"], result
    assert not out.exists()


@pytest.mark.asyncio
async def test_a_custom_sot_needs_all_four_dimensions(mcp: MCPServer, tmp_path: Path) -> None:
    """Without ``variant`` create_sot needs pins, pitch, body_width and body_length."""
    out = tmp_path / "x.kicad_mod"
    result = await _call(
        mcp, "generate_footprint", {"package_type": "sot", "output_path": str(out), "params": {"pins": 5, "pitch": 0.95}}
    )
    assert result["status"] == "invalid_params", result
    assert "without variant, sot needs body_length, body_width" in result["reason"], result
    assert not out.exists()
    result = await _call(
        mcp, "generate_footprint",
        {"package_type": "sot", "output_path": str(out),
         "params": {"pins": 3, "pitch": 0.95, "body_width": 1.6, "body_length": 2.9}},
    )
    assert (result["status"], result["pad_count"], result["footprint_name"]) == ("ok", 3, "SOT-3_1.6x2.9mm"), result


@pytest.mark.asyncio
async def test_a_custom_sot_has_three_pins_as_the_description_says(mcp: MCPServer, tmp_path: Path) -> None:
    """kicad_tools 0.24.0 has a custom SOT layout for 3 pins only; the pins description says so
    and any other count comes back as the generator's refusal, with nothing written."""
    assert "sot without variant: 3 only" in sourcing_toolset.FootprintParams.model_fields["pins"].description
    out = tmp_path / "x.kicad_mod"
    result = await _call(
        mcp, "generate_footprint",
        {"package_type": "sot", "output_path": str(out),
         "params": {"pins": 5, "pitch": 0.95, "body_width": 1.6, "body_length": 2.9}},
    )
    assert result["status"] == "incomplete", result
    assert "not implemented for 5 pins" in result["reason"]
    assert not out.exists()


@pytest.mark.asyncio
async def test_an_unsupported_package_type_is_refused(mcp: MCPServer, tmp_path: Path) -> None:
    out = tmp_path / "x.kicad_mod"
    with pytest.raises(ToolError, match="package_type"):
        await _call(mcp, "generate_footprint", {"package_type": "bga", "output_path": str(out), "params": {}})
    assert not out.exists()


# Every string parameter that reaches the .kicad_mod, with a package type that takes it.
STRING_PARAMETERS = [
    ("chip", {"size": "0603"}, "name"),
    ("chip", {"size": "0603"}, "prefix"),
    ("chip", {}, "size"),
    ("sot", {}, "variant"),
    ("soic", {"pins": 8}, "name"),
]


# Each character kicad_tools' Footprint.to_sexp writes unescaped, with the reason's opening.
# A '"' ends the quoted string early (kicad-cli fp upgrade / fp export svg exit 2 on the
# library, review round 1); KiCad's reader takes '\' as an escape, so a literal "\n" in a name
# would come back as a line break.
UNWRITABLE = {
    **{name: (brk, "line break in {field}:") for name, brk in BREAKS.items()},
    "quote": ('"', "'\"' or '\\' in {field}:"),
    "backslash": ("\\", "'\"' or '\\' in {field}:"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("char", UNWRITABLE, ids=list(UNWRITABLE))
@pytest.mark.parametrize(("package_type", "base", "field"), STRING_PARAMETERS, ids=[f"{p}-{f}" for p, _, f in STRING_PARAMETERS])
async def test_an_unwritable_character_in_any_string_parameter_is_refused_through_call_tool(
    mcp: MCPServer, tmp_path: Path, package_type: str, base: dict, field: str, char: str
) -> None:
    """Build ticket 25's guard in ``footprint_gen`` must see the explicit parameters: before
    this ticket it saw one dict and let it pass. Review round 1 added '"' and '\\'."""
    assert field in sourcing_toolset.PACKAGE_PARAMETERS[package_type]
    text, opening = UNWRITABLE[char]
    out = tmp_path / "x.kicad_mod"
    params = {**base, field: f"A{text}B"}
    result = await _call(mcp, "generate_footprint", {"package_type": package_type, "output_path": str(out), "params": params})
    assert result["status"] == "incomplete", result
    assert result["reason"].startswith(opening.format(field=field)), result
    assert not out.exists()


def test_every_string_parameter_and_character_is_covered_by_the_refusal_test() -> None:
    string_fields = {
        name for name, info in sourcing_toolset.FootprintParams.model_fields.items() if str in typing.get_args(info.annotation)
    }
    assert string_fields == {field for _, _, field in STRING_PARAMETERS}
    assert {"\n", "\r", '"', "\\"} <= {text for text, _ in UNWRITABLE.values()}


@pytest.mark.asyncio
async def test_list_package_types_names_each_types_parameters(mcp: MCPServer) -> None:
    result = await _call(mcp, "list_package_types", {})
    assert result["package_types"] == inner_package_types()
    assert result["parameters"]["soic"]["required"] == ["pins"]
    assert result["parameters"]["chip"]["accepted"] == ["size", "prefix", "metric", "name"]
    assert result["parameters"]["sot"]["required"] == []


@pytest.mark.asyncio
async def test_kicad_loads_one_generated_footprint_per_package_type(mcp: MCPServer, tmp_path: Path) -> None:
    """KiCad itself is the oracle. ``kicad-cli fp export svg`` exits 0 even when it cannot
    find the footprint or write the file, so the SVG's existence is the check. ``kicad-cli fp
    upgrade --force`` re-saves the library through KiCad's parser; the pads it wrote back,
    counted on the raw ``(pad ...)`` nodes, must be every pad the tool reported."""
    assert set(SPECS) == set(inner_package_types())
    lib = tmp_path / "gen.pretty"
    for package_type, params in SPECS.items():
        result = await _call(
            mcp, "generate_footprint",
            {"package_type": package_type, "output_path": str(lib / f"{package_type}.kicad_mod"), "params": params},
        )
        assert result["status"] == "ok", (package_type, result)
        assert (result["footprint_name"], result["pad_count"]) == EXPECTED[package_type]

    cli = str(find_kicad_cli())
    svg_dir = tmp_path / "svg"
    svg_dir.mkdir()
    for package_type in SPECS:
        subprocess.run(
            [cli, "fp", "export", "svg", str(lib), "--fp", package_type, "-o", str(svg_dir)],
            capture_output=True, text=True, timeout=120, check=True,
        )
        svg = svg_dir / f"{package_type}.svg"
        assert svg.is_file(), package_type
        assert "<svg" in svg.read_text(encoding="utf-8"), package_type

    upgraded = tmp_path / "upgraded.pretty"
    subprocess.run(
        [cli, "fp", "upgrade", "--force", str(lib), "-o", str(upgraded)],
        capture_output=True, text=True, timeout=120, check=True,
    )
    for package_type in SPECS:
        root = wl.load(upgraded / f"{package_type}.kicad_mod")
        assert root.name == "footprint"
        assert len(list(root.find_all("pad"))) == EXPECTED[package_type][1], package_type


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ["prefix", "name", "pad_with"])
async def test_a_top_level_generator_parameter_is_refused_by_name_and_nothing_is_written(
    mcp: MCPServer, tmp_path: Path, extra: str
) -> None:
    """Fix round 2: the SDK's argument model ignores extras, so a caller's top-level
    ``prefix`` used to vanish and the call returned ok."""
    out = tmp_path / "fp.pretty" / "R.kicad_mod"
    arguments = {"package_type": "chip", "output_path": str(out), "params": {"size": "0603"}, extra: "X"}
    with pytest.raises(ToolError, match=extra) as caught:
        await mcp.call_tool("generate_footprint", arguments)
    assert "params" in str(caught.value)
    assert not out.exists() and not out.parent.exists()


@pytest.mark.asyncio
async def test_every_unknown_top_level_argument_is_named(mcp: MCPServer, tmp_path: Path) -> None:
    out = tmp_path / "R.kicad_mod"
    arguments = {"package_type": "chip", "output_path": str(out), "params": {"size": "0603"}, "prefix": 1, "name": 2}
    with pytest.raises(ToolError) as caught:
        await mcp.call_tool("generate_footprint", arguments)
    assert "prefix" in str(caught.value) and "name" in str(caught.value)
    assert not out.exists()


@pytest.mark.asyncio
async def test_package_type_output_path_and_params_are_still_accepted(mcp: MCPServer, tmp_path: Path) -> None:
    out = tmp_path / "R.kicad_mod"
    result = await _call(
        mcp, "generate_footprint", {"package_type": "chip", "output_path": str(out), "params": {"size": "0603"}}
    )
    assert result["status"] == "ok", result
    assert out.is_file()
