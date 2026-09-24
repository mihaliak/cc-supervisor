"""A tiny stdlib JSON-Schema checker for the subset our `schema/*.json` files use (tests only)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def load_schema(name: str) -> dict[str, Any]:
    data = json.loads((SCHEMA_DIR / name).read_text("utf-8"))
    assert isinstance(data, dict)
    return data


def _type_ok(value: Any, name: str) -> bool:
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[name])


def errors(value: Any, schema: dict[str, Any], root: dict[str, Any], where: str = "$") -> list[str]:
    """All violations of `schema` by `value` (empty list = valid)."""
    if "$ref" in schema:
        ref = schema["$ref"]
        assert ref.startswith("#/$defs/"), ref
        return errors(value, root["$defs"][ref.removeprefix("#/$defs/")], root, where)
    out: list[str] = []
    if "oneOf" in schema:
        matches = sum(1 for sub in schema["oneOf"] if not errors(value, sub, root, where))
        if matches != 1:
            out.append(f"{where}: matches {matches} oneOf branches")
        return out
    if "const" in schema and value != schema["const"]:
        out.append(f"{where}: expected const {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{where}: {value!r} not in enum")
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(value, n) for n in names):
            return [*out, f"{where}: {value!r} is not {names}"]
    if isinstance(value, bool):
        return out
    if isinstance(value, int | float):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{where}: {value} < minimum")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{where}: {value} > maximum")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            out.append(f"{where}: shorter than minLength")
        if schema.get("format") == "date-time":
            try:
                datetime.fromisoformat(value)
            except ValueError:
                out.append(f"{where}: not a date-time")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{where}: missing required {key}")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, sub in value.items():
            if key in props:
                out += errors(sub, props[key], root, f"{where}.{key}")
            elif extra is False:
                out.append(f"{where}: unexpected key {key}")
            elif isinstance(extra, dict):
                out += errors(sub, extra, root, f"{where}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            out += errors(item, schema["items"], root, f"{where}[{i}]")
    return out


def validate(value: Any, schema_name: str) -> list[str]:
    root = load_schema(schema_name)
    return errors(value, root, root)
