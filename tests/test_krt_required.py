"""Build ticket 53: a test that needs KiCadRoutingTools skips with the reason when KRT is
absent, and fails when ``KICAD_ROUTING_TOOLS_REQUIRED`` is set (the gate run).

Covers the helper (``tests/_krt.py``), the conftest wiring (``needs_krt`` marker and the
``krt_checkout`` fixture, in a nested pytest run), the rung-1 layout tests that failed instead
of skipping, and a ratchet: no other module in ``tests/`` makes its own KRT skip decision.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from tests import _krt

pytest_plugins = ["pytester"]  # the conftest wiring is tested in a nested pytest run

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent


def _assert_fails(match: str, fn, *args, **kwargs) -> None:
    """``fn`` must call ``pytest.fail``. Not ``pytest.raises``: a ``Skipped`` escaping it would
    turn this very test into a skip, so a fail-to-skip regression would pass unseen."""
    try:
        fn(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 -- Skipped and Failed are BaseExceptions
        outcome = exc
    else:
        outcome = None
    assert isinstance(outcome, pytest.fail.Exception), f"expected pytest.fail, got {outcome!r}"
    assert match in str(outcome), str(outcome)


def _absent():
    return False, "KICAD_ROUTING_TOOLS_PATH='nowhere' does not contain py_router/route.py"


# --- the KICAD_ROUTING_TOOLS_REQUIRED flag ---------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "yes", "false", "no", "off"])  # anything but "" or "0"
def test_the_flag_turns_an_absent_krt_into_a_failure(value: str) -> None:
    _assert_fails("KICAD_ROUTING_TOOLS_REQUIRED is set", _krt.missing, "not found", {_krt.REQUIRED_FLAG: value})


@pytest.mark.parametrize("env", [{}, {"KICAD_ROUTING_TOOLS_REQUIRED": ""}, {"KICAD_ROUTING_TOOLS_REQUIRED": "0"}])
def test_without_the_flag_an_absent_krt_skips_with_the_reason(env: dict) -> None:
    with pytest.raises(pytest.skip.Exception, match="not found") as caught:
        _krt.missing("not found", env)
    assert _krt.REQUIRED_FLAG in str(caught.value)  # the skip says how the gate treats it


def test_require_passes_the_probe_reason_through_to_skip_and_fail() -> None:
    with pytest.raises(pytest.skip.Exception, match="does not contain py_router/route.py"):
        _krt.require({}, probe=_absent)
    _assert_fails("does not contain py_router/route.py", _krt.require, {_krt.REQUIRED_FLAG: "1"}, probe=_absent)


def test_require_returns_quietly_when_krt_is_usable_even_under_the_flag() -> None:
    assert _krt.require({_krt.REQUIRED_FLAG: "1"}, probe=lambda: (True, "")) is None


def test_checkout_or_missing_skips_or_fails_on_a_missing_checkout() -> None:
    from kicad_mcp.routers import kicad_routing_tools as krt

    def gone() -> Path:
        raise krt.KicadRoutingToolsError("KiCadRoutingTools not found.")

    with pytest.raises(pytest.skip.Exception, match="not found"):
        _krt.checkout_or_missing({}, find=gone)
    _assert_fails("not found", _krt.checkout_or_missing, {_krt.REQUIRED_FLAG: "1"}, find=gone)


def test_checkout_or_missing_returns_the_found_checkout(tmp_path: Path) -> None:
    assert _krt.checkout_or_missing({_krt.REQUIRED_FLAG: "1"}, find=lambda: tmp_path) == tmp_path


def test_the_real_probe_reports_a_wrong_path_as_not_usable(tmp_path: Path, monkeypatch) -> None:
    from kicad_mcp.routers import kicad_routing_tools as krt

    monkeypatch.setenv(krt.ENV_PATH, str(tmp_path / "nowhere"))
    usable, reason = _krt.availability()
    assert usable is False
    assert "nowhere" in reason


def test_the_probe_cache_is_keyed_on_the_krt_path(tmp_path: Path, monkeypatch) -> None:
    from kicad_mcp.routers import kicad_routing_tools as krt

    for name in ("first", "second"):
        monkeypatch.setenv(krt.ENV_PATH, str(tmp_path / name))
        usable, reason = _krt.availability()
        assert usable is False and name in reason, reason


def test_a_probe_that_raises_counts_as_not_usable(tmp_path: Path, monkeypatch) -> None:
    from kicad_mcp.routers import kicad_routing_tools as krt

    def boom(**kwargs):
        raise RuntimeError("probe exploded")

    monkeypatch.setenv(krt.ENV_PATH, str(tmp_path / "fresh-cache-key"))
    monkeypatch.setattr(krt, "check_availability", boom)
    usable, reason = _krt.availability()
    assert usable is False
    assert "probe exploded" in reason


# --- the conftest wiring, in a nested run ----------------------------------------------


_MARKED = "import pytest\n\n@pytest.mark.needs_krt\ndef test_one():\n    pass\n"
_CHECKOUT = "def test_one(krt_checkout):\n    pass\n"


@pytest.mark.parametrize("body", [_MARKED, _CHECKOUT], ids=["marker", "fixture"])
def test_the_conftest_wiring_fails_closed_under_the_flag_and_skips_without(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """The real ``tests/conftest.py`` (``-p tests.conftest``), KRT pointed at a missing path."""
    from kicad_mcp.routers import kicad_routing_tools as krt

    monkeypatch.setenv(krt.ENV_PATH, str(pytester.path / "nowhere"))
    pytester.makepyfile(test_one=body)

    monkeypatch.setenv(_krt.REQUIRED_FLAG, "1")
    result = pytester.runpytest("-p", "tests.conftest", "-p", "no:asyncio")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*ERROR at setup of test_one*", "*KICAD_ROUTING_TOOLS_REQUIRED is set*nowhere*"])

    monkeypatch.delenv(_krt.REQUIRED_FLAG)
    result = pytester.runpytest("-p", "tests.conftest", "-p", "no:asyncio", "-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*SKIPPED*KiCadRoutingTools not usable*nowhere*"])


def test_under_the_flag_the_krt_check_runs_before_a_fixture_that_skips_for_another_reason(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rung-1 shape: a module fixture that skips on its own ("kicad-cli not installed") must
    not hide an absent KRT from the gate; the marker is decided before the test's fixtures."""
    from kicad_mcp.routers import kicad_routing_tools as krt

    monkeypatch.setenv(krt.ENV_PATH, str(pytester.path / "nowhere"))
    monkeypatch.setenv(_krt.REQUIRED_FLAG, "1")
    pytester.makepyfile(
        test_one=(
            "import pytest\n\n"
            "@pytest.fixture(scope='module')\n"
            "def other():\n"
            "    pytest.skip('kicad-cli not installed')\n\n"
            "@pytest.mark.needs_krt\n"
            "def test_one(other):\n"
            "    pass\n"
        )
    )
    result = pytester.runpytest("-p", "tests.conftest", "-p", "no:asyncio")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*KICAD_ROUTING_TOOLS_REQUIRED is set*nowhere*"])


def test_a_typoed_marker_is_an_error_under_the_projects_pytest_config(pytester: pytest.Pytester) -> None:
    """Under the committed ``[tool.pytest.ini_options]`` an unknown marker (``needs_krtt``) is an
    error, not a ``PytestUnknownMarkWarning`` that silently drops the gating."""
    pytester.makepyprojecttoml((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pytester.makepyfile(test_one="import pytest\n\n@pytest.mark.needs_krtt\ndef test_one():\n    pass\n")
    result = pytester.runpytest("-p", "tests.conftest", "-p", "no:asyncio", "test_one.py")
    assert result.ret != 0
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*needs_krtt*"])


def test_the_marker_is_registered_so_a_typo_cannot_silently_drop_it(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_one=_MARKED)
    result = pytester.runpytest("-p", "tests.conftest", "-p", "no:asyncio", "--markers")
    result.stdout.fnmatch_lines(["*needs_krt*KICAD_ROUTING_TOOLS_REQUIRED*"])


# --- the rung-1 layout tests (the ticket's reproduction) -------------------------------


@pytest.mark.parametrize(
    "name",
    ["test_rules_loosening_is_limited_to_the_owner_approved_list", "test_flow_reaches_the_pour_step_through_the_tools"],
)
def test_the_rung1_tests_that_reach_the_pour_step_need_krt(name: str) -> None:
    module = importlib.import_module("tests.test_rung1_layout")
    marks = [m.name for m in getattr(getattr(module, name), "pytestmark", [])]
    assert _krt.MARKER in marks, f"{name} reaches KRT's plane step and must carry needs_krt"


@pytest.mark.parametrize(
    ("module", "alias"), [("tests.test_pour_step", "needs_router"), ("tests.test_rung_backends", "needs_krt")]
)
def test_the_module_aliases_are_the_needs_krt_marker(module: str, alias: str) -> None:
    mark = getattr(importlib.import_module(module), alias)
    assert isinstance(mark, pytest.MarkDecorator)
    assert (mark.mark.name, mark.args, mark.kwargs) == (_krt.MARKER, (), {})


# --- ratchet: every KRT availability decision goes through tests/_krt.py ---------------

_PROBES = {"check_availability", "krt_availability", "find_kicad_routing_tools", "availability"}
_SKIPS = {"skip", "skipif", "xfail", "importorskip", "SkipTest"}
_EXEMPT = {"_krt.py", "test_krt_required.py"}
# Replacing a locator controls only the probes built on top of it (review round 3):
# availability('krt') -> krt_availability() -> check_availability() -> find_kicad_routing_tools().
# Not find_kicad_routing_tools_python: replaced alone, it leaves the real checkout deciding.
_COVERS = {
    "find_kicad_routing_tools": frozenset(_PROBES),
    "check_availability": frozenset({"check_availability", "krt_availability", "availability"}),
    "krt_availability": frozenset({"krt_availability", "availability"}),
    "availability": frozenset({"availability"}),
}
_ENV_PATH = {"ENV_PATH", "KICAD_ROUTING_TOOLS_PATH"}
_PATCH_CALLS = {"setattr", "patch", "object"}  # monkeypatch.setattr, mock.patch, mock.patch.object


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _is_skip(func: ast.AST) -> bool:
    """``pytest.skip`` / ``skipif`` / ``xfail`` / ``importorskip``, ``pytest.skip.Exception``
    and ``unittest.SkipTest``."""
    if _name(func) in _SKIPS:
        return True
    return isinstance(func, ast.Attribute) and func.attr == "Exception" and _name(func.value) == "skip"


def _calls(nodes) -> list[ast.Call]:
    return [c for n in nodes for c in ast.walk(n) if isinstance(c, ast.Call)]


def _strings(node: ast.AST) -> set[str]:
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _mentions_krt(call: ast.Call) -> bool:
    return any("KiCadRoutingTools" in text or "KRT" in text for text in _strings(call))


def _named(args: list[ast.AST]) -> set[str]:
    """What the given arguments name: string constants (a dotted ``"pkg.mod.attr"`` also by its
    last part), attribute names and bare names."""
    found: set[str] = set()
    for arg in args:
        for text in _strings(arg):
            found |= {text, text.rsplit(".", 1)[-1]}
        found |= {n.attr for n in ast.walk(arg) if isinstance(n, ast.Attribute)}
        found |= {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
    return found


def _evidence(fn: ast.AST) -> set[str]:
    """The kinds of control the function's own calls and decorators establish:

    - ``covers:<probe>``: a locator replaced (``monkeypatch.setattr``, ``mock.patch``,
      ``mock.patch.object``; target and attribute, by attribute or by dotted string), for each
      probe built on top of it (``_COVERS``);
    - ``env``: ``KICAD_ROUTING_TOOLS_PATH`` set: named by ``setenv``'s first argument, or by
      ``setitem(os.environ, <name>, ...)``'s second; a value that mentions it does not count;
    - ``env-cleared``: it deleted (``delenv``'s first argument);
    - ``globs``: the plugin-directory globs replaced (``_PLUGIN_GLOBS``).
    """
    kinds: set[str] = set()
    for call in _calls([fn]):
        kind = _name(call.func)
        if kind in _PATCH_CALLS:
            named = _named(call.args[:2])
            for locator in named & _COVERS.keys():
                kinds |= {f"covers:{probe}" for probe in _COVERS[locator]}
            if "_PLUGIN_GLOBS" in named:
                kinds.add("globs")
        elif kind == "setenv" and _named(call.args[:1]) & _ENV_PATH:
            kinds.add("env")
        elif kind == "setitem" and "environ" in _named(call.args[:1]) and _named(call.args[1:2]) & _ENV_PATH:
            kinds.add("env")
        elif kind == "delenv" and _named(call.args[:1]) & _ENV_PATH:
            kinds.add("env-cleared")
    return kinds


def _fixtures(tree: ast.AST) -> dict[str, ast.AST]:
    """Module-level ``@pytest.fixture`` functions by name."""
    found = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if _name(deco.func if isinstance(deco, ast.Call) else deco) == "fixture":
                    found[node.name] = node
    return found


def _requested(fn: ast.AST) -> list[str]:
    """The fixtures a function requests: its parameters and ``@pytest.mark.usefixtures(...)``."""
    names = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
    for deco in fn.decorator_list:
        if isinstance(deco, ast.Call) and _name(deco.func) == "usefixtures":
            names += [a.value for a in deco.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    return names


def _controlled(fn: ast.AST, fixtures: dict[str, ast.AST], probe: str) -> bool:
    """The function, together with every fixture it requests (resolved in the same module and in
    ``tests/conftest.py``, transitively; they all apply to the same test), puts the real install
    out of ``probe``'s decision: it replaces ``probe`` or a locator under it, or sets
    ``KICAD_ROUTING_TOOLS_PATH``, or deletes it **and** replaces the plugin-directory globs. A
    ``delenv`` or a globs patch alone is not control: the other half of discovery still finds a
    real install."""
    kinds: set[str] = set()
    stack, seen = [fn], set()
    while stack:
        node = stack.pop()
        kinds |= _evidence(node)
        for name in _requested(node):
            if name in fixtures and name not in seen:
                seen.add(name)
                stack.append(fixtures[name])
    return bool(kinds & {f"covers:{probe}", "env"}) or {"env-cleared", "globs"} <= kinds


def krt_skip_decisions(source: str, shared: str = "") -> list[str]:
    """Places in one test module that decide on KRT's presence on their own instead of through
    ``tests/_krt.py``:

    - a skip call (``pytest.skip``/``skipif``/``xfail``/``importorskip``,
      ``pytest.skip.Exception``, ``unittest.SkipTest``) whose text names KiCadRoutingTools or KRT;
    - a probe (``check_availability``, ``krt_availability``, ``find_kicad_routing_tools``) called
      at import time (module or class body), which is how a ``skipif`` condition is computed;
    - a probe called in a function that does not put it under its own control
      (``_controlled``): the real install then decides what the function does, whether it
      skips, returns early or branches.

    ``shared`` is the source of ``tests/conftest.py``, whose fixtures count like the module's.

    Known false positives (they fail closed): a fixture imported from another test module, one
    from a plugin, or a class-level fixture is not resolved; move the control into the test, a
    same-module fixture or ``tests/conftest.py``.

    Known blind spots (static evidence, not execution): control counts wherever it appears in
    the function, so a ``setenv`` that points ``KICAD_ROUTING_TOOLS_PATH`` at the real install,
    control under a condition (``if ...: monkeypatch.setenv(...)``), control in a nested helper
    that is never called, and ``monkeypatch.undo()`` after the control all pass; so does a
    class-level fixture that shadows a module-level control fixture of the same name.
    """
    tree = ast.parse(source)
    fixtures = {**_fixtures(ast.parse(shared)), **_fixtures(tree)}
    found = []
    for call in _calls([tree]):
        if _is_skip(call.func) and _mentions_krt(call):
            found.append(f"line {call.lineno}: {_name(call.func)} names KRT")

    def import_time(body, where):
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(stmt, ast.ClassDef):
                import_time(stmt.body, f"class {stmt.name}")
                continue
            for call in _calls([stmt]):
                if _name(call.func) in _PROBES:
                    found.append(f"line {call.lineno}: {_name(call.func)}() at import time ({where})")

    import_time(tree.body, "module")
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for call in _calls(fn.body):
                probe = _name(call.func)
                if probe in _PROBES and not _controlled(fn, fixtures, probe):
                    found.append(f"line {call.lineno}: {fn.name}() probes the real KRT ({probe})")
    return sorted(set(found))


def test_no_test_module_decides_a_krt_skip_on_its_own() -> None:
    shared = (TESTS / "conftest.py").read_text(encoding="utf-8")
    offenders = {
        path.name: hits
        for path in sorted(TESTS.glob("*.py"))
        if path.name not in _EXEMPT
        for hits in [krt_skip_decisions(path.read_text(encoding="utf-8"), shared=shared)]
        if hits
    }
    assert offenders == {}, (
        "route these through the needs_krt marker or the krt_checkout fixture (tests/_krt.py), "
        f"so the gate run fails instead of skipping: {offenders}"
    )


@pytest.mark.parametrize(
    "source",
    [
        # the shapes this ticket replaced (test_rung_backends, test_routing_krt, test_pour_step,
        # test_router_cli_snapshot)
        "KRT_OK = rung_backends.krt_availability()['available']\n",
        "_A = krt.check_availability()\n@pytest.mark.skipif(_A['status'] != 'complete', reason='x')\nclass T:\n    pass\n",
        "needs = pytest.mark.skipif(not ok(), reason='KiCadRoutingTools is not usable here')\n",
        "def _c():\n    try:\n        return krt.find_kicad_routing_tools()\n    except E:\n        pytest.skip('gone')\n",
        "class T:\n    A = krt.check_availability()\n",
        # review round 1: the silent early return, the raised skip forms, delenv without globs
        "def test_x(tmp_path):\n    if krt.check_availability().get('status') != 'complete':\n        return\n",
        "def test_x():\n    raise pytest.skip.Exception('KiCadRoutingTools not installed')\n",
        "def test_x():\n    raise unittest.SkipTest('KRT missing')\n",
        "def test_x():\n    raise SkipTest('KRT missing')\n",
        "def test_x(monkeypatch):\n    monkeypatch.delenv(krt.ENV_PATH)\n    krt.find_kicad_routing_tools()\n",
        # review round 2: the globs patched while the real KICAD_ROUTING_TOOLS_PATH still decides
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(krt, '_PLUGIN_GLOBS', {})\n"
        "    if krt.check_availability().get('status') != 'complete':\n"
        "        return\n",
        # ... also when the globs patch comes from a fixture
        "import pytest\n\n@pytest.fixture\ndef no_globs(monkeypatch):\n"
        "    monkeypatch.setattr(krt, '_PLUGIN_GLOBS', {})\n\n"
        "def test_x(no_globs):\n    if krt.check_availability().get('status') != 'complete':\n        return\n",
        # rung_backends.availability('krt') is a probe too
        "def test_x():\n    if not rung_backends.availability('krt')['available']:\n        return\n",
        # the interpreter locator alone leaves the real checkout deciding
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(krt, 'find_kicad_routing_tools_python', lambda: Path('python'))\n"
        "    if krt.check_availability().get('status') != 'complete':\n"
        "        return\n",
        # review round 3: a replaced probe controls only the probes built on top of it
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(krt, 'krt_availability', lambda: {})\n"
        "    if krt.check_availability().get('status') != 'complete':\n"
        "        return\n",
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(rung_backends, 'availability', lambda name: {})\n"
        "    if krt.check_availability().get('status') != 'complete':\n"
        "        return\n",
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(rung_backends, 'availability', lambda name: {})\n"
        "    if not rung_backends.krt_availability()['available']:\n"
        "        return\n",
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(krt, 'check_availability', lambda: {})\n"
        "    krt.find_kicad_routing_tools()\n",
        # ... and only the variable's name counts, not a value that mentions ENV_PATH
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setenv('UNRELATED', krt.ENV_PATH)\n"
        "    krt.check_availability()\n",
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setitem(os.environ, 'UNRELATED', krt.ENV_PATH)\n"
        "    krt.check_availability()\n",
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setitem(settings, krt.ENV_PATH, 'x')\n"
        "    krt.check_availability()\n",
    ],
    ids=[
        "import-probe", "skipif-class", "reason-text", "probe-and-skip", "class-body",
        "silent-return", "skip-exception", "unittest-skiptest", "bare-skiptest", "delenv-only",
        "globs-only", "globs-only-via-fixture", "rung-backends-availability", "python-locator-only",
        "cross-probe", "cross-probe-availability-over-check", "cross-probe-availability-over-krt",
        "cross-probe-check-over-find", "setenv-value-not-name", "setitem-value-not-name",
        "setitem-not-environ",
    ],
)
def test_the_ratchet_fires_on_each_shape_it_bans(source: str) -> None:
    assert krt_skip_decisions(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(krt, 'check_availability', lambda: {})\n"
        "    assert krt.check_availability()['status'] == 'incomplete'\n",
        "def test_x(tmp_path, monkeypatch):\n"
        "    monkeypatch.setenv(krt.ENV_PATH, str(tmp_path))\n"
        "    assert krt.find_kicad_routing_tools() == tmp_path\n",
        "def test_x(monkeypatch):\n"
        "    monkeypatch.delenv(krt.ENV_PATH, raising=False)\n"
        "    monkeypatch.setattr(krt, '_PLUGIN_GLOBS', {})\n"
        "    assert krt.check_availability()['status'] == 'incomplete'\n",
        # review round 2: hermetic tests that the round-1 rule flagged falsely
        "@mock.patch('kicad_mcp.routers.kicad_routing_tools.find_kicad_routing_tools')\n"
        "def test_x(find):\n    assert krt.check_availability()['status'] == 'incomplete'\n",
        "def test_x():\n"
        "    with mock.patch('kicad_mcp.routers.kicad_routing_tools.check_availability', return_value={}):\n"
        "        krt.check_availability()\n",
        "def test_x():\n"
        "    with mock.patch.object(krt, 'find_kicad_routing_tools', side_effect=krt.KicadRoutingToolsError('x')):\n"
        "        assert krt.check_availability()['status'] == 'incomplete'\n",
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(krt, 'find_kicad_routing_tools_python', lambda: Path('python'))\n"
        "    monkeypatch.setattr(krt, 'find_kicad_routing_tools', lambda: Path('x'))\n"
        "    krt.check_availability()\n",
        "def test_x(monkeypatch, tmp_path):\n"
        "    monkeypatch.setitem(os.environ, 'KICAD_ROUTING_TOOLS_PATH', str(tmp_path))\n"
        "    krt.find_kicad_routing_tools()\n",
        "def test_x(monkeypatch, tmp_path):\n"
        "    monkeypatch.setitem(os.environ, krt.ENV_PATH, str(tmp_path))\n"
        "    krt.check_availability()\n",
        "import pytest\n\n@pytest.fixture\ndef fake_krt(monkeypatch, tmp_path):\n"
        "    monkeypatch.setenv(krt.ENV_PATH, str(tmp_path))\n\n"
        "def test_x(fake_krt):\n    krt.check_availability()\n",
        "import pytest\n\n@pytest.fixture()\ndef outer(inner):\n    return inner\n\n"
        "@pytest.fixture\ndef inner(monkeypatch):\n    monkeypatch.setattr(krt, 'check_availability', lambda: {})\n\n"
        "def test_x(outer):\n    krt.check_availability()\n",
        "import pytest\n\n@pytest.fixture\ndef fake_krt(monkeypatch, tmp_path):\n"
        "    monkeypatch.setenv(krt.ENV_PATH, str(tmp_path))\n\n"
        "@pytest.mark.usefixtures('fake_krt')\ndef test_x():\n    krt.check_availability()\n",
        # review round 3: a replaced probe controls the probes built on top of it
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(krt, 'check_availability', lambda: {})\n"
        "    rung_backends.krt_availability()\n"
        "    rung_backends.availability('krt')\n",
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(rung_backends, 'krt_availability', lambda: {})\n"
        "    rung_backends.availability('krt')\n",
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(krt, 'find_kicad_routing_tools', lambda: Path('x'))\n"
        "    krt.find_kicad_routing_tools()\n"
        "    rung_backends.availability('krt')\n",
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(rung_backends, 'availability', lambda name: {})\n"
        "    rung_backends.availability('krt')\n",
    ],
    ids=[
        "patched-probe", "env-to-fake-checkout", "no-env-no-globs",
        "mock-patch-decorator-dotted", "mock-patch-context-dotted", "patch-object-locator",
        "setattr-locators", "setitem-environ-string", "setitem-environ-attr",
        "same-module-fixture", "fixture-chain", "usefixtures",
        "check-covers-the-probes-above", "krt-availability-covers-availability", "find-covers-all",
        "availability-covers-itself",
    ],
)
def test_the_ratchet_lets_a_unit_test_of_the_probe_through(source: str) -> None:
    assert krt_skip_decisions(source) == []


def test_the_ratchet_resolves_a_fixture_from_the_shared_conftest() -> None:
    conftest = (
        "import pytest\n\n@pytest.fixture\ndef fake_krt(monkeypatch, tmp_path):\n"
        "    monkeypatch.setenv('KICAD_ROUTING_TOOLS_PATH', str(tmp_path))\n"
    )
    test = "def test_x(fake_krt):\n    krt.check_availability()\n"
    assert krt_skip_decisions(test) != []  # unresolved fixture: no control seen
    assert krt_skip_decisions(test, shared=conftest) == []
