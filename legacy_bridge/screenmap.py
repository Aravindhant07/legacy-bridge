"""Screen maps: how to recognise each screen, where its fields are, and how to move between screens."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from .driver import Snapshot


class Anchor(BaseModel):
    row: int
    col: int
    text: str

    def matches(self, s: Snapshot) -> bool:
        return s.text_at(self.row, self.col, len(self.text)) == self.text


class Label(BaseModel):
    col: int
    text: str


class FieldDef(BaseModel):
    row: int
    col: int
    len: int
    input: bool = False
    type: Literal["text", "money", "int", "date"] = "text"
    pattern: str | None = None  # for inputs
    label: Label | None = None  # verified before reading, to detect layout drift
    hidden: bool = False  # passwords: never logged


class ColumnDef(BaseModel):
    col: int
    len: int
    type: Literal["text", "money", "int", "date"] = "text"


class TableDef(BaseModel):
    first_row: int
    last_row: int
    columns: dict[str, ColumnDef]
    key_column: str  # a row is present when this column is non-blank
    next_page: str | None = None  # key that shows the next page
    end_marker: str | None = None  # message text when there are no more pages
    page_indicator: Anchor | None = None


class Edge(BaseModel):
    """How to reach another screen from this one."""

    to: str
    fill: dict[str, str] = Field(default_factory=dict)
    press: str = "ENTER"


class ScreenDef(BaseModel):
    identify: list[Anchor]
    fields: dict[str, FieldDef] = Field(default_factory=dict)
    tables: dict[str, TableDef] = Field(default_factory=dict)
    message_row: int | None = None
    edges: list[Edge] = Field(default_factory=list)
    interrupt: bool = False  # a pop-up that can appear anywhere and is dismissed with `dismiss`
    dismiss: str | None = None
    clear_key: str = "BACKSPACE"  # how to erase an input field (repeated `len` times)

    def inputs(self) -> list[tuple[str, FieldDef]]:
        """Input fields in tab order (top-to-bottom, left-to-right)."""
        return sorted(((n, f) for n, f in self.fields.items() if f.input), key=lambda x: (x[1].row, x[1].col))


class ScreenMap(BaseModel):
    system: str
    screens: dict[str, ScreenDef]
    rows: int = 24
    cols: int = 80

    @classmethod
    def load(cls, path: str | Path) -> "ScreenMap":
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))

    def identify(self, s: Snapshot) -> str | None:
        for name, sd in self.screens.items():
            if all(a.matches(s) for a in sd.identify):
                return name
        return None

    def path(self, start: str, goal: str) -> list[Edge] | None:
        """Shortest sequence of edges from start to goal (breadth-first)."""
        if start == goal:
            return []
        frontier, seen = [(start, [])], {start}
        while frontier:
            nxt = []
            for node, route in frontier:
                for e in self.screens[node].edges:
                    if e.to in seen or e.to not in self.screens:
                        continue
                    if e.to == goal:
                        return route + [e]
                    seen.add(e.to)
                    nxt.append((e.to, route + [e]))
            frontier = nxt
        return None


_MONEY = re.compile(r"^([\d,]+\.\d{2})(-?)$")


def convert(raw: str, kind: str) -> Any:
    v = raw.strip().rstrip("_").strip()
    if kind == "money":
        m = _MONEY.match(v.replace(" ", ""))
        if not m:
            return None if not v else v
        n = float(m.group(1).replace(",", ""))
        return -n if m.group(2) else n
    if kind == "int":
        try:
            return int(v.replace(",", ""))
        except ValueError:
            return None if not v else v
    return v
