"""A short hint with an example call when a tool is called with a missing or wrong argument.

The MCP layer validates the arguments before the tool body runs and answers with pydantic's raw
text. ``install`` wraps the server's ``call_tool`` once, for every tool, and rewrites that answer:

* a missing required field: one short message that names the tool and each missing field, says what
  the field is (the first line of its docstring ``Args`` entry, else its type) and shows one example
  call;
* any other validation failure (wrong type and so on): the raw message stays and the example is
  appended.

The wrapper never validates, never changes the arguments and never turns a refusal into an
acceptance: it runs the original call and only changes the text of the error it raises. The example
is built from the tool's own input schema, so it cannot go stale; a test runs every registered
tool's example through that tool's own validation.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

_MAX_DEPTH = 6


def _resolve(schema: Any, root: dict, seen: tuple[str, ...] = ()) -> Any:
    """Follow ``$ref`` chains inside the schema; a cycle or a dangling ref yields ``{}``."""
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen:
            return {}
        seen = seen + (ref,)
        node: Any = root
        for part in ref[2:].split("/"):
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return {}
        schema = node
    return schema if isinstance(schema, dict) else {}


def _value(schema: Any, root: dict, name: str, depth: int) -> Any:
    schema = _resolve(schema, root)
    if depth > _MAX_DEPTH:
        return None
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list) and options:
            non_null = [o for o in options if _resolve(o, root).get("type") != "null"]
            return _value((non_null or options)[0], root, name, depth + 1)
    if isinstance(schema.get("allOf"), list) and schema["allOf"]:
        return _value(schema["allOf"][0], root, name, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "string":
        text = f"<{name}>"
        return text.ljust(int(schema.get("minLength") or 0), "x")
    if kind == "integer":
        low = schema.get("minimum")
        if low is None and schema.get("exclusiveMinimum") is not None:
            low = schema["exclusiveMinimum"] + 1
        return int(low) if low is not None else 1
    if kind == "number":
        low = schema.get("minimum")
        if low is None and schema.get("exclusiveMinimum") is not None:
            low = schema["exclusiveMinimum"] + 1
        return low if low is not None else 1
    if kind == "boolean":
        return False
    if kind == "array":
        if int(schema.get("minItems") or 0) >= 1:
            return [_value(schema.get("items", {}), root, name, depth + 1)] * int(schema["minItems"])
        return []
    if kind == "object":
        return example_arguments(schema, root, depth + 1) if schema.get("required") else {}
    return None


def example_arguments(schema: dict, root: dict | None = None, depth: int = 0) -> dict:
    """Placeholder values for the required fields of an input schema."""
    root = schema if root is None else root
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    out: dict[str, Any] = {}
    for name in schema.get("required") or []:
        out[name] = _value(props.get(name, {}), root, str(name), depth)
    return out


def _render(value: Any) -> str:
    if isinstance(value, (str, list, dict)) or value is None:
        try:
            return json.dumps(value)
        except (TypeError, ValueError):
            return "..."
    return repr(value)


def example_call(tool: str, schema: dict) -> str:
    """One example call, ``tool(name=value, ...)``, with the required fields only."""
    try:
        args = example_arguments(schema if isinstance(schema, dict) else {})
    except Exception:  # noqa: BLE001 - a hint must never raise
        args = {}
    return f"{tool}(" + ", ".join(f"{k}={_render(v)}" for k, v in args.items()) + ")"


def _docstring_args(fn: Any) -> dict[str, str]:
    """First line of each ``Args`` entry of the tool's docstring."""
    try:
        doc = inspect.getdoc(fn) or ""
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    in_args = False
    for line in doc.splitlines():
        stripped = line.strip()
        if not in_args:
            in_args = stripped in ("Args:", "Arguments:", "Parameters:")
            continue
        if line and not line[0].isspace():
            break
        if line.startswith("    ") and not line.startswith("     ") or (":" in stripped and line.startswith("  ") and not line.startswith("   ")):
            name, sep, rest = stripped.partition(":")
            name = name.split("(")[0].strip()
            if sep and name.isidentifier():
                out[name] = rest.strip()
    return out


def _type_text(schema: Any, root: dict) -> str:
    schema = _resolve(schema, root)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = " or ".join(str(k) for k in kind)
    if not kind:
        options = [o for key in ("anyOf", "oneOf") for o in schema.get(key, []) if isinstance(o, dict)]
        kind = " or ".join(dict.fromkeys(str(_resolve(o, root).get("type", "value")) for o in options)) or "value"
    return str(kind)


_MAX_DESCRIPTION = 100


def _short(line: str) -> str:
    """The line cut at a sentence end or a maximum length; empty when it would end mid-expression."""
    if len(line) > _MAX_DESCRIPTION:
        cut = line.rfind(". ", 0, _MAX_DESCRIPTION)
        line = line[: cut + 1] if cut > 0 else ""
    line = line.rstrip(".").strip()
    balanced = (
        line.count("`") % 2 == 0
        and line.count('"') % 2 == 0
        and all(line.count(o) == line.count(c) for o, c in ("()", "[]", "{}"))
    )
    return line if balanced else ""


def _describe(name: str, tool: Any) -> str:
    schema = tool.parameters if isinstance(getattr(tool, "parameters", None), dict) else {}
    prop = (schema.get("properties") or {}).get(name, {})
    resolved = _resolve(prop, schema)
    text = str(resolved.get("description") or prop.get("description") or "").strip()
    if not text:
        text = _docstring_args(getattr(tool, "fn", None)).get(name, "")
    first = _short(text.splitlines()[0].strip()) if text else ""
    return first or _type_text(prop, schema)


def _hint_message(name: str, tool: Any, exc: ValidationError) -> str:
    schema = tool.parameters if isinstance(getattr(tool, "parameters", None), dict) else {}
    example = example_call(name, schema)
    errors = exc.errors()
    missing = list(dict.fromkeys(str(e["loc"][0]) for e in errors if e.get("type") == "missing" and len(e["loc"]) == 1))
    if missing and len(missing) == len(errors):
        parts = [f"`{field}` ({_describe(field, tool)})" for field in missing]
        noun = "field" if len(parts) == 1 else "fields"
        return (
            f"Error executing tool {name}: missing required {noun} {', '.join(parts)}. "
            f"Example call: {example}"
        )
    return f"Error executing tool {name}: {exc}\nExample call: {example}"


def install(mcp: Any) -> None:
    """Wrap ``mcp.call_tool`` so a validation failure carries the hint (once, for every tool)."""
    original = mcp.call_tool

    async def call_tool(name, arguments, *args, **kwargs):
        try:
            return await original(name, arguments, *args, **kwargs)
        except ToolError as exc:
            cause = exc.__cause__
            if not isinstance(cause, ValidationError) or type(exc) is not ToolError:
                raise
            try:
                tool = mcp._tool_manager.get_tool(name)
                message = _hint_message(name, tool, cause) if tool is not None else None
            except Exception:  # noqa: BLE001 - a hint must never hide the original error
                message = None
            if message is None:
                raise
            raise ToolError(message) from cause

    mcp.call_tool = call_tool
