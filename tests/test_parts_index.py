"""The local parts index reader (issue 199): every filter, sorting, paging, refusal and the old-index flag, on
the small hand-built fixture index (tests/_parts_index_fixture.py). No network, no real data."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from boardproof.sourcing import parts_index as pi
from boardproof.sourcing.models import PartResult
from tests._parts_index_fixture import FIXTURE_DATE, build_fixture

OHM = "Ω"
MICRO = "µ"


@pytest.fixture
def idx(tmp_path, monkeypatch):
    monkeypatch.delenv(pi.ENV_VAR, raising=False)
    monkeypatch.setattr(pi, "_now", lambda: FIXTURE_DATE + timedelta(days=1))
    return str(build_fixture(tmp_path / "cache.sqlite3"))


def ids(result) -> list[int]:
    return [int(i["part"]["lcsc_id"][1:]) for i in result["items"]]


def run(idx, **kw):
    kw.setdefault("limit", pi.MAX_LIMIT)
    return pi.filter_parts(idx, **kw)


def refusal(fn, *a, **kw) -> dict:
    with pytest.raises(pi.IndexRefusal) as err:
        fn(*a, **kw)
    return err.value.as_result()


# ------------------------------------------------------------------ quantities

@pytest.mark.parametrize("text,expected", [
    ("10k" + OHM, (10000.0, OHM)), ("4.7k" + OHM, (4700.0, OHM)), ("100nF", (1e-7, "F")),
    ("10" + MICRO + "F", (1e-5, "F")), ("10μF", (1e-5, "F")), ("22pF", (2.2e-11, "F")), ("50V", (50.0, "V")),
    ("62.5mW", (0.0625, "W")), ("1M" + OHM, (1e6, OHM)), ("10K", (10000.0, "")), ("±1%", (1.0, "%")),
    ("1mH", (1e-3, "H")), ("3A", (3.0, "A")), ("200mA", (0.2, "A")), ("2.2k ohm", (2200.0, OHM)),
    ("0" + OHM, (0.0, OHM)), ("100", (100.0, "")), ("1MHz", (1e6, "Hz")), (5, (5.0, "")), ("10m", (0.01, "")),
])
def test_parse_quantity_reads_si_prefixes_and_units(text, expected):
    value, unit = pi.parse_quantity(text)
    assert unit == expected[1]
    assert value == pytest.approx(expected[0], rel=1e-9)


@pytest.mark.parametrize("text", ["X7R", "", "-55℃~+125℃", "abc", "10 foo", "1mm", None, True, [1], "NaN"])
def test_parse_quantity_refuses_what_is_not_a_quantity(text):
    assert pi.parse_quantity(text) is None


def test_milli_and_mega_are_case_sensitive_and_lone_unit_letters_are_units():
    assert pi.parse_quantity("1m")[0] == pytest.approx(1e-3)
    assert pi.parse_quantity("1M")[0] == pytest.approx(1e6)
    assert pi.parse_quantity("1F") == (1.0, "F")
    assert pi.parse_quantity("1H") == (1.0, "H")


@pytest.mark.parametrize("value,op,wanted,expected", [
    ("10k" + OHM, "eq", "10k", True), ("10k" + OHM, "eq", "10000", True), ("10k" + OHM, "eq", "10k" + OHM, True),
    ("10k" + OHM, "eq", "10kF", False), ("10k" + OHM, "eq", "11k", False), ("10k" + OHM, "ne", "11k", True),
    ("10k" + OHM, "ne", "10k", False),
    ("10k" + OHM, "lt", "11k", True), ("10k" + OHM, "lt", "10k", False), ("10k" + OHM, "le", "10k", True),
    ("10k" + OHM, "gt", "9k", True), ("10k" + OHM, "gt", "10k", False), ("10k" + OHM, "ge", "10k", True),
    ("10k" + OHM, "ge", "11k", False), ("10k" + OHM, "lt", "11kF", False),
    ("X7R", "eq", "x7r", True), ("X7R", "ne", "X5R", True), ("X7R", "eq", "X5R", False), ("X7R", "lt", "10", False),
    ("X7R", "contains", "7", True), ("X7R", "contains", "5", False), ("100nF", "eq", "0.1" + MICRO + "F", True),
    ("±1%", "eq", "1%", True), ("±1%", "le", "1%", True), ("±5%", "le", "1%", False),
])
def test_match_attribute(value, op, wanted, expected):
    assert pi.match_attribute(value, op, wanted) is expected


# ------------------------------------------------------------------ opening

def test_no_index_configured_is_refused_with_how_to_get_it(monkeypatch):
    monkeypatch.delenv(pi.ENV_VAR, raising=False)
    r = refusal(pi.list_filters, None)
    assert r["ok"] is False and r["reason"] == "index_not_configured"
    assert pi.ENV_VAR in r["message"] and "cache.sqlite3" in r["message"] and "jlcparts" in r["message"]
    assert r["how_to_get"] == pi.HOW_TO_GET and r["env_var"] == pi.ENV_VAR


def test_missing_file_is_refused(tmp_path):
    r = refusal(pi.filter_parts, str(tmp_path / "nope.sqlite3"))
    assert r["reason"] == "index_not_found" and "jlcparts" in r["message"]


def test_a_directory_is_not_an_index(tmp_path):
    assert refusal(pi.filter_parts, str(tmp_path))["reason"] == "index_not_found"


def test_not_a_sqlite_file_is_refused(tmp_path):
    p = tmp_path / "bad.sqlite3"
    p.write_bytes(b"this is not a database" * 50)
    assert refusal(pi.filter_parts, str(p))["reason"] == "index_unreadable"


def test_env_var_supplies_the_path(idx, monkeypatch):
    monkeypatch.setenv(pi.ENV_VAR, idx)
    assert pi.filter_parts(None, category="Resistors")["total_count"] == 13


def test_argument_wins_over_env_var(idx, tmp_path, monkeypatch):
    monkeypatch.setenv(pi.ENV_VAR, str(tmp_path / "other.sqlite3"))
    assert pi.filter_parts(idx, category="Resistors")["ok"] is True


def test_missing_required_column_is_refused_by_name(tmp_path):
    p = tmp_path / "v.sqlite3"
    conn = sqlite3.connect(p)
    conn.executescript("CREATE TABLE components (lcsc INTEGER); CREATE TABLE categories (id INTEGER, category TEXT, subcategory TEXT);"
                       "CREATE TABLE manufacturers (id INTEGER, name TEXT);")
    conn.commit()
    conn.close()
    r = refusal(pi.filter_parts, str(p))
    assert r["reason"] == "index_schema_unsupported"
    assert "stock" in r["missing"]["components"] and "price" in r["missing"]["components"]


def test_missing_table_is_refused(tmp_path):
    p = tmp_path / "t.sqlite3"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.commit()
    conn.close()
    assert refusal(pi.filter_parts, str(p))["reason"] == "index_schema_unsupported"


def test_reading_never_writes(idx, tmp_path):
    import os
    before = (open(idx, "rb").read(), os.stat(idx).st_mtime_ns)
    pi.list_filters(idx, category="Resistors")
    pi.filter_parts(idx, category="Resistors", attributes=[{"name": "Resistance", "value": "10k"}])
    assert (open(idx, "rb").read(), os.stat(idx).st_mtime_ns) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cache.sqlite3"]


def test_the_connection_is_read_only(idx):
    conn, _ = pi.open_index(idx)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM components")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE x (a)")
    finally:
        conn.close()


def test_the_index_file_can_be_read_only_on_disk(idx):
    import os
    import stat
    os.chmod(idx, stat.S_IREAD)
    try:
        assert pi.filter_parts(idx, category="Resistors")["total_count"] == 13
    finally:
        os.chmod(idx, stat.S_IWRITE | stat.S_IREAD)


# ------------------------------------------------------------------ index date and age

def test_every_answer_states_the_index_date(idx):
    for result in (pi.list_filters(idx), pi.list_filters(idx, category="Resistors"), pi.filter_parts(idx, category="Resistors")):
        assert result["index"]["index_date"] == "2026-11-15"
        assert result["index"]["part_count"] == 34
        assert result["index"]["file"] == "cache.sqlite3"
        assert result["index"]["index_stale"] is False
        assert "stale_note" not in result["index"]


def test_index_older_than_seven_days_is_flagged(idx, monkeypatch):
    monkeypatch.setattr(pi, "_now", lambda: FIXTURE_DATE + timedelta(days=7, seconds=1))
    for result in (pi.list_filters(idx), pi.filter_parts(idx, category="Resistors")):
        assert result["index"]["index_stale"] is True
        assert "days old" in result["index"]["stale_note"] and "7" in result["index"]["stale_note"]
    assert result["ok"] is True  # flagged, not refused


def test_exactly_seven_days_is_not_flagged(idx, monkeypatch):
    monkeypatch.setattr(pi, "_now", lambda: FIXTURE_DATE + timedelta(days=7))
    assert pi.list_filters(idx)["index"]["index_stale"] is False


def test_index_date_is_the_newest_part_update(tmp_path, monkeypatch):
    p = build_fixture(tmp_path / "c.sqlite3", newest=int(datetime(2026, 1, 3, tzinfo=timezone.utc).timestamp()))
    monkeypatch.setattr(pi, "_now", lambda: datetime(2026, 1, 4, tzinfo=timezone.utc))
    assert pi.list_filters(str(p))["index"]["index_date"] == "2026-01-03"


def test_an_index_without_update_times_has_an_unknown_age_and_is_flagged(tmp_path):
    p = build_fixture(tmp_path / "c.sqlite3")
    conn = sqlite3.connect(p)
    conn.execute("UPDATE components SET last_update = 0")
    conn.commit()
    conn.close()
    info = pi.list_filters(str(p))["index"]
    assert info["index_date"] is None and info["index_stale"] is True and "unknown" in info["date_note"]


# ------------------------------------------------------------------ list_part_filters

def test_list_without_category_names_categories_with_counts(idx):
    r = pi.list_filters(idx)
    assert {c["category"]: (c["subcategories"], c["parts"]) for c in r["categories"]} == {
        "Capacitors": (2, 14), "Diodes": (1, 7), "Resistors": (1, 13)}
    assert r["tiers"] == ["basic", "preferred", "extended"]
    assert set(pi.FILTER_NAMES) == set(r["filter_names"])
    assert r["attribute_ops"] == list(pi.ATTRIBUTE_OPS)


def test_list_with_category_gives_attribute_names_values_and_counts(idx):
    r = pi.list_filters(idx, category="resistors")
    assert r["category"] == "Resistors" and r["parts_in_scope"] == 13
    attrs = {a["name"]: a for a in r["attributes"]}
    assert set(attrs) == {"Resistance", "Tolerance", "Power Watts"}
    res = attrs["Resistance"]
    assert res["unit"] == OHM and res["parts_with_value"] == 13
    counts = {v["value"]: v["count"] for v in res["values"]}
    assert counts["10k" + OHM] == 3 and counts["100k" + OHM] == 2
    assert res["numeric_range"] == [0.0, 1e6]
    assert attrs["Tolerance"]["unit"] == "%"


def test_list_applies_the_jlc_override_and_ignores_dashes(idx):
    attrs = {a["name"]: a for a in pi.list_filters(idx, category="Resistors", max_values=50)["attributes"]}
    values = {v["value"]: v["count"] for v in attrs["Resistance"]["values"]}
    assert values["33k" + OHM] == 1  # JLCPCB value wins over the LCSC "-"
    assert "-" not in values
    assert {v["value"]: v["count"] for v in attrs["Tolerance"]["values"]}["±2%"] == 1  # a JLCPCB "-" never overwrites


def test_list_orders_values_by_count_and_cuts_with_a_flag(idx):
    r = pi.list_filters(idx, category="Resistors", attributes=["Resistance"], max_values=2)
    res = r["attributes"][0]
    assert len(res["values"]) == 2 and res["values_truncated"] is True
    assert res["values"][0]["count"] >= res["values"][1]["count"]
    assert res["distinct_values"] > 2


def test_list_counts_parts_without_attributes(idx):
    assert pi.list_filters(idx, category="Diodes")["parts_without_attributes"] == 2


def test_list_subcategories(idx):
    r = pi.list_filters(idx, category="Capacitors")
    assert {s["name"]: s["parts"] for s in r["subcategories"]} == {
        "Aluminum Electrolytic Capacitors - SMD": 4, "Multilayer Ceramic Capacitors MLCC - SMD/SMT": 10}
    r = pi.list_filters(idx, category="Capacitors", subcategory="aluminum electrolytic capacitors - smd")
    assert r["parts_in_scope"] == 4 and {a["name"] for a in r["attributes"]} == {"Capacitance", "Voltage Rated"}


def test_list_unknown_category_names_the_valid_ones(idx):
    r = refusal(pi.list_filters, idx, category="Resistor")
    assert r["reason"] == "unknown_category" and r["valid_categories"] == ["Capacitors", "Diodes", "Resistors"]


def test_list_unknown_subcategory_names_the_valid_ones(idx):
    r = refusal(pi.list_filters, idx, category="Capacitors", subcategory="Tantalum")
    assert r["reason"] == "unknown_subcategory" and len(r["valid_subcategories"]) == 2


def test_list_subcategory_without_category_is_refused(idx):
    assert refusal(pi.list_filters, idx, subcategory="Schottky Diodes")["reason"] == "category_required"


def test_list_unknown_attribute_name_names_the_valid_ones(idx):
    r = refusal(pi.list_filters, idx, category="Resistors", attributes=["Resistence"])
    assert r["reason"] == "unknown_attribute" and r["unknown"] == ["Resistence"]
    assert r["valid_attributes"] == ["Power Watts", "Resistance", "Tolerance"]


@pytest.mark.parametrize("bad", [0, 201, True, "5"])
def test_list_max_values_is_checked(idx, bad):
    assert refusal(pi.list_filters, idx, category="Resistors", max_values=bad)["reason"] == "invalid_argument"


def test_list_stays_inside_the_character_budget(idx, monkeypatch):
    monkeypatch.setattr(pi, "CHARACTER_LIMIT", 1500)
    r = pi.list_filters(idx, category="Resistors", max_values=200)
    assert r["values_cut_to"] < 200 and "note" in r


# ------------------------------------------------------------------ filter_parts: index-side filters

def test_category_and_subcategory(idx):
    assert sorted(ids(run(idx, category="Resistors"))) == [*range(1001, 1014)]
    r = run(idx, category="Capacitors", subcategory="Aluminum Electrolytic Capacitors - SMD")
    assert sorted(ids(r)) == [3001, 3002, 3003, 3004]
    assert run(idx)["total_count"] == 34


def test_unknown_category_and_subcategory_are_refused_with_valid_names(idx):
    assert refusal(pi.filter_parts, idx, category="Resistor")["valid_categories"]
    assert refusal(pi.filter_parts, idx, category="Capacitors", subcategory="x")["valid_subcategories"]
    assert refusal(pi.filter_parts, idx, subcategory="Schottky Diodes")["reason"] == "category_required"


def test_package_filter_is_exact_and_case_insensitive(idx):
    assert sorted(ids(run(idx, category="Resistors", package="0402"))) == [1001, 1002, 1009]
    assert sorted(ids(run(idx, category="Diodes", package="sot-23"))) == [4001, 4005, 4006, 4007]
    assert run(idx, category="Resistors", package="040")["total_count"] == 0  # not a substring match


def test_manufacturer_filter(idx):
    assert sorted(ids(run(idx, manufacturer="yageo"))) == [1006, 1009, 2007, 4003, 4004]
    assert sorted(ids(run(idx, category="Resistors", manufacturer="YAGEO"))) == [1006, 1009]


def test_min_stock(idx):
    assert sorted(ids(run(idx, category="Resistors", min_stock=100000))) == [1001, 1003]
    assert sorted(ids(run(idx, category="Resistors", min_stock=0)))[0] == 1001 and run(idx, category="Resistors", min_stock=0)["total_count"] == 13
    assert 1004 not in ids(run(idx, category="Resistors", min_stock=1))  # stock 0
    assert 1003 in ids(run(idx, category="Resistors", min_stock=300000))  # the bound is inclusive


def test_tiers(idx):
    assert sorted(ids(run(idx, category="Capacitors", tiers=["preferred"]))) == [2007, 3002]
    basic = sorted(ids(run(idx, category="Capacitors", tiers=["basic"])))
    assert basic == [2001, 2002, 2003, 2004, 2005, 2006, 2008]
    assert sorted(ids(run(idx, category="Capacitors", tiers=["extended"]))) == [2009, 2010, 3001, 3003, 3004]
    assert run(idx, category="Capacitors", tiers=["basic", "preferred"])["total_count"] == 9
    assert run(idx, category="Capacitors", tiers=["basic", "basic"])["total_count"] == 7


def test_tier_reported_in_the_part_result(idx):
    items = {i["part"]["lcsc_id"]: i["part"] for i in run(idx, category="Capacitors")["items"]}
    assert items["C2007"]["jlcpcb_class"] == "preferred" and items["C2007"]["jlcpcb_basic"] is False
    assert items["C2001"]["jlcpcb_class"] == "basic" and items["C2001"]["jlcpcb_basic"] is True
    assert items["C2009"]["jlcpcb_class"] == "extended" and items["C2009"]["jlcpcb_basic"] is False


def test_free_text_all_words_must_match(idx):
    assert sorted(ids(run(idx, category="Resistors", text="10k"))) == [1001, 1003, 1006]
    assert sorted(ids(run(idx, category="Resistors", text="10k 100mW"))) == [1003, 1006]
    assert sorted(ids(run(idx, text="schottky"))) == [4001, 4002, 4003, 4004, 4005, 4006, 4007]


def test_free_text_matches_mpn_manufacturer_and_lcsc_number(idx):
    assert ids(run(idx, text="PMEG4010")) == [4002]
    assert ids(run(idx, text="nexperia", category="Diodes", min_stock=100000)) == [4001]
    assert ids(run(idx, text="C1003")) == [1003]
    assert ids(run(idx, text="c1003")) == [1003]


def test_free_text_wildcards_are_literal(idx):
    assert run(idx, text="%%")["total_count"] == 0
    assert run(idx, text="1%")["total_count"] > 0  # a real percent sign still matches
    assert run(idx, text="_")["total_count"] == 0
    assert run(idx, text="10_")["total_count"] == 0


def test_values_are_never_spliced_into_sql(idx):
    nasty = "'; DROP TABLE components; --"
    for kw in ({"text": nasty}, {"package": nasty}, {"manufacturer": nasty},
               {"category": "Resistors", "attributes": [{"name": "Resistance", "op": "eq", "value": nasty}]}):
        assert run(idx, **kw)["total_count"] == 0
    assert refusal(pi.filter_parts, idx, category=nasty)["reason"] == "unknown_category"
    assert run(idx, package="' OR 1=1 --")["total_count"] == 0
    assert run(idx)["total_count"] == 34


def test_zero_results_with_package_or_manufacturer_give_hints(idx):
    r = run(idx, category="Resistors", package="040")
    assert r["status"] == "no_results" and r["hints"]["packages_containing"] == ["0402"]
    r = run(idx, manufacturer="yage")
    assert r["hints"]["manufacturers_containing"] == ["YAGEO"]


def test_no_results_is_not_an_error(idx):
    r = run(idx, category="Resistors", min_stock=10**9)
    assert r["ok"] is True and r["status"] == "no_results" and r["total_count"] == 0 and r["items"] == []


# ------------------------------------------------------------------ price

def test_max_price_uses_the_unit_price_at_the_quantity(idx):
    assert sorted(ids(run(idx, category="Resistors", max_price=0.002))) == [1001, 1003, 1004, 1012]
    assert sorted(ids(run(idx, category="Resistors", max_price=0.002, price_quantity=1000))) == [1001, 1002, 1003, 1004, 1007, 1012]
    # between tiers: the tier below applies
    assert sorted(ids(run(idx, category="Resistors", max_price=0.0025, price_quantity=99)))[:1] == [1001]
    assert 1002 not in ids(run(idx, category="Resistors", max_price=0.0025, price_quantity=99))  # 0.003 at 99
    assert 1002 in ids(run(idx, category="Resistors", max_price=0.0025, price_quantity=100))  # 0.0024 at 100


def test_max_price_bound_is_inclusive(idx):
    assert 1001 in ids(run(idx, category="Resistors", max_price=0.002))


def test_a_quantity_below_the_first_tier_uses_the_first_tier():
    assert pi.unit_price('[{"qFrom": 10, "qTo": null, "price": 0.5}]', 1) == 0.5
    assert pi.unit_price('[{"qFrom": 1, "qTo": 9, "price": 1}, {"qFrom": 10, "qTo": null, "price": 0.5}]', 10) == 0.5
    assert pi.unit_price('[{"qFrom": 1, "qTo": 9, "price": 1}, {"qFrom": 10, "qTo": null, "price": 0.5}]', 9) == 1


@pytest.mark.parametrize("bad", ["", "[]", "not json", "{}", '[{"qFrom": 0, "price": 1}]', '[{"qFrom": 1, "price": -1}]',
                                 '[{"qFrom": "1", "price": 1}]', "[1]", None, '[{"qFrom": true, "price": 1}]'])
def test_unreadable_price_is_none(bad):
    assert pi.unit_price(bad, 1) is None


def test_parts_without_a_price_are_excluded_by_max_price_and_counted(idx):
    r = run(idx, category="Diodes", max_price=1.0)
    assert 4007 not in ids(r) and r["excluded_without_price"] == 1
    assert 4007 in ids(run(idx, category="Diodes"))
    assert "excluded_without_price" not in run(idx, category="Diodes")


def test_price_breaks_in_the_part_result(idx):
    part = run(idx, category="Resistors", text="C1001")["items"][0]["part"]
    assert [(b["quantity"], round(b["price_usd"], 6)) for b in part["price_breaks"]] == [(1, 0.002), (100, 0.0016), (1000, 0.0012)]
    no_price = run(idx, text="NOPRICE")["items"][0]["part"]
    assert no_price["price_breaks"] == []


# ------------------------------------------------------------------ attributes

def test_attribute_numeric_equality_with_si_prefix(idx):
    f = [{"name": "Resistance", "op": "eq", "value": "10k"}]
    assert sorted(ids(run(idx, category="Resistors", attributes=f))) == [1001, 1003, 1006]
    f = [{"name": "Resistance", "value": "10000" + OHM}]  # op defaults to eq
    assert sorted(ids(run(idx, category="Resistors", attributes=f))) == [1001, 1003, 1006]


def test_attribute_equality_is_numeric_not_textual(idx):
    f = [{"name": "Capacitance", "op": "eq", "value": "0.1" + MICRO + "F"}]
    assert sorted(ids(run(idx, category="Capacitors", subcategory="Multilayer Ceramic Capacitors MLCC - SMD/SMT", attributes=f))) == [2001, 2002, 2007, 2010]


def test_attribute_comparisons(idx):
    def got(op, value):
        return sorted(ids(run(idx, category="Resistors", attributes=[{"name": "Resistance", "op": op, "value": value}])))
    assert got("lt", "1k") == [1009, 1011]
    assert got("le", "1k") == [1005, 1007, 1009, 1011]
    assert got("gt", "100k") == [1010]
    assert got("ge", "100k") == [1004, 1010, 1012]
    assert got("ne", "10k") == sorted(set(range(1001, 1014)) - {1001, 1003, 1006})


def test_attribute_filters_are_anded_and_a_range_works(idx):
    f = [{"name": "Resistance", "op": "ge", "value": "1k"}, {"name": "Resistance", "op": "le", "value": "10k"},
         {"name": "Tolerance", "op": "eq", "value": "1%"}]
    assert sorted(ids(run(idx, category="Resistors", attributes=f))) == [1001, 1002, 1003, 1006, 1007, 1008]


def test_attribute_filter_with_unit_must_match_the_unit(idx):
    ok = [{"name": "Voltage Rated", "op": "ge", "value": "50V"}]
    assert sorted(ids(run(idx, category="Capacitors", attributes=ok))) == [2001, 2002, 2006, 2007, 2008, 2009]
    wrong_unit = [{"name": "Voltage Rated", "op": "ge", "value": "50A"}]
    assert run(idx, category="Capacitors", attributes=wrong_unit)["total_count"] == 0
    no_unit = [{"name": "Voltage Rated", "op": "ge", "value": 50}]
    assert run(idx, category="Capacitors", attributes=no_unit)["total_count"] == 6


def test_attribute_text_equality_and_contains(idx):
    f = [{"name": "Temperature Coefficient", "op": "eq", "value": "x7r"}]
    assert sorted(ids(run(idx, category="Capacitors", attributes=f))) == [2001, 2002, 2005, 2007, 2008, 2009]
    f = [{"name": "Temperature Coefficient", "op": "contains", "value": "X5"}]
    assert sorted(ids(run(idx, category="Capacitors", attributes=f))) == [2003, 2004, 2010]
    f = [{"name": "Temperature Coefficient", "op": "ne", "value": "X7R"}]
    assert sorted(ids(run(idx, category="Capacitors", attributes=f))) == [2003, 2004, 2006, 2010]


def test_attribute_with_package_and_stock_and_tier_combined(idx):
    r = run(idx, category="Capacitors", package="0603", min_stock=500000, tiers=["basic"],
            attributes=[{"name": "Capacitance", "op": "ge", "value": "100nF"}])
    assert sorted(ids(r)) == [2002, 2005]


def test_a_part_without_the_attribute_never_matches_and_is_counted(idx):
    f = [{"name": "Reverse Voltage", "op": "ge", "value": "40V"}]
    r = run(idx, category="Diodes", attributes=f)
    assert sorted(ids(r)) == [4002, 4003, 4004, 4007]
    assert r["excluded_without_attribute"] == {"Reverse Voltage": 2}
    ne = run(idx, category="Diodes", attributes=[{"name": "Reverse Voltage", "op": "ne", "value": "40V"}])
    assert 4005 not in ids(ne) and 4006 not in ids(ne)


def test_jlc_attribute_overrides_the_lcsc_one_in_filters(idx):
    f = [{"name": "Resistance", "op": "eq", "value": "33k"}]
    assert ids(run(idx, category="Resistors", attributes=f)) == [1013]
    f = [{"name": "Tolerance", "op": "eq", "value": "2%"}]
    assert ids(run(idx, category="Resistors", attributes=f)) == [1013]


def test_unknown_attribute_name_is_refused_with_the_valid_names(idx):
    r = refusal(pi.filter_parts, idx, category="Resistors", attributes=[{"name": "Resistence", "value": "1k"}])
    assert r["reason"] == "unknown_attribute" and r["unknown"] == ["Resistence"]
    assert r["valid_attributes"] == ["Power Watts", "Resistance", "Tolerance"]


def test_attribute_names_are_case_sensitive(idx):
    assert refusal(pi.filter_parts, idx, category="Resistors", attributes=[{"name": "resistance", "value": "1k"}])["reason"] == "unknown_attribute"


def test_an_attribute_of_another_category_is_refused(idx):
    f = [{"name": "Reverse Voltage", "value": "40V"}]
    assert refusal(pi.filter_parts, idx, category="Resistors", attributes=f)["reason"] == "unknown_attribute"


def test_attribute_filter_needs_a_category(idx):
    r = refusal(pi.filter_parts, idx, attributes=[{"name": "Resistance", "value": "1k"}])
    assert r["reason"] == "category_required" and "list_part_filters" in r["message"]


@pytest.mark.parametrize("bad", [
    "text", {"name": "a"}, [{"name": "Resistance"}], [{"value": "1"}], [{"name": "Resistance", "value": "1", "extra": 1}],
    [{"name": "Resistance", "op": "like", "value": "1"}], [{"name": "Resistance", "op": "lt", "value": "abc"}],
    [{"name": "Resistance", "value": ""}], [{"name": "Resistance", "value": True}], [{"name": "Resistance", "value": ["1"]}], ["x"],
])
def test_bad_attribute_filters_are_refused(idx, bad):
    assert refusal(pi.filter_parts, idx, category="Resistors", attributes=bad)["reason"] == "invalid_filter"


def test_a_bad_op_names_the_valid_ops(idx):
    r = refusal(pi.filter_parts, idx, category="Resistors", attributes=[{"name": "Resistance", "op": "like", "value": "1"}])
    assert r["valid_ops"] == ["eq", "ne", "lt", "le", "gt", "ge", "contains"]


# ------------------------------------------------------------------ result shape

def test_each_item_is_a_part_result_with_extra_facts(idx):
    r = run(idx, category="Resistors", text="C1001")
    item = r["items"][0]
    part = PartResult(**item["part"])
    assert part.lcsc_id == "C1001" and part.mpn == "0402WGF1002TCE" and part.manufacturer == "UNI-ROYAL(Uniroyal Elec)"
    assert part.provider == "jlcparts_index" and part.source == "scrape" and part.distributor == "LCSC"
    assert part.stock_qty == 120000 and part.datasheet_url == "https://example.invalid/ds/1001.pdf"
    assert item["package"] == "0402" and item["category"] == "Resistors"
    assert item["subcategory"] == "Chip Resistor - Surface Mount" and "Chip Resistor" in item["description"]
    assert item["attributes"] == {"Resistance": "10k" + OHM, "Tolerance": "±1%", "Power Watts": "62.5mW"}
    assert "attributes_note" not in item
    assert r["index"]["index_date"] == "2026-11-15"
    assert any("currency" in n for n in r["notes"])


def test_a_part_without_parameter_data_has_null_attributes_and_a_note(idx):
    for text in ("NOATTR", "LISTEXTRA"):
        item = run(idx, text=text)["items"][0]
        assert item["attributes"] is None and "No parameter data" in item["attributes_note"]
    r = run(idx, text="NOATTR")
    assert any("attributes null" in n for n in r["notes"])


def test_the_part_without_data_is_listed_without_attribute_filters(idx):
    assert {4005, 4006} <= set(ids(run(idx, category="Diodes")))


def test_works_on_an_older_cache_without_optional_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(pi, "_now", lambda: FIXTURE_DATE)
    p = str(build_fixture(tmp_path / "old.sqlite3", with_optional_columns=False))
    r = pi.filter_parts(p, category="Capacitors", tiers=["preferred"])
    assert r["total_count"] == 0  # no preferred column: nobody is preferred
    r = pi.filter_parts(p, category="Resistors", limit=3)
    assert r["total_count"] == 13 and all(i["attributes"] is None for i in r["items"])
    assert pi.list_filters(p, category="Resistors")["attributes"] == []
    assert pi.filter_parts(p, category="Resistors", tiers=["extended"])["total_count"] == 5


# ------------------------------------------------------------------ sorting and paging

def test_default_sort_is_stock_descending_with_lcsc_tiebreak(idx):
    r = run(idx, category="Capacitors")
    stocks = [i["part"]["stock_qty"] for i in r["items"]]
    assert stocks == sorted(stocks, reverse=True) and r["sort_by"] == "stock" and r["sort_order"] == "desc"
    r = run(idx, category="Capacitors", min_stock=0, tiers=["extended"], sort_by="stock", sort_order="asc")
    assert ids(r)[:2] == [2010, 3003]  # both stock 0: lcsc order


def test_sort_by_price_ascending_by_default_and_unpriced_last(idx):
    r = run(idx, category="Diodes", sort_by="price")
    assert r["sort_order"] == "asc"
    prices = [i["part"]["price_breaks"][0]["price_usd"] if i["part"]["price_breaks"] else None for i in r["items"]]
    assert ids(r) == [4001, 4003, 4004, 4002, 4005, 4006, 4007] and prices[-1] is None
    r = run(idx, category="Diodes", sort_by="price", sort_order="desc")
    assert ids(r) == [4006, 4005, 4002, 4004, 4003, 4001, 4007]  # still unpriced last


def test_sort_by_price_uses_the_price_quantity(idx):
    a = ids(run(idx, category="Capacitors", subcategory="Aluminum Electrolytic Capacitors - SMD", sort_by="price", price_quantity=1))
    assert a == [3001, 3003, 3004, 3002]
    # at 100: 3001 .25*.85=.2125, 3003 .3*.85=.255, 3004 .34, 3002 .3825: same order but proves the tier is read
    r = run(idx, category="Capacitors", subcategory="Aluminum Electrolytic Capacitors - SMD", sort_by="price", price_quantity=100, max_price=0.3)
    assert ids(r) == [3001, 3003]


def test_sort_by_lcsc(idx):
    assert ids(run(idx, category="Diodes", sort_by="lcsc", sort_order="desc"))[:2] == [4007, 4006]
    assert ids(run(idx, category="Diodes", sort_by="lcsc"))[:2] == [4001, 4002]


def test_bad_sort_arguments_are_refused_with_the_valid_names(idx):
    assert refusal(pi.filter_parts, idx, sort_by="name")["valid_sort_fields"] == ["stock", "price", "lcsc"]
    assert refusal(pi.filter_parts, idx, sort_order="up")["valid_sort_orders"] == ["asc", "desc"]


def test_paging_walks_the_whole_result_once(idx):
    seen, offset, pages = [], 0, 0
    while True:
        r = pi.filter_parts(idx, category="Resistors", limit=5, offset=offset, sort_by="lcsc")
        seen += ids(r)
        pages += 1
        assert r["total_count"] == 13 and r["count"] == len(r["items"])
        if not r["has_more"]:
            assert r["next_offset"] is None
            break
        assert r["next_offset"] == offset + 5
        offset = r["next_offset"]
    assert seen == list(range(1001, 1014)) and pages == 3


def test_paging_past_the_end_is_empty_not_an_error(idx):
    r = pi.filter_parts(idx, category="Resistors", offset=100)
    assert r["status"] == "no_results" and r["total_count"] == 13 and r["has_more"] is False


@pytest.mark.parametrize("kw", [{"limit": 0}, {"limit": pi.MAX_LIMIT + 1}, {"limit": True}, {"limit": "5"}, {"offset": -1},
                                {"offset": 1.5}, {"price_quantity": 0}, {"min_stock": -1}, {"min_stock": "5"},
                                {"max_price": -1}, {"max_price": float("nan")}, {"max_price": "1"}, {"max_price": True},
                                {"tiers": []}, {"tiers": ["cheap"]}, {"tiers": "basic"}, {"text": "  "},
                                {"text": "a b c d e f g h i"}, {"package": " "}, {"manufacturer": 5}])
def test_invalid_arguments_are_refused(idx, kw):
    assert refusal(pi.filter_parts, idx, **kw)["reason"] == "invalid_argument"


def test_a_bad_tier_names_the_valid_tiers(idx):
    assert refusal(pi.filter_parts, idx, tiers=["cheap"])["valid_tiers"] == ["basic", "preferred", "extended"]


def test_too_many_candidates_are_refused_not_cut(idx, monkeypatch):
    monkeypatch.setattr(pi, "MAX_CANDIDATES", 12)
    r = refusal(pi.filter_parts, idx, category="Resistors")
    assert r["reason"] == "too_many_candidates" and r["candidates"] == 13
    assert pi.filter_parts(idx, category="Resistors", text="10k")["total_count"] == 3  # narrowed enough
    assert refusal(pi.list_filters, idx, category="Resistors")["reason"] == "too_many_candidates"
    f = [{"name": "Resistance", "value": "1k"}]
    assert refusal(pi.filter_parts, idx, category="Resistors", attributes=f)["reason"] == "too_many_candidates"


def test_the_limit_is_exactly_inclusive(idx, monkeypatch):
    monkeypatch.setattr(pi, "MAX_CANDIDATES", 13)
    assert pi.filter_parts(idx, category="Resistors")["total_count"] == 13


def test_response_is_cut_to_the_character_budget_with_a_resume_point(idx, monkeypatch):
    monkeypatch.setattr(pi, "CHARACTER_LIMIT", 3500)
    r = pi.filter_parts(idx, category="Resistors", limit=13, sort_by="lcsc")
    assert r["truncated"] is True and 1 <= r["count"] < 13 and r["has_more"] is True
    assert r["next_offset"] == r["count"] and "next_offset" in r["truncation_message"]
    nxt = pi.filter_parts(idx, category="Resistors", limit=13, offset=r["next_offset"], sort_by="lcsc")
    assert ids(nxt)[0] == 1001 + r["count"]


def test_a_small_page_is_not_marked_truncated(idx):
    r = pi.filter_parts(idx, category="Resistors", limit=2)
    assert "truncated" not in r and r["count"] == 2 and r["has_more"] is True


# ------------------------------------------------------------------ guards that need their own pin (mutant survivors)

def test_the_connection_uri_is_read_only_and_immutable_and_query_only_is_on(idx, monkeypatch):
    seen = []
    real = sqlite3.connect

    def spy(target, *a, **kw):
        seen.append(target)
        return real(target, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy)
    conn, _ = pi.open_index(idx)
    try:
        assert len(seen) == 1 and "mode=ro" in seen[0] and "immutable=1" in seen[0] and seen[0].startswith("file:")
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        conn.close()


def test_limit_ceiling_is_the_documented_forty(idx):
    assert pi.MAX_LIMIT == 40
    assert pi.filter_parts(idx, limit=40)["limit"] == 40
    assert refusal(pi.filter_parts, idx, limit=41)["reason"] == "invalid_argument"


def test_preferred_wins_over_basic_like_the_jlcparts_web_build():
    assert pi.tier_name(1, 1) == "preferred"
    assert pi.tier_name(1, 0) == "basic" and pi.tier_name(0, 1) == "preferred" and pi.tier_name(0, 0) == "extended"


def test_jlc_attribute_wins_when_both_sides_have_a_value():
    both = '{"attributes": {"Resistance": "%s", "Only lcsc": "x"}}'
    merged = pi.merged_attributes(both % "1k", both % "2k")
    assert merged["Resistance"] == "2k" and merged["Only lcsc"] == "x"
    assert pi.merged_attributes(both % "1k", both % "-")["Resistance"] == "1k"
    assert pi.merged_attributes("[]", None) == {} and pi.merged_attributes("not json", "{}") == {}


# ------------------------------------------------------------------ review round 1: hostile input and data

@pytest.mark.parametrize("text", ["1e9999999", "-1e9999999", "1e-99999999999", "9" * 400, "1e300G", "1e99999999999999999999", 10**400, -(10**400)])
def test_huge_numbers_are_not_quantities_and_never_raise(text):
    assert pi.parse_quantity(text) is None
    assert pi.match_attribute("1e9999999", "eq", "1k") is False
    assert pi.match_attribute("10k", "eq", "1e9999999") is False


@pytest.mark.parametrize("value", [10**400, "1e9999999", float("inf"), float("nan")])
def test_a_huge_filter_value_is_refused_not_raised(idx, value):
    for op in ("eq", "lt"):
        r = refusal(pi.filter_parts, idx, category="Resistors", attributes=[{"name": "Resistance", "op": op, "value": value}])
        assert r["reason"] == "invalid_filter"


def test_a_hostile_index_value_is_unparseable_text_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(pi, "_now", lambda: FIXTURE_DATE)
    p = build_fixture(tmp_path / "h.sqlite3")
    conn = sqlite3.connect(p)
    conn.execute("UPDATE components SET extra = ? WHERE lcsc = 1001", ('{"attributes": {"Resistance": "1e9999999", "Tolerance": "' + "9" * 400 + '"}}',))
    conn.commit()
    conn.close()
    r = pi.filter_parts(str(p), category="Resistors", attributes=[{"name": "Resistance", "op": "gt", "value": "1"}])
    assert 1001 not in ids(r)
    assert pi.list_filters(str(p), category="Resistors")["ok"] is True


B = chr(92)
@pytest.mark.parametrize("given", [B * 2 + "server" + B + "share" + B + "cache.sqlite3", "//server/share/cache.sqlite3", B * 2 + "?" + B + "C:" + B + "x.sqlite3", B * 2 + "." + B + "pipe" + B + "x"])
def test_a_unc_path_is_refused_before_touching_the_file_system(given, monkeypatch):
    def forbidden(self):
        raise AssertionError("the file system was touched")

    monkeypatch.setattr(pi.Path, "is_file", forbidden)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened")))
    for fn in (pi.filter_parts, pi.list_filters):
        r = refusal(fn, given)
        assert r["reason"] == "index_path_refused" and "network" in r["message"]
    monkeypatch.setenv(pi.ENV_VAR, given)
    assert refusal(pi.filter_parts, None)["reason"] == "index_path_refused"


def test_sort_ties_break_by_lcsc_whatever_order_sql_returns(idx, monkeypatch):
    monkeypatch.setattr(pi, "_SCAN_ORDER", " ORDER BY c.lcsc DESC")
    r = run(idx, category="Capacitors", min_stock=0, tiers=["extended"], sort_by="stock", sort_order="asc")
    assert ids(r)[:2] == [2010, 3003]  # both stock 0
    r = run(idx, category="Resistors", sort_by="lcsc", sort_order="asc")
    assert ids(r) == list(range(1001, 1014))
    r = run(idx, category="Capacitors", subcategory="Multilayer Ceramic Capacitors MLCC - SMD/SMT", sort_by="price", sort_order="asc", max_price=0.0015)
    assert ids(r) == sorted(ids(r), key=lambda i: (r["items"][ids(r).index(i)]["part"]["price_breaks"][0]["price_usd"], i))


def _hostile(tmp_path, monkeypatch, sql, params=()):
    monkeypatch.setattr(pi, "_now", lambda: FIXTURE_DATE)
    p = build_fixture(tmp_path / "h.sqlite3")
    conn = sqlite3.connect(p)
    conn.execute(sql, params)
    conn.commit()
    conn.close()
    return str(p)


def test_deeply_nested_json_is_unreadable_data_not_a_crash(tmp_path, monkeypatch):
    deep = "[" * 200000 + "]" * 200000
    p = _hostile(tmp_path, monkeypatch, "UPDATE components SET extra = ?, price = ?, jlc_extra = ? WHERE lcsc = 1001", (deep, deep, deep))
    r = pi.filter_parts(p, category="Resistors", max_price=1.0, attributes=[{"name": "Resistance", "value": "10k"}])
    assert 1001 not in ids(r) and r["ok"] is True
    item = pi.filter_parts(p, text="C1001")["items"][0]
    assert item["attributes"] is None and item["part"]["price_breaks"] == []
    assert pi.list_filters(p, category="Resistors")["ok"] is True


@pytest.mark.parametrize("price", ['[{"qFrom": Infinity, "price": 1}]', '[{"qFrom": 1, "price": Infinity}]', '[{"qFrom": 1e999, "price": 1}]',
                                   '[{"qFrom": NaN, "price": 1}]', '[{"qFrom": 1, "price": 1e999}]'])
def test_a_non_finite_price_tier_is_ignored_not_a_crash(tmp_path, monkeypatch, price):
    p = _hostile(tmp_path, monkeypatch, "UPDATE components SET price = ? WHERE lcsc = 1001", (price,))
    r = pi.filter_parts(p, category="Resistors", max_price=1.0, sort_by="price")
    assert 1001 not in ids(r) and r["excluded_without_price"] == 1
    assert pi.filter_parts(p, text="C1001")["items"][0]["part"]["price_breaks"] == []


def test_text_in_the_stock_column_is_skipped_with_a_count(tmp_path, monkeypatch):
    p = _hostile(tmp_path, monkeypatch, "UPDATE components SET stock = 'lots' WHERE lcsc = 1001")
    for sort_by in ("stock", "price", "lcsc"):
        r = pi.filter_parts(p, category="Resistors", sort_by=sort_by, limit=40)
        assert 1001 not in ids(r) and r["total_count"] == 12 and r["skipped_unreadable_rows"] == 1
    assert "skipped_unreadable_rows" not in pi.filter_parts(str(build_fixture(tmp_path / "ok.sqlite3")), category="Resistors")


def test_a_null_category_name_is_skipped_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(pi, "_now", lambda: FIXTURE_DATE)
    p = build_fixture(tmp_path / "n.sqlite3")
    conn = sqlite3.connect(p)
    conn.executescript("ALTER TABLE categories RENAME TO old; CREATE TABLE categories (id INTEGER PRIMARY KEY, category TEXT, subcategory TEXT);"
                       "INSERT INTO categories SELECT * FROM old; INSERT INTO categories VALUES (99, NULL, NULL); DROP TABLE old;")
    conn.commit()
    conn.close()
    assert pi.filter_parts(str(p), category="Resistors")["total_count"] == 13
    assert pi.list_filters(str(p), category="Resistors")["ok"] is True
    assert [c["category"] for c in pi.list_filters(str(p))["categories"]] == ["Capacitors", "Diodes", "Resistors"]
    assert refusal(pi.filter_parts, str(p), category="Nope")["valid_categories"] == ["Capacitors", "Diodes", "Resistors"]
    assert refusal(pi.filter_parts, str(p), category="Capacitors", subcategory="x")["valid_subcategories"]


def test_an_unreadable_mpn_gives_an_unreadable_item_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(pi, "_now", lambda: FIXTURE_DATE)
    path = build_fixture(tmp_path / "m.sqlite3")
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE c2 AS SELECT * FROM components; DROP TABLE components; ALTER TABLE c2 RENAME TO components;"
                       "UPDATE components SET mfr = NULL WHERE lcsc = 1001;")
    conn.commit()
    conn.close()
    p = str(path)
    r = pi.filter_parts(p, category="Resistors", sort_by="lcsc", limit=3)
    assert r["items"][0]["lcsc_id"] == "C1001" and "unreadable" in r["items"][0] and "part" not in r["items"][0]
    assert r["unreadable_rows"] == 1 and ids_of_ok(r) == [1002, 1003]


def ids_of_ok(r):
    return [int(i["part"]["lcsc_id"][1:]) for i in r["items"] if "part" in i]


@pytest.mark.parametrize("stamp", ["abc", 9223372036854775807, -5, 1e30])
def test_a_hostile_update_time_means_an_unknown_date(tmp_path, monkeypatch, stamp):
    p = _hostile(tmp_path, monkeypatch, "UPDATE components SET last_update = ?", (stamp,))
    info = pi.list_filters(p)["index"]
    assert info["index_date"] is None and info["index_stale"] is True


def test_list_says_how_many_values_were_not_numeric_and_why_unit_is_null(idx):
    attrs = {a["name"]: a for a in pi.list_filters(idx, category="Capacitors", max_values=50)["attributes"]}
    t = attrs["Temperature Coefficient"]
    assert t["unit"] is None and t["numeric_values"] == 0 and t["non_numeric_values"] == t["distinct_values"] == 3
    assert t["non_numeric_parts"] == 10 and "no value reads as a quantity" in t["unit_note"] and "numeric_range" not in t
    v = attrs["Voltage Rated"]
    assert v["unit"] == "V" and v["non_numeric_values"] == 0 and "unit_note" not in v


def test_list_flags_a_range_taken_from_some_values_only(tmp_path, monkeypatch):
    extra = '{"attributes": {"Resistance": "see datasheet", "Tolerance": "±1%", "Power Watts": "5"}}'
    p = _hostile(tmp_path, monkeypatch, "UPDATE components SET extra = ? WHERE lcsc = 1001", (extra,))
    attrs = {a["name"]: a for a in pi.list_filters(p, category="Resistors", max_values=50)["attributes"]}
    res = attrs["Resistance"]
    assert res["non_numeric_values"] == 1 and res["non_numeric_parts"] == 1 and res["numeric_values"] == res["distinct_values"] - 1
    assert "numeric_range" in res and "numeric values only" in res["range_note"]
    mixed = attrs["Power Watts"]
    assert mixed["unit"] is None and "different units" in mixed["unit_note"]


def test_unit_of_says_why_there_is_no_unit():
    assert pi._unit_of(["5", "7"])[0] is None and "no unit" in pi._unit_of(["5", "7"])[2]
    assert pi._unit_of(["5V", "7"])[2].startswith("the numeric values carry different units")
    assert pi._unit_of(["X7R"])[2].startswith("no value reads as a quantity")
    assert pi._unit_of(["5V", "7V"]) == ("V", [5.0, 7.0], None)


# ------------------------------------------------------------------ review round 2

BS = chr(92)
NETWORK_PATHS = [
    BS * 2 + "192.0.2.1" + BS + "share" + BS + "x.sqlite3",
    "//192.0.2.1/share/x.sqlite3",
    "/" + BS + "192.0.2.1" + BS + "share" + BS + "x",
    BS + "/192.0.2.1/share/x",
    BS * 2 + "?" + BS + "UNC" + BS + "192.0.2.1" + BS + "share" + BS + "x",
    BS * 2 + "?" + BS + "C:" + BS + "x.sqlite3",
    BS * 2 + "." + BS + "pipe" + BS + "x",
    BS + "??" + BS + "UNC" + BS + "192.0.2.1" + BS + "share" + BS + "x",
    BS + "??" + BS + "C:" + BS + "x",
    "/??/UNC/192.0.2.1/share/x",
    "//?/UNC/192.0.2.1/share/x",
]


@pytest.mark.parametrize("given", NETWORK_PATHS)
def test_every_unc_and_nt_form_is_refused_without_a_file_system_or_sqlite_call(given, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("the file system or sqlite was touched")

    for name in ("is_file", "exists", "resolve"):
        monkeypatch.setattr(pi.Path, name, forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    for fn in (pi.filter_parts, pi.list_filters):
        assert refusal(fn, given)["reason"] == "index_path_refused"
    monkeypatch.setenv(pi.ENV_VAR, given)
    assert refusal(pi.filter_parts, None)["reason"] == "index_path_refused"
    assert refusal(pi.list_filters, "")["reason"] == "index_path_refused"


@pytest.mark.parametrize("given", ["C:/data/x.sqlite3", "C:" + BS + "data" + BS + "x.sqlite3", "data/x.sqlite3", "./x", "x.sqlite3", "/tmp/x", "C:/a//b"])
def test_local_paths_are_not_taken_for_network_paths(given):
    assert pi._is_network_path(given) is False


@pytest.mark.parametrize("value", ["inf", "NaN", "Infinity", "-inf", "nan"])
def test_a_text_that_only_looks_like_a_float_special_is_still_a_text_value(idx, value):
    for op in ("eq", "contains", "ne"):
        r = pi.filter_parts(idx, category="Capacitors", attributes=[{"name": "Temperature Coefficient", "op": op, "value": value}])
        assert r["ok"] is True
    assert refusal(pi.filter_parts, idx, category="Capacitors",
                   attributes=[{"name": "Temperature Coefficient", "op": "lt", "value": "inf"}])["reason"] == "invalid_filter"
    assert refusal(pi.filter_parts, idx, category="Capacitors",
                   attributes=[{"name": "Temperature Coefficient", "value": float("inf")}])["reason"] == "invalid_filter"
    assert refusal(pi.filter_parts, idx, category="Capacitors",
                   attributes=[{"name": "Temperature Coefficient", "value": "1e9999999"}])["reason"] == "invalid_filter"


def test_a_text_value_inf_matches_a_part_whose_attribute_says_inf(tmp_path, monkeypatch):
    p = _hostile(tmp_path, monkeypatch, "UPDATE components SET extra = ? WHERE lcsc = 1001",
                 ('{"attributes": {"Resistance": "inf", "Tolerance": "NaN"}}',))
    r = pi.filter_parts(p, category="Resistors", attributes=[{"name": "Resistance", "value": "INF"}])
    assert ids(r) == [1001]
    assert ids(pi.filter_parts(p, category="Resistors", attributes=[{"name": "Tolerance", "op": "contains", "value": "nan"}])) == [1001]


def test_list_reports_categories_it_had_to_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(pi, "_now", lambda: FIXTURE_DATE)
    p = build_fixture(tmp_path / "n.sqlite3")
    conn = sqlite3.connect(p)
    conn.executescript("ALTER TABLE categories RENAME TO old; CREATE TABLE categories (id INTEGER PRIMARY KEY, category TEXT, subcategory TEXT);"
                       "INSERT INTO categories SELECT * FROM old; DROP TABLE old;"
                       "INSERT INTO categories VALUES (98, NULL, 'x'); INSERT INTO categories VALUES (99, 'Odd', NULL);"
                       "UPDATE components SET category_id = 98 WHERE lcsc IN (1001, 1002); UPDATE components SET category_id = 99 WHERE lcsc = 2001;")
    conn.commit()
    conn.close()
    r = pi.list_filters(str(p))
    assert r["skipped_categories"]["categories"] == 2 and r["skipped_categories"]["parts"] == 3
    assert [c["category"] for c in r["categories"]] == ["Capacitors", "Diodes", "Resistors"]
    assert "skipped_categories" not in pi.list_filters(str(build_fixture(tmp_path / "ok.sqlite3")))
    assert pi.list_filters(str(p), category="Resistors")["ok"] is True


@pytest.mark.parametrize("given", NETWORK_PATHS)
def test_the_text_rule_alone_refuses_every_form(given):
    assert pi._network_by_text(given) is True


@pytest.mark.skipif(__import__("os").name != "nt", reason="pathlib reads the drive of a UNC path only on Windows")
@pytest.mark.parametrize("given", [BS * 2 + "192.0.2.1" + BS + "share" + BS + "x", "/" + BS + "192.0.2.1" + BS + "share" + BS + "x",
                                   BS + "/192.0.2.1/share/x", "//192.0.2.1/share/x"])
def test_the_drive_rule_alone_refuses_what_pathlib_calls_unc(given):
    assert pi._network_by_drive(given) is True
    assert pi._network_by_drive("C:/data/x") is False


def test_the_two_rules_are_ored_each_one_alone_is_enough(monkeypatch):
    unc = BS * 2 + "192.0.2.1" + BS + "share" + BS + "x"
    monkeypatch.setattr(pi, "_network_by_drive", lambda given: False)
    assert pi._is_network_path(unc) is True
    monkeypatch.undo()
    monkeypatch.setattr(pi, "_network_by_text", lambda given: False)
    assert pi._is_network_path(unc) is True and pi._is_network_path("C:/data/x") is False


def test_the_drive_rule_reads_unc_through_a_platform_independent_path_class(monkeypatch):
    class NoDrive:
        def __init__(self, *a):
            self.drive = ""

    monkeypatch.setattr(pi, "Path", NoDrive)
    assert pi._network_by_drive(BS * 2 + "192.0.2.1" + BS + "share" + BS + "x") is True
