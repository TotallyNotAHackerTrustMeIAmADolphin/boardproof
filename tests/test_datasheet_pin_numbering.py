"""Issue #39: a pin-numbering answer for a package variant, or an explicit
"not machine-extracted" status plus a rendered image, never a guess.

The PDFs are built here (a minimal hand-assembled PDF: text plus ruled
lines), not vendor files: they pin the contract, not a vendor's layout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.sourcing.datasheet import inspect_datasheet


def _pdf(path: Path, pages: list[str]) -> Path:
    """Write a minimal multi-page PDF; each page is a raw content stream."""
    objs: list[bytes] = []
    n_pages = len(pages)
    kids = " ".join(f"{4 + i * 2} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, content in enumerate(pages):
        data = content.encode()
        objs.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (5 + i * 2)
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def _text(x: int, y: int, s: str, size: int = 10) -> str:
    return f"BT /F1 {size} Tf {x} {y} Td ({s}) Tj ET\n"


def _grid(x0: int, y_top: int, col_w: list[int], rows: list[list[str]], row_h: int = 20) -> str:
    out = ""
    width = sum(col_w)
    height = row_h * len(rows)
    for r in range(len(rows) + 1):
        y = y_top - r * row_h
        out += f"{x0} {y} m {x0 + width} {y} l S\n"
    xs = [x0]
    for w in col_w:
        xs.append(xs[-1] + w)
    for x in xs:
        out += f"{x} {y_top} m {x} {y_top - height} l S\n"
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            out += _text(xs[c] + 4, y_top - r * row_h - 14, cell)
    return out


def _pin_page(package: str, rows: list[list[str]]) -> str:
    return _text(50, 750, f"{package} package pin configuration", 14) + _grid(50, 720, [60, 100, 200], rows)


PIN_ROWS = [["Pin No.", "Name", "Function"], ["1", "REF", "Reference input"], ["2", "CATHODE", "Cathode"], ["3", "ANODE", "Anode"]]


def test_pin_table_with_package_heading_is_machine_extracted(tmp_path):
    pdf = _pdf(tmp_path / "a.pdf", [_pin_page("SOT-23", PIN_ROWS)])
    result = inspect_datasheet(pdf, package="SOT-23")
    pn = result["pin_numbering"]
    assert pn["machine_extracted"] is True
    assert pn["package"] == "SOT-23"
    assert pn["pins"] == [
        {"pin": "1", "name": "REF", "function": "Reference input"},
        {"pin": "2", "name": "CATHODE", "function": "Cathode"},
        {"pin": "3", "name": "ANODE", "function": "Anode"},
    ]
    assert pn["page"] == 1


def test_two_packages_with_different_orders_are_not_mixed(tmp_path):
    other = [["Pin No.", "Name", "Function"], ["1", "CATHODE", "c"], ["2", "ANODE", "a"], ["3", "REF", "r"]]
    pdf = _pdf(tmp_path / "b.pdf", [_pin_page("SOT-23", PIN_ROWS), _pin_page("SOT-89", other)])
    a = inspect_datasheet(pdf, package="SOT-89")["pin_numbering"]
    assert a["machine_extracted"] is True
    assert [p["name"] for p in a["pins"]] == ["CATHODE", "ANODE", "REF"]
    assert a["page"] == 2


def test_diagram_only_page_is_not_extracted_and_yields_an_image(tmp_path):
    # the package is named, the pins are a drawing: no table text at all
    page = _text(50, 750, "SOT-23 package pin configuration", 14) + "100 400 200 100 re S\n"
    pdf = _pdf(tmp_path / "c.pdf", [page])
    out = tmp_path / "img"
    result = inspect_datasheet(pdf, package="SOT-23", image_dir=out)
    pn = result["pin_numbering"]
    assert pn["machine_extracted"] is False
    assert pn["pins"] == []
    assert "NOT machine-extracted" in pn["reason"]
    assert len(pn["page_images"]) == 1
    img = Path(pn["page_images"][0]["path"])
    assert img.is_file() and img.read_bytes().startswith(b"\x89PNG")
    assert pn["page_images"][0]["page"] == 1


def test_package_not_on_any_page_is_not_extracted(tmp_path):
    pdf = _pdf(tmp_path / "d.pdf", [_pin_page("SOT-23", PIN_ROWS)])
    pn = inspect_datasheet(pdf, package="DPAK", image_dir=tmp_path / "i")["pin_numbering"]
    assert pn["machine_extracted"] is False
    assert pn["pins"] == []


def test_table_with_package_only_elsewhere_on_page_is_not_trusted(tmp_path):
    # package named on the page, but not in the heading above the table
    page = _text(50, 780, "Ordering: SOT-23 and SOT-89", 12) + _text(50, 750, "Pin description", 14) + _grid(
        50, 720, [60, 100, 200], PIN_ROWS
    )
    pdf = _pdf(tmp_path / "e.pdf", [page])
    pn = inspect_datasheet(pdf, package="SOT-23")["pin_numbering"]
    assert pn["machine_extracted"] is False


def test_no_package_given_reports_unscoped_table_as_not_extracted_for_a_variant(tmp_path):
    pdf = _pdf(tmp_path / "f.pdf", [_pin_page("SOT-23", PIN_ROWS)])
    result = inspect_datasheet(pdf)
    assert "pin_numbering" not in result  # old callers get the old shape


def test_never_invents_pins_from_a_malformed_table(tmp_path):
    rows = [["Pin No.", "Name", "Function"], ["one", "REF", "x"], ["", "", ""]]
    pdf = _pdf(tmp_path / "g.pdf", [_pin_page("SOT-23", rows)])
    pn = inspect_datasheet(pdf, package="SOT-23")["pin_numbering"]
    assert pn["machine_extracted"] is False
    assert pn["pins"] == []


# ---- review round 1: B1, B2, B3, F1, F3, F4, F5 ----------------------------------------------

def _ext(pdf, package, **kw):
    return inspect_datasheet(pdf, package=package, **kw)["pin_numbering"]


@pytest.mark.parametrize(
    "heading,package",
    [
        ("SOT-23, SOT-89 package pin configuration", "SOT-23"),
        ("SOT-23, SOT-89 package pin configuration", "SOT-89"),
        ("Not for SOT-23 use:", "SOT-23"),
        ("SOT-23 package pin configuration", "23"),
        ("SOT-23-5 package pin configuration", "SOT-23"),
    ],
)
def test_b1_heading_naming_other_packages_or_a_longer_token_is_unverified(tmp_path, heading, package):
    page = _text(50, 750, heading, 14) + _grid(50, 720, [60, 100, 200], PIN_ROWS)
    assert _ext(_pdf(tmp_path / "b1.pdf", [page]), package)["machine_extracted"] is False


def test_b2_table_continued_on_next_page_is_unverified(tmp_path):
    first = [["Pin No.", "Name", "Function"], ["1", "REF", "r"], ["2", "CATHODE", "c"]]
    cont = _text(50, 770, "CJ431 datasheet", 8) + _grid(50, 740, [60, 100, 200], [["3", "ANODE", "a"]])
    pdf = _pdf(tmp_path / "b2.pdf", [_pin_page("SOT-23", first), cont])
    pn = _ext(pdf, "SOT-23")
    assert pn["machine_extracted"] is False and pn["pins"] == []


def test_b2_table_with_a_gap_is_unverified(tmp_path):
    rows = [["Pin No.", "Name", "Function"], ["1", "REF", "r"], ["3", "ANODE", "a"]]
    assert _ext(_pdf(tmp_path / "gap.pdf", [_pin_page("SOT-23", rows)]), "SOT-23")["machine_extracted"] is False


def test_b3_different_tables_under_headings_naming_one_package_are_unverified(tmp_path):
    other = [["Pin No.", "Name", "Function"], ["1", "CATHODE", "c"], ["2", "ANODE", "a"], ["3", "REF", "r"]]
    pdf = _pdf(tmp_path / "two.pdf", [_pin_page("SOT-23", PIN_ROWS), _pin_page("SOT-23", other)])
    assert _ext(pdf, "SOT-23")["machine_extracted"] is False


def test_b3_unrecognised_header_is_unverified(tmp_path):
    rows = [["A", "B", "C"], ["1", "REF", "r"], ["2", "CATHODE", "c"]]
    assert _ext(_pdf(tmp_path / "hdr.pdf", [_pin_page("SOT-23", rows)]), "SOT-23")["machine_extracted"] is False


def test_b3_duplicate_pin_id_is_unverified(tmp_path):
    rows = [["Pin No.", "Name", "Function"], ["A1", "REF", "r"], ["A1", "ANODE", "a"]]
    assert _ext(_pdf(tmp_path / "dup.pdf", [_pin_page("SOT-23", rows)]), "SOT-23")["machine_extracted"] is False


def _image_page(side: int) -> str:
    hexdata = "ff0000 00ff00 0000ff ffffff>"
    return (
        _text(50, 780, "SOT-23 package pin configuration", 14)
        + f"q {side} 0 0 {side} 100 100 cm BI /W 2 /H 2 /CS /RGB /BPC 8 /F /AHx ID {hexdata} EI Q\n"
    )


def _png_size(path):
    data = Path(path).read_bytes()
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def test_b3_large_embedded_image_is_cropped_to_the_image(tmp_path):
    pn = _ext(_pdf(tmp_path / "img.pdf", [_image_page(400)]), "SOT-23", image_dir=tmp_path / "o")
    im = pn["page_images"][0]
    assert im["crop"] == "images"
    w, h = _png_size(im["path"])
    assert w < 1250 and h < 1667


def test_f3_tiny_embedded_image_gives_the_full_page(tmp_path):
    pn = _ext(_pdf(tmp_path / "tiny.pdf", [_image_page(20)]), "SOT-23", image_dir=tmp_path / "o")
    im = pn["page_images"][0]
    assert im["crop"] == "full_page"
    assert _png_size(im["path"]) == (1250, 1667)


@pytest.mark.parametrize("package", ["", "   ", "-", " - _ "])
def test_f4_blank_package_is_an_explicit_unverified_result(tmp_path, package):
    pdf = _pdf(tmp_path / "f4.pdf", [_pin_page("SOT-23", PIN_ROWS)])
    pn = _ext(pdf, package)
    assert pn["machine_extracted"] is False and pn["pins"] == []
    assert "package" in pn["reason"]


def test_f5_pin_name_with_newline_or_control_char_is_unverified(tmp_path):
    for bad in ("ANODE\nCATHODE", "AN\x07ODE"):
        rows = [["Pin No.", "Name", "Function"], ["1", "REF", "r"], ["2", bad, "a"]]
        pdf = _pdf(tmp_path / "f5.pdf", [_pin_page("SOT-23", rows)])
        assert _ext(pdf, "SOT-23")["machine_extracted"] is False


def test_f1_registered_tool_passes_package_and_image_dir_and_describes_the_result(tmp_path):
    from mcp.server.mcpserver import MCPServer

    from kicad_mcp.toolsets import sourcing as sourcing_toolset

    mcp = MCPServer("t")
    sourcing_toolset.build(mcp)
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "inspect_datasheet")  # type: ignore[attr-defined]
    for word in ("pin_numbering", "machine_extracted", "UNVERIFIED"):
        assert word in tool.description
    pdf = _pdf(tmp_path / "t.pdf", [_text(50, 750, "SOT-23 package pin configuration", 14)])
    out = tool.fn(str(pdf), package="SOT-23", image_dir=str(tmp_path / "img"))
    assert out["pin_numbering"]["machine_extracted"] is False
    assert Path(out["pin_numbering"]["page_images"][0]["path"]).is_file()


# ---- review round 2: B-N1, F-N3 -----------------------------------------------------------------

_FIRST_TWO = [["Pin No.", "Name", "Function"], ["1", "REF", "r"], ["2", "CATHODE", "c"]]


@pytest.mark.parametrize(
    "heading,rows",
    [
        ("Pin functions (continued)", [["3", "ANODE", "a"]]),
        ("Table 1. Pin Functions (continued)", [["3", "ANODE", "a"]]),
        ("Pin Configuration, continued", [["3", "ANODE", "a"]]),
        ("SOT-23 pin configuration (continued)", [["3", "ANODE", "a"]]),
        ("Pin functions", [["Pin No.", "Name", "Function"], ["3", "ANODE", "a"]]),
        ("Pin Functions Pins 1 to 8 (continued)", [["3", "ANODE", "a"]]),
        ("Pin Functions, Pins 3 to 6", [["3", "ANODE", "a"]]),
        ("Pin Functions (continued) Revision 2 to 3", [["3", "ANODE", "a"]]),
        ("Pin Functions (continued) Rev-2", [["3", "ANODE", "a"]]),
        ("LM-317 Pin Functions (continued)", [["3", "ANODE", "a"]]),
        ("Rev-2 pin functions (continued)", [["3", "ANODE", "a"]]),
        ("Pins 1 to 8 (continued)", [["3", "ANODE", "a"]]),
        ("LM-317 pin functions (continued)", [["3", "ANODE", "a"]]),
        ("PINS 1 TO 8 (CONTINUED)", [["3", "ANODE", "a"]]),
        ("sot-89 pin functions (continued)", [["3", "ANODE", "a"]]),
    ],
)
def test_b2_table_continued_under_pin_heading_is_unverified(tmp_path, heading, rows):
    cont = _text(50, 770, heading, 12) + _grid(50, 740, [60, 100, 200], rows)
    pdf = _pdf(tmp_path / "cont.pdf", [_pin_page("SOT-23", _FIRST_TWO), cont])
    pn = _ext(pdf, "SOT-23")
    assert pn["machine_extracted"] is False and pn["pins"] == []


def test_next_page_headerless_grid_with_no_text_above_is_unverified(tmp_path):
    cont = _grid(50, 780, [60, 100, 200], [["3", "ANODE", "a"]])
    pdf = _pdf(tmp_path / "nohead.pdf", [_pin_page("SOT-23", _FIRST_TWO), cont])
    assert _ext(pdf, "SOT-23")["machine_extracted"] is False


# Round 4 (fail closed, no "other package" exception): ANY pin-like table on the next page makes
# the first table unverified, whatever its heading says. Recognising other packages is a heuristic
# that failed open on "SC431 Pin Functions (continued)" and similar; it is gone.
@pytest.mark.parametrize(
    "heading",
    [
        "SC431 Pin Functions (continued)",
        "SC1117 pin functions (continued)",
        "Pin Functions (continued) SC70 compatible",
        "DBV (SOT-23-5L) pin functions",
        "TO-220 pin configuration",
        "SOIC-8 pin functions",
        "QFN-16 pin functions",
        "SOT89 pin functions",
        "D2PAK pin functions",
    ],
)
def test_round4_next_page_pin_table_under_any_heading_is_unverified(tmp_path, heading):
    cont = _text(50, 770, heading, 12) + _grid(50, 740, [60, 100, 200], [["3", "X", "x"]])
    pdf = _pdf(tmp_path / "r4.pdf", [_pin_page("SOT-23", _FIRST_TWO), cont])
    pn = _ext(pdf, "SOT-23")
    assert pn["machine_extracted"] is False and pn["pins"] == []
    assert "may continue" in pn["reason"]


def test_round4_plain_repeated_header_continuation_is_unverified(tmp_path):
    cont = _grid(50, 740, [60, 100, 200], [["Pin No.", "Name", "Function"], ["3", "ANODE", "a"]])
    pdf = _pdf(tmp_path / "r4plain.pdf", [_pin_page("SOT-23", _FIRST_TWO), cont])
    assert _ext(pdf, "SOT-23")["machine_extracted"] is False


def test_next_page_without_any_table_does_not_block(tmp_path):
    cont = _text(50, 770, "Applications information", 12)
    pdf = _pdf(tmp_path / "notable.pdf", [_pin_page("SOT-23", _FIRST_TWO), cont])
    assert _ext(pdf, "SOT-23")["machine_extracted"] is True


def test_next_page_table_of_empty_cells_does_not_block(tmp_path):
    cont = _text(50, 770, "Pin functions", 12) + _grid(50, 740, [60, 100, 200], [["", "", ""], ["", "", ""]])
    pdf = _pdf(tmp_path / "emptycells.pdf", [_pin_page("SOT-23", _FIRST_TWO), cont])
    assert _ext(pdf, "SOT-23")["machine_extracted"] is True


def test_only_the_line_directly_above_the_next_table_is_its_heading(tmp_path):
    cont = (
        _text(50, 780, "SOT-89 package", 12)
        + _text(50, 765, "Pin functions (continued)", 12)
        + _grid(50, 740, [60, 100, 200], [["3", "ANODE", "a"]])
    )
    pdf = _pdf(tmp_path / "twolines.pdf", [_pin_page("SOT-23", _FIRST_TWO), cont])
    assert _ext(pdf, "SOT-23")["machine_extracted"] is False


def test_next_page_table_under_a_different_package_heading_is_unverified(tmp_path):
    other = [["Pin No.", "Name", "Function"], ["1", "A", "a"], ["2", "B", "b"]]
    pdf = _pdf(tmp_path / "sep.pdf", [_pin_page("SOT-23", _FIRST_TWO), _pin_page("SOT-89", other)])
    pn = _ext(pdf, "SOT-23")
    assert pn["machine_extracted"] is False and pn["pins"] == []


def test_next_page_non_pin_table_does_not_block(tmp_path):
    other = [["Parameter", "Value"], ["Vin", "5"]]
    cont = _text(50, 770, "Electrical characteristics", 12) + _grid(50, 740, [100, 100], other)
    pdf = _pdf(tmp_path / "nonpin.pdf", [_pin_page("SOT-23", _FIRST_TWO), cont])
    assert _ext(pdf, "SOT-23")["machine_extracted"] is True


def test_f3_blank_package_guard_with_a_pin_configuration_heading(tmp_path):
    page = _text(50, 750, "Pin configuration", 14) + _grid(50, 720, [60, 100, 200], PIN_ROWS)
    pn = _ext(_pdf(tmp_path / "blank.pdf", [page]), "-")
    assert pn["machine_extracted"] is False and "blank" in pn["reason"]


def test_f3_heading_with_trailing_status_words_is_unverified(tmp_path):
    page = _text(50, 750, "SOT-23 not for new designs", 14) + _grid(50, 720, [60, 100, 200], PIN_ROWS)
    assert _ext(_pdf(tmp_path / "nrnd.pdf", [page]), "SOT-23")["machine_extracted"] is False


# Round 5: every table on the next page is tested, and every one of its first rows, so neither an
# ordering table above the pin table nor a title row above the header hides it.
def _two_page(tmp_path, name, next_page):
    return _pdf(tmp_path / name, [_pin_page("SOT-23", _FIRST_TWO), next_page])


def test_next_page_pin_table_not_first_on_page_is_unverified(tmp_path):
    cont = _grid(50, 760, [60, 100, 200], [["Ordering", "Part", "Pkg"], ["a", "b", "c"]]) + _grid(
        50, 650, [60, 100, 200], [["Pin No.", "Name", "Function"], ["3", "X", "x"]]
    )
    pn = _ext(_two_page(tmp_path, "notfirst.pdf", cont), "SOT-23")
    assert pn["machine_extracted"] is False and pn["pins"] == []
    assert "may continue" in pn["reason"]


def test_next_page_pin_table_behind_a_title_row_is_unverified(tmp_path):
    rows = [["Pin Functions", "", ""], ["Pin No.", "Name", "Function"], ["3", "X", "x"]]
    cont = _grid(50, 760, [60, 100, 200], rows)
    assert _ext(_two_page(tmp_path, "title.pdf", cont), "SOT-23")["machine_extracted"] is False


@pytest.mark.parametrize(
    "first_row",
    [
        ["a3", "X", "x"],
        ["3A", "X", "x"],
        ["A1B", "X", "x"],
        ["EP", "X", "x"],
        ["3.", "X", "x"],
        ["1234", "X", "x"],
        ["", "5", "X"],
        ["Pin Name", "Function", "x"],
        ["Pins", "Function", "x"],
        ["Pin/Pad", "Function", "x"],
        ["Terminal", "Function", "x"],
    ],
)
def test_f1_widened_pin_like_shapes_make_the_first_table_unverified(tmp_path, first_row):
    cont = _grid(50, 760, [60, 100, 200], [first_row, ["4", "Y", "y"]])
    assert _ext(_two_page(tmp_path, "wide.pdf", cont), "SOT-23")["machine_extracted"] is False


@pytest.mark.parametrize(
    "rows",
    [
        [["", "", ""], ["", "", ""]],
        [["Ordering", "Part", "Pkg"], ["a", "b", "c"]],
        [["Name", "Function", "x"], ["a", "b", "c"]],
    ],
)
def test_f1_non_pin_tables_on_the_next_page_still_let_the_first_table_stand(tmp_path, rows):
    cont = _grid(50, 760, [60, 100, 200], rows)
    assert _ext(_two_page(tmp_path, "neg.pdf", cont), "SOT-23")["machine_extracted"] is True
