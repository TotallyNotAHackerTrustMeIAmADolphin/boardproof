"""Tests for the JLCPCB part-tier lookup layer (archived backlog P1-22).

Level 1 (docs/TESTING.md): the lookup functions are called directly. Everything
here runs offline -- that is the point of the module, so a test that needed
the network would be testing the wrong thing.

The synthetic library fixtures are built through `kicad_tools`' own
S-expression API (`SExp` + `serialize_sexp`), never by writing KiCad file
text by hand, per CLAUDE.md's standing guardrail.
"""

from __future__ import annotations

import json

import pytest
from kicad_tools.sexp.parser import SExp, serialize_sexp

from kicad_mcp.sourcing import PartResult
from kicad_mcp.sourcing import jlcpcb_basic as jlc


# --- fixtures --------------------------------------------------------------


def _symbol(name: str, **props: str) -> SExp:
    children: list[SExp] = [SExp.quoted_atom(name)]
    for key, value in props.items():
        children.append(SExp.list("property", SExp.quoted_atom(key.replace("_", " ")), SExp.quoted_atom(value)))
    return SExp.list("symbol", *children)


def _write_footprint(path, model_filename: str | None = None) -> None:
    """A minimal-but-real `.kicad_mod`, optionally carrying a `(model ...)`
    reference -- written the same PCM-path-shaped way the real
    JLCPCB-Kicad-Library writes it (`${KICAD8_3RD_PARTY}/.../<name>.step`),
    since the code under test must resolve only the basename out of that,
    never trust its directory portion.
    """
    children = [SExp.quoted_atom(path.stem), SExp.list("layer", SExp.quoted_atom("F.Cu"))]
    if model_filename:
        children.append(
            SExp.list(
                "model",
                SExp.quoted_atom(
                    f"${{KICAD8_3RD_PARTY}}/3dmodels/com_github_CDFER_JLCPCB-Kicad-Library/JLCPCB.3dshapes/{model_filename}"
                ),
                SExp.list("offset", SExp.list("xyz", SExp.atom(0), SExp.atom(0), SExp.atom(0))),
            )
        )
    path.write_text(serialize_sexp(SExp.list("footprint", *children)), encoding="utf-8")


def _write_symbol_lib(path, *symbols: SExp) -> None:
    lib = SExp.list(
        "kicad_symbol_lib",
        SExp.list("version", SExp.atom(20231120)),
        SExp.list("generator", SExp.quoted_atom("kicad_symbol_editor")),
        *symbols,
    )
    path.write_text(serialize_sexp(lib), encoding="utf-8")


@pytest.fixture
def fake_library(tmp_path):
    """A minimal stand-in for a CDFER/JLCPCB-Kicad-Library clone."""
    root = tmp_path / "JLCPCB-Kicad-Library"
    symbols = root / "symbols"
    symbols.mkdir(parents=True)
    _write_symbol_lib(
        symbols / "JLCPCB-Analog.kicad_sym",
        _symbol(
            "Comparator, LM393",
            LCSC="C7955",
            Part="LM393DR2G",
            Manufacturer="onsemi",
            Class="Basic Component",
            Footprint="PCM_JLCPCB:SOIC-8_L5.0-W4.0-P1.27-LS6.0-BL",
            Minimum_Qty="5",
            Attrition_Qty="4",
            Stock="418418",
            Price="0.071USD",
        ),
        _symbol(
            "Op-Amp, LM2904",
            LCSC="C18229",
            Part="LM2904DR2G",
            Manufacturer="onsemi",
            Class="Preferred Component",
            Footprint="PCM_JLCPCB:SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL",
        ),
    )
    # The real library's hand-added Extended symbols carry NO Class property;
    # tier there comes from library membership alone.
    _write_symbol_lib(
        symbols / "JLCPCB-Extended.kicad_sym",
        _symbol("MCU, RP2040", LCSC="C2040", Footprint="PCM_JLCPCB:QFN-56"),
    )
    # A manufacturing helper: no Class, no LCSC -> not an orderable part.
    _write_symbol_lib(
        symbols / "JLCPCB-Manufacturing.kicad_sym",
        _symbol("Mousebites, Cosmetic, 3mm", Footprint="PCM_JLCPCB:MouseBites"),
    )
    (root / "footprints" / "JLCPCB.pretty").mkdir(parents=True)
    (root / "3dmodels" / "JLCPCB.3dshapes").mkdir(parents=True)
    return root


@pytest.fixture
def fake_index(fake_library):
    built = jlc.build_index(fake_library, commit="deadbeef")
    assert built["status"] == "ok"
    return jlc.JLCPCBBasicIndex(built)


# --- index building --------------------------------------------------------


def test_build_index_reads_tier_from_the_class_property(fake_library):
    built = jlc.build_index(fake_library)
    assert built["status"] == "ok"
    by_lcsc = {p["lcsc"]: p for p in built["parts"]}
    assert by_lcsc["C7955"]["tier"] == jlc.TIER_BASIC
    assert by_lcsc["C7955"]["tier_source"] == "symbol_property"
    assert by_lcsc["C18229"]["tier"] == jlc.TIER_PREFERRED
    assert by_lcsc["C7955"]["mpn"] == "LM393DR2G"
    assert by_lcsc["C7955"]["minimum_qty"] == 5


def test_build_index_infers_extended_from_library_membership_and_says_so(fake_library):
    built = jlc.build_index(fake_library)
    rp2040 = next(p for p in built["parts"] if p["lcsc"] == "C2040")
    assert rp2040["tier"] == jlc.TIER_EXTENDED
    # Weaker evidence than a Class property -- the index has to admit that.
    assert rp2040["tier_source"] == "library_membership"


def test_build_index_skips_non_parts_without_failing(fake_library):
    built = jlc.build_index(fake_library)
    assert built["counts"]["total"] == 3
    assert any("Mousebites" in s.get("symbol", "") for s in built["skipped"])


def test_build_index_drops_volatile_stock_and_price(fake_library):
    # Upstream rewrites Stock/Price daily; a vendored snapshot of them would
    # be a silently wrong answer within a day.
    built = jlc.build_index(fake_library)
    for entry in built["parts"]:
        assert "stock" not in entry
        assert "price" not in entry


def test_build_index_on_a_directory_that_is_not_the_library(tmp_path):
    result = jlc.build_index(tmp_path / "nope")
    assert result["status"] == "error"
    assert "symbols/" in result["reason"]


def test_write_index_refuses_a_failed_build(tmp_path):
    result = jlc.write_index({"status": "error", "reason": "parse blew up"}, tmp_path / "out.json")
    assert result["status"] == "error"
    assert not (tmp_path / "out.json").exists()


# --- the shipped index -----------------------------------------------------


def test_vendored_index_is_present_and_sane():
    index = jlc.load_index()
    assert index is not None, "the vendored JLCPCB tier index should ship with the package"
    assert len(index) > 500
    assert index.source["license"] == "MIT"
    assert index.source["repository"] == jlc.UPSTREAM_REPO_URL
    assert index.counts["basic"] > 0
    assert index.counts["preferred"] > 0


def test_vendored_index_knows_a_real_basic_part():
    # C7955 / LM393DR2G is a JLCPCB Basic part in the upstream library.
    result = jlc.classify_parts(["C7955"])
    assert result["status"] == "ok"
    assert result["results"][0]["jlcpcb_class"] == jlc.TIER_BASIC
    assert result["results"][0]["jlcpcb_basic"] is True


def test_vendored_index_knows_a_real_extended_part():
    # RP2040 is in the hand-added Extended library.
    result = jlc.classify_parts(["C2040"])
    assert result["results"][0]["jlcpcb_class"] == jlc.TIER_EXTENDED
    assert result["results"][0]["jlcpcb_basic"] is False


# --- classification (batch) ------------------------------------------------


def test_classify_parts_is_batch_and_mixes_lcsc_ids_with_mpns(fake_index):
    result = jlc.classify_parts(["C7955", "LM2904DR2G", "C2040"], index=fake_index)
    assert result["status"] == "ok"
    assert result["count"] == 3
    tiers = [r["jlcpcb_class"] for r in result["results"]]
    assert tiers == [jlc.TIER_BASIC, jlc.TIER_PREFERRED, jlc.TIER_EXTENDED]
    assert [r["match_type"] for r in result["results"]] == ["lcsc", "mpn", "lcsc"]
    assert result["summary"] == {"basic": 1, "preferred": 1, "extended": 1, "unknown": 0}


def test_unknown_part_is_null_not_false(fake_index):
    """The whole point of the tri-state: absence from the index is 'unknown',
    which must never be reported as 'confirmed Extended'."""
    result = jlc.classify_parts(["C999999999"], index=fake_index)
    assert result["status"] == "partial"
    entry = result["results"][0]
    assert entry["jlcpcb_basic"] is None
    assert entry["jlcpcb_class"] is None
    assert entry["matched"] is False
    assert "NOT 'confirmed Extended'" in entry["reason"]
    assert "reason" in result


def test_preferred_part_is_not_basic_but_is_fee_exempt(fake_index):
    entry = jlc.classify_parts(["C18229"], index=fake_index)["results"][0]
    assert entry["jlcpcb_basic"] is False
    assert entry["jlcpcb_class"] == jlc.TIER_PREFERRED
    # ...and False here must not be read as "costs a setup fee".
    assert entry["setup_fee_exempt_economic"] is True


def test_extended_part_is_not_fee_exempt(fake_index):
    entry = jlc.classify_parts(["C2040"], index=fake_index)["results"][0]
    assert entry["jlcpcb_basic"] is False
    assert entry["setup_fee_exempt_economic"] is False


def test_classify_parts_rejects_bad_input(fake_index):
    assert jlc.classify_parts([], index=fake_index)["status"] == "error"
    assert jlc.classify_parts("C7955", index=fake_index)["status"] == "error"  # type: ignore[arg-type]


def test_classify_parts_reports_unavailable_when_index_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(jlc, "VENDORED_INDEX_PATH", tmp_path / "gone.json")
    monkeypatch.setattr(jlc, "_INDEX_CACHE", None)
    result = jlc.classify_parts(["C7955"])
    assert result["status"] == "unavailable"
    assert "reason" in result
    # Never a bare empty list where the honest answer is "blocked" (P1-04).
    assert result["results"][0]["jlcpcb_basic"] is None
    assert jlc.JLCPCB_LIBRARY_ENV_VAR in result["reason"]


def test_conflicting_mpn_matches_report_the_conflict_instead_of_picking(tmp_path):
    payload = {
        "schema_version": 1,
        "source": {},
        "counts": {},
        "parts": [
            {"lcsc": "C1", "mpn": "AMBIG", "manufacturer": "A", "tier": "basic"},
            {"lcsc": "C2", "mpn": "AMBIG", "manufacturer": "B", "tier": "extended"},
        ],
    }
    index = jlc.JLCPCBBasicIndex(payload)
    entry = jlc.classify_parts(["AMBIG"], index=index)["results"][0]
    assert entry["jlcpcb_class"] is None
    assert "conflicting tiers" in entry["reason"]
    assert len(entry["candidates"]) == 2


def test_corrupt_index_degrades_explicitly_rather_than_raising(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(jlc, "VENDORED_INDEX_PATH", bad)
    monkeypatch.setattr(jlc, "_INDEX_CACHE", None)
    assert jlc.load_index() is None
    assert jlc.classify_parts(["C7955"])["status"] == "unavailable"


# --- PartResult annotation -------------------------------------------------


def _part(mpn: str, **kwargs) -> PartResult:
    return PartResult(manufacturer=kwargs.pop("manufacturer", "onsemi"), mpn=mpn, provider="fake", **kwargs)


def test_annotate_part_results_is_batch_and_prefers_lcsc_over_mpn(fake_index):
    parts = [_part("LM393DR2G", lcsc_id="C7955"), _part("LM2904DR2G"), _part("Unknown-XYZ")]
    annotated, report = jlc.annotate_part_results(parts, index=fake_index)
    assert [p.jlcpcb_class for p in annotated] == [jlc.TIER_BASIC, jlc.TIER_PREFERRED, None]
    assert [p.jlcpcb_basic for p in annotated] == [True, False, None]
    assert report["status"] == "partial"
    assert report["summary"]["unknown"] == 1


def test_annotate_leaves_parts_untouched_when_index_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(jlc, "VENDORED_INDEX_PATH", tmp_path / "gone.json")
    monkeypatch.setattr(jlc, "_INDEX_CACHE", None)
    parts = [_part("LM393DR2G", lcsc_id="C7955")]
    annotated, report = jlc.annotate_part_results(parts)
    assert annotated[0].jlcpcb_basic is None
    assert report["status"] == "unavailable"


# --- tier ranking ----------------------------------------------------------


def test_tier_rank_orders_basic_then_preferred_then_unknown_then_extended():
    ranks = [jlc.tier_rank(t) for t in (jlc.TIER_BASIC, jlc.TIER_PREFERRED, None, jlc.TIER_EXTENDED)]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == 4
    # Unknown must never outrank a confirmed fee-exempt part.
    assert jlc.tier_rank(None) > jlc.tier_rank(jlc.TIER_PREFERRED)


# --- local library / asset resolution --------------------------------------


def test_library_status_is_explicit_about_a_missing_local_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv(jlc.JLCPCB_LIBRARY_ENV_VAR, str(tmp_path / "not-here"))
    monkeypatch.setattr(jlc, "_candidate_library_roots", lambda explicit=None: [tmp_path / "not-here"])
    status = jlc.library_status()
    assert status["library_available"] is False
    assert "library_reason" in status
    assert "easyeda2kicad" in status["library_reason"]
    assert jlc.JLCPCB_LIBRARY_ENV_VAR in status["library_reason"]


def test_resolve_library_root_finds_a_clone_layout(fake_library):
    layout = jlc.resolve_library_root(fake_library)
    assert layout is not None
    assert layout["layout"] == "clone"
    assert layout["symbols_dir"] == fake_library / "symbols"


def test_resolve_assets_prefers_local_library_when_files_are_present(fake_library, fake_index):
    fp = fake_library / "footprints" / "JLCPCB.pretty" / "SOIC-8_L5.0-W4.0-P1.27-LS6.0-BL.kicad_mod"
    _write_footprint(fp, model_filename="SOIC-8_L5.0-W4.0-P1.27-LS6.0-BL.step")
    model = fake_library / "3dmodels" / "JLCPCB.3dshapes" / "SOIC-8_L5.0-W4.0-P1.27-LS6.0-BL.step"
    model.write_text("", encoding="utf-8")

    result = jlc.resolve_assets(["C7955"], library_root=fake_library, index=fake_index)
    assert result["status"] == "ok"
    entry = result["results"][0]
    assert entry["source"] == "jlcpcb_library"
    assert entry["status"] == "ok"
    assert entry["symbol_library_path"].endswith("JLCPCB-Analog.kicad_sym")
    assert entry["footprint_path"] == str(fp)
    assert entry["model_3d_path"] == str(model)


def test_resolve_assets_reads_the_real_model_filename_off_the_footprint(fake_library, fake_index):
    """Regression test for a real bug found while smoke-testing this ticket
    against the live PCM install: JLCPCB-Kicad-Library's QFN-56 footprint is
    named "...EP3.2x3.2mm_ThermalVias" but its 3D model file is
    "...EP3.2x3.2mm.step" -- NOT the same stem. Guessing the model filename
    from the footprint name silently reported a real, present 3D model as
    missing. The footprint's own `(model "...")` reference must be read
    instead."""
    fp = fake_library / "footprints" / "JLCPCB.pretty" / "SOIC-8_L5.0-W4.0-P1.27-LS6.0-BL.kicad_mod"
    _write_footprint(fp, model_filename="Totally-Different-Model-Name.step")
    (fake_library / "3dmodels" / "JLCPCB.3dshapes" / "SOIC-8_L5.0-W4.0-P1.27-LS6.0-BL.step").write_text(
        "", encoding="utf-8"
    )
    real_model = fake_library / "3dmodels" / "JLCPCB.3dshapes" / "Totally-Different-Model-Name.step"
    real_model.write_text("", encoding="utf-8")

    entry = jlc.resolve_assets(["C7955"], library_root=fake_library, index=fake_index)["results"][0]
    assert entry["status"] == "ok"
    assert entry["model_3d_path"] == str(real_model)


def test_resolve_assets_reports_missing_model_when_footprint_has_no_model_directive(fake_library, fake_index):
    fp = fake_library / "footprints" / "JLCPCB.pretty" / "SOIC-8_L5.0-W4.0-P1.27-LS6.0-BL.kicad_mod"
    _write_footprint(fp, model_filename=None)
    entry = jlc.resolve_assets(["C7955"], library_root=fake_library, index=fake_index)["results"][0]
    assert entry["status"] == "partial"
    assert entry["model_3d_path"] is None


def test_resolve_assets_reports_partial_when_only_the_symbol_is_present(fake_library, fake_index):
    result = jlc.resolve_assets(["C7955"], library_root=fake_library, index=fake_index)
    entry = result["results"][0]
    assert entry["source"] == "jlcpcb_library"
    assert entry["status"] == "partial"
    assert entry["footprint_path"] is None
    assert "footprint" in entry["reason"]


def test_resolve_assets_falls_back_to_easyeda2kicad_for_uncovered_parts(fake_library, fake_index):
    result = jlc.resolve_assets(["C999999999"], library_root=fake_library, index=fake_index)
    entry = result["results"][0]
    assert entry["source"] == "easyeda2kicad"
    assert entry["lcsc_id"] == "C999999999"
    assert "fetch_part_assets" in entry["reason"]
    assert result["status"] == "no_local_assets"


def test_resolve_assets_without_a_local_checkout_says_what_it_would_have_given(fake_index, monkeypatch, tmp_path):
    monkeypatch.setattr(jlc, "_candidate_library_roots", lambda explicit=None: [tmp_path / "absent"])
    result = jlc.resolve_assets(["C7955"], index=fake_index)
    entry = result["results"][0]
    assert entry["source"] == "easyeda2kicad"
    assert entry["would_provide"]["symbol_library"] == "JLCPCB-Analog"
    assert jlc.JLCPCB_LIBRARY_ENV_VAR in entry["reason"]
    assert result["library_available"] is False


def test_resolve_assets_rejects_bad_input(fake_index):
    assert jlc.resolve_assets([], index=fake_index)["status"] == "error"


def test_refresh_index_without_a_checkout_is_unavailable_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(jlc, "_candidate_library_roots", lambda explicit=None: [tmp_path / "absent"])
    result = jlc.refresh_index()
    assert result["status"] == "unavailable"
    assert jlc.JLCPCB_LIBRARY_ENV_VAR in result["reason"]


def test_round_trip_build_write_load(fake_library, tmp_path):
    built = jlc.build_index(fake_library, commit="cafe")
    out = tmp_path / "index.json"
    written = jlc.write_index(built, out)
    assert written["status"] == "ok"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["source"]["commit"] == "cafe"
    reloaded = jlc.load_index(out)
    assert reloaded is not None
    assert len(reloaded) == 3


# --- lookup order: explicit, env var, submodule, PCM (publish-prep ticket 03b) ---------------


def _mk_clone(root):
    symbols = root / "symbols"
    symbols.mkdir(parents=True)
    _write_symbol_lib(symbols / "JLCPCB-Analog.kicad_sym", _symbol("Comparator, LM393", LCSC="C7955", Class="Basic"))
    return root


def _mk_pcm(home):
    third_party = home / "Documents" / "KiCad" / "10.0" / "3rdparty"
    sym = third_party / "symbols" / jlc._PCM_DIR_NAME
    sym.mkdir(parents=True)
    _write_symbol_lib(sym / "JLCPCB-Analog.kicad_sym", _symbol("Comparator, LM393", LCSC="C7955", Class="Basic"))
    return third_party


@pytest.fixture
def where(tmp_path, monkeypatch):
    """An isolated world: no env var, a fake home with no KiCad, a fake checkout root.

    Returns a namespace of the paths; each test creates only the candidates it needs.
    """
    fake_user = tmp_path / "fake-user"
    fake_user.mkdir()
    repo = tmp_path / "repo"
    (repo / "src" / "kicad_mcp").mkdir(parents=True)
    (repo / ".gitmodules").write_text("[submodule]\n", encoding="utf-8")
    monkeypatch.delenv(jlc.JLCPCB_LIBRARY_ENV_VAR, raising=False)
    monkeypatch.setattr(jlc, "_home_dir", lambda: fake_user)
    monkeypatch.setattr(jlc, "_checkout_root", lambda: repo)

    class W:
        pass

    w = W()
    w.home, w.repo = fake_user, repo
    w.submodule = repo / jlc.SUBMODULE_RELATIVE_PATH
    w.env_dir = tmp_path / "from-env"
    w.setenv = lambda: monkeypatch.setenv(jlc.JLCPCB_LIBRARY_ENV_VAR, str(w.env_dir))
    return w


def _root_of(layout):
    return layout["root"] if layout else None


def test_env_var_wins_over_submodule_and_pcm(where):
    _mk_clone(where.env_dir)
    _mk_clone(where.submodule)
    pcm = _mk_pcm(where.home)
    where.setenv()
    assert _root_of(jlc.resolve_library_root()) == where.env_dir
    assert pcm.is_dir()  # present but lower in the order


def test_explicit_argument_wins_over_env_var(where, tmp_path):
    explicit = _mk_clone(tmp_path / "explicit")
    _mk_clone(where.env_dir)
    where.setenv()
    assert _root_of(jlc.resolve_library_root(explicit)) == explicit


def test_submodule_wins_over_pcm_when_no_env_var(where):
    _mk_clone(where.submodule)
    _mk_pcm(where.home)
    layout = jlc.resolve_library_root()
    assert _root_of(layout) == where.submodule
    assert layout["layout"] == "clone"


def test_pcm_is_used_when_nothing_else_is_there(where):
    third_party = _mk_pcm(where.home)
    layout = jlc.resolve_library_root()
    assert _root_of(layout) == third_party
    assert layout["layout"] == "pcm"


def test_no_candidate_at_all_resolves_to_none(where):
    assert jlc.resolve_library_root() is None


def test_an_empty_submodule_directory_is_not_a_library_and_is_reported(where):
    where.submodule.mkdir(parents=True)  # what `git worktree add` leaves behind
    assert jlc.resolve_library_root() is None
    status = jlc.library_status()
    assert status["library_available"] is False
    assert any("not initialised" in n and jlc.SUBMODULE_FETCH_COMMAND in n for n in status["library_notes"])
    assert jlc.SUBMODULE_FETCH_COMMAND in status["library_reason"]
    assert status["fetch_command"] == "git submodule update --init --depth 1 third_party/JLCPCB-Kicad-Library"


def test_an_empty_submodule_is_reported_even_when_pcm_is_used_instead(where):
    where.submodule.mkdir(parents=True)
    third_party = _mk_pcm(where.home)
    assert _root_of(jlc.resolve_library_root()) == third_party
    notes = jlc.library_notes()
    assert len(notes) == 1 and jlc.SUBMODULE_FETCH_COMMAND in notes[0]
    assert jlc.library_status()["library_notes"] == notes


def test_a_missing_submodule_directory_is_reported_with_the_command(where):
    notes = jlc.library_notes()
    assert len(notes) == 1
    assert "missing" in notes[0] and jlc.SUBMODULE_FETCH_COMMAND in notes[0]


def test_a_submodule_directory_with_unrelated_files_is_reported(where):
    where.submodule.mkdir(parents=True)
    (where.submodule / "README.md").write_text("x", encoding="utf-8")
    assert jlc.resolve_library_root() is None
    assert "no 'symbols/JLCPCB-*.kicad_sym'" in jlc.library_notes()[0]


def test_a_dead_env_var_is_reported_and_the_next_candidate_is_used(where):
    _mk_clone(where.submodule)
    where.setenv()  # points at a directory that does not exist
    assert _root_of(jlc.resolve_library_root()) == where.submodule
    notes = jlc.library_notes()
    assert len(notes) == 1 and jlc.JLCPCB_LIBRARY_ENV_VAR in notes[0] and "not a directory" in notes[0]


def test_no_notes_when_the_first_candidate_is_fine(where):
    _mk_clone(where.submodule)
    assert jlc.library_notes() == []
    assert "library_notes" not in jlc.library_status()


def test_resolve_assets_carries_the_notes(where, fake_index):
    where.submodule.mkdir(parents=True)
    out = jlc.resolve_assets(["C7955"], index=fake_index)
    assert out["library_available"] is False
    assert jlc.SUBMODULE_FETCH_COMMAND in out["library_notes"][0]
    assert jlc.SUBMODULE_FETCH_COMMAND in out["results"][0]["reason"]


def test_refresh_index_unavailable_names_the_fetch_command(where):
    where.submodule.mkdir(parents=True)
    out = jlc.refresh_index()
    assert out["status"] == "unavailable"
    assert jlc.SUBMODULE_FETCH_COMMAND in out["reason"]
    assert jlc.SUBMODULE_FETCH_COMMAND in out["library_notes"][0]


def test_outside_a_checkout_the_submodule_path_is_not_considered(where, monkeypatch, tmp_path):
    monkeypatch.undo()  # drop the isolated world; test the real _checkout_root
    site = tmp_path / "site-packages" / "kicad_mcp" / "sourcing"
    site.mkdir(parents=True)
    monkeypatch.setattr(jlc, "__file__", str(site / "jlcpcb_basic.py"))
    assert jlc._checkout_root() is None


def test_checkout_root_is_this_repository_and_matches_gitmodules():
    root = jlc._checkout_root()
    assert root is not None
    assert (root / jlc.SUBMODULE_RELATIVE_PATH).parent.is_dir()
    gitmodules = (root / ".gitmodules").read_text(encoding="utf-8")
    assert f"path = {jlc.SUBMODULE_RELATIVE_PATH}" in gitmodules
    assert "shallow = true" in gitmodules
    assert jlc.UPSTREAM_REPO_URL in gitmodules


# --- the real submodule (skipped with a stated reason when it is not fetched) ----------------


@pytest.fixture
def real_submodule():
    root = jlc._checkout_root()
    path = root / jlc.SUBMODULE_RELATIVE_PATH if root else None
    if path is None or jlc._library_layout(path) is None:
        pytest.skip(f"JLCPCB library submodule not initialised; fetch it with: {jlc.SUBMODULE_FETCH_COMMAND}")
    return path


def test_vendored_index_is_what_the_pinned_submodule_builds(real_submodule):
    """The checked-in index and the pinned library agree (regenerate with scripts/refresh_jlcpcb_index.py)."""
    vendored = json.loads(jlc.VENDORED_INDEX_PATH.read_text(encoding="utf-8"))
    built = jlc.build_index(real_submodule, commit=vendored["source"]["commit"])
    assert built["status"] == "ok"
    assert built["parts"] == vendored["parts"]
    assert built["counts"] == vendored["counts"]


def test_resolve_assets_finds_a_real_part_in_the_submodule(real_submodule):
    out = jlc.resolve_assets(["C7955"], library_root=real_submodule)
    assert out["library_available"] is True
    first = out["results"][0]
    assert first["source"] == "jlcpcb_library"
    assert jlc.Path(first["symbol_library_path"]).is_file()
    assert jlc.Path(first["footprint_path"]).is_file()
    assert jlc.library_notes(real_submodule) == []
