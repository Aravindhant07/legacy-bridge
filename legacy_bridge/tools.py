"""Tool definitions: typed inputs and a flow of screen steps."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


class InputDef(BaseModel):
    type: Literal["string", "integer", "number", "boolean"] = "string"
    pattern: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    default: Any = None
    description: str = ""


class ConfirmDef(BaseModel):
    screen: str
    key: str
    cancel: str
    result: list[str]
    fail_if_screen: dict[str, str] = Field(default_factory=dict)
    read: list[str] = Field(default_factory=list)


class ToolDef(BaseModel):
    description: str
    mode: Literal["read", "write"] = "read"
    inputs: dict[str, InputDef] = Field(default_factory=dict)
    steps: list[dict[str, Any]]
    confirm: ConfirmDef | None = None

    def json_schema(self) -> dict[str, Any]:
        props, required = {}, []
        for n, i in self.inputs.items():
            p: dict[str, Any] = {"type": i.type, "description": i.description}
            if i.pattern:
                p["pattern"] = i.pattern
            if i.minimum is not None:
                p["minimum"] = i.minimum
            if i.maximum is not None:
                p["maximum"] = i.maximum
            if i.default is not None:
                p["default"] = i.default
            else:
                required.append(n)
            props[n] = p
        return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


class ToolSet(BaseModel):
    system: str
    tools: dict[str, ToolDef]

    @classmethod
    def load(cls, path: str | Path) -> "ToolSet":
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))


class InvalidInput(ValueError):
    pass


def validate_inputs(tool: ToolDef, args: dict[str, Any]) -> dict[str, Any]:
    """Check arguments against the tool's input definitions; fill defaults."""
    unknown = set(args) - set(tool.inputs)
    if unknown:
        raise InvalidInput(f"unknown argument(s): {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for name, d in tool.inputs.items():
        if name not in args or args[name] is None:
            if d.default is None:
                raise InvalidInput(f"missing required argument '{name}'")
            out[name] = d.default
            continue
        v = args[name]
        if d.type == "integer":
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise InvalidInput(f"'{name}' must be an integer")
        elif d.type == "number":
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise InvalidInput(f"'{name}' must be a number")
        else:
            v = str(v)
        if d.minimum is not None and v < d.minimum or d.maximum is not None and v > d.maximum:
            raise InvalidInput(f"'{name}' must be between {d.minimum} and {d.maximum}")
        if d.pattern and not re.fullmatch(d.pattern, str(v)):
            raise InvalidInput(f"'{name}' does not match the required format {d.pattern}")
        out[name] = v
    return out
