"""Adapt AgL schemas to the native agents' shared structured-output subset."""

from __future__ import annotations

import json
from typing import cast

from agnette.agent.spec import AgentClaude, AgentCodex, AgentSpec
from agnette.util.decimal import reject_json_constant
from agnette.util.unicode import json_object_unique, loads_json


def native_output_schema(agent: AgentSpec, schema: str) -> str | None:
    """Wrap supported schemas in an object; preserve unsupported contracts via prompts.

    Open objects, unconstrained JSON, and optional fields cannot be represented
    faithfully in the strict subset. Generated enum alternatives are disjoint,
    so replacing oneOf with anyOf preserves their accepted values.
    """
    if not isinstance(agent, (AgentClaude, AgentCodex)):
        return None
    adapted = _adapt(cast(dict[str, object], loads_json(schema)))
    if adapted is None:
        return None
    definitions = adapted.pop("$defs", None)
    envelope: dict[str, object] = {
        "type": "object",
        "properties": {"result": adapted},
        "required": ["result"],
        "additionalProperties": False,
    }
    if definitions is not None:
        envelope["$defs"] = definitions
    return json.dumps(envelope, ensure_ascii=False)


def _adapt(schema: dict[str, object]) -> dict[str, object] | None:
    if not schema:
        return None
    result = dict(schema)
    if schema.get("type") == "object":
        if schema.get("additionalProperties") is not False:
            return None
        properties = cast(dict[str, object], schema["properties"])
        if set(cast(list[str], schema["required"])) != set(properties):
            return None
    for key in ("properties", "$defs"):
        if key in schema:
            children: dict[str, object] = {}
            for name, child in cast(dict[str, dict[str, object]], schema[key]).items():
                adapted = _adapt(child)
                if adapted is None:
                    return None
                children[name] = adapted
            result[key] = children
    if "items" in schema:
        item = _adapt(cast(dict[str, object], schema["items"]))
        if item is None:
            return None
        result["items"] = item
    if "oneOf" in schema:
        alternatives: list[object] = []
        for child in cast(list[dict[str, object]], schema["oneOf"]):
            alternative = _adapt(child)
            if alternative is None:
                return None
            alternatives.append(alternative)
        del result["oneOf"]
        result["anyOf"] = alternatives
    if "const" in schema:
        result["enum"] = [result.pop("const")]
    if "enum" in result:
        result["type"] = "string"
    return result


def json_field(output: str, field: str) -> str:
    """Extract a top-level JSON field without rounding numbers."""
    # Leave numeric range validation to AgL so it can retry invalid answers.
    value = loads_json(
        output,
        parse_float=str,
        parse_constant=reject_json_constant,
        object_pairs_hook=json_object_unique,
    )
    if not isinstance(value, dict) or field not in value:
        raise ValueError(f"Native structured output has no {field} field")
    decoder = json.JSONDecoder()
    position = output.index("{") + 1
    while True:
        position += len(output[position:]) - len(output[position:].lstrip())
        key, end = cast(tuple[object, int], decoder.raw_decode(output, position))
        position = output.index(":", end) + 1
        position += len(output[position:]) - len(output[position:].lstrip())
        _, end = cast(tuple[object, int], decoder.raw_decode(output, position))
        if key == field:
            return output[position:end]
        position = output.index(",", end) + 1


def unwrap_native_output(output: str) -> str:
    """Extract the native response envelope."""
    return json_field(output, "result")
