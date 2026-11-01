"""Draft screen maps from a captured screen.

Positions come from code, never from a model:
  * labels are text runs ending in ':' (with optional dot leaders ". . . :");
  * a value starts after the label and runs to the next wide gap or the next label;
  * input fields are runs of '_' (how most terminal apps draw empty fields).
A model is optionally used only for meaning: a snake_case name and a type per label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..driver import Snapshot

LABEL = re.compile(r"([A-Z][A-Z0-9 /#.&()-]*?[A-Z0-9)#])(?:\s*(?:\. )+\.?|\s*)\s*:")
MONEY = re.compile(r"^-?[\d,]+\.\d{2}-?$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class ProposedField:
    name: str
    label: str
    row: int
    label_col: int
    col: int
    len: int
    input: bool
    type: str = "text"


def snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def guess_type(value: str) -> str:
    v = value.strip()
    if MONEY.match(v):
        return "money"
    if DATE.match(v):
        return "date"
    if v.isdigit() and len(v) < 10:
        return "int"
    return "text"


def find_fields(snap: Snapshot, skip_rows: tuple[int, ...] = (1, 2, 23, 24)) -> list[ProposedField]:
    """Label/value pairs found by layout alone."""
    out: list[ProposedField] = []
    for r, line in enumerate(snap.lines, 1):
        if r in skip_rows:
            continue
        for m in LABEL.finditer(line):
            label = m.group(1).strip()
            start = m.end()
            # value: skip spaces after the colon, then read until a gap of 3+ spaces, the next label, or end of line
            rest = line[start:]
            lead = len(rest) - len(rest.lstrip(" "))
            first = rest.lstrip(" ").split("  ")[0]
            if lead > 3:
                # right-aligned numbers sit far from their label: the field starts one space after the colon
                if not (first and re.match(r"^-?[\d,]+(\.\d+)?-?$", first) and lead < 25):
                    continue
                vstart = start + 1
            else:
                vstart = start + lead
            nxt = LABEL.search(line, vstart)
            body = line[vstart:(nxt.start() if nxt else len(line))]
            content_start = len(body) - len(body.lstrip(" "))
            wide_gap = re.search(r"\s{3,}", body[content_start:])
            content_end = content_start + (wide_gap.start() if wide_gap else len(body[content_start:].rstrip()))
            vend = vstart + content_end
            raw = line[vstart:vend].strip()
            is_input = bool(raw) and set(raw.rstrip()) <= {"_"} or raw.endswith("_")
            if is_input:
                vend = vstart + len(re.match(r"[^_]*_*", line[vstart:]).group(0))
            if vend <= vstart:
                continue
            out.append(ProposedField(snake(label), label, r, m.start() + 1, vstart + 1, vend - vstart, is_input,
                                     "text" if is_input else guess_type(raw)))
    return out


def find_table(snap: Snapshot, header_row: int | None = None) -> dict[str, Any] | None:
    """A header row of 3+ upper-case words separated by wide gaps, followed by data rows."""
    for r, line in enumerate(snap.lines, 1):
        if header_row and r != header_row:
            continue
        cols = [(m.start() + 1, m.group(0).strip()) for m in re.finditer(r"[A-Z][A-Z ]*?(?=\s{2,}|$)", line)]
        if len(cols) < 3 or ":" in line or r in (1, 2, 23, 24):
            continue
        data_rows = [i for i in range(r + 1, len(snap.lines)) if snap.lines[i - 1].strip() and i < 22]
        if not data_rows:
            continue
        columns = {}
        for i, (c, name) in enumerate(cols):
            end = cols[i + 1][0] if i + 1 < len(cols) else max(len(snap.lines[j - 1].rstrip()) for j in data_rows) + 1
            sample = snap.text_at(data_rows[0], c, end - c)
            columns[snake(name)] = {"col": c, "len": end - c, "type": guess_type(sample.strip())}
        return {"header_row": r, "first_row": r + 1, "last_row": max(data_rows), "columns": columns}
    return None


def draft_screen(snap: Snapshot, screen_id: str | None = None) -> dict[str, Any]:
    """A draft ScreenDef (as a dict) for the screen on display."""
    sid = screen_id or snap.lines[0].split()[0]
    fields = {}
    for f in find_fields(snap):
        d: dict[str, Any] = {"row": f.row, "col": f.col, "len": f.len}
        if f.input:
            d["input"] = True
        else:
            d["label"] = {"col": f.label_col, "text": f.label + snap.lines[f.row - 1][f.label_col - 1 + len(f.label): f.col - 1].rstrip()}
            if f.type != "text":
                d["type"] = f.type
        name, n = f.name, 2
        while name in fields:
            name, n = f"{f.name}_{n}", n + 1
        fields[name] = d
    out: dict[str, Any] = {"identify": [{"row": 1, "col": snap.lines[0].find(sid) + 1, "text": sid}], "fields": fields, "message_row": 23}
    t = find_table(snap)
    if t:
        cols = t.pop("columns")
        key = next(iter(cols))
        out["tables"] = {"rows": {"first_row": t["first_row"], "last_row": t["last_row"], "key_column": key, "columns": cols}}
    return out


# -- optional model step: names and types only ----------------------------

NAME_SCHEMA = {
    "type": "object",
    "properties": {"fields": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "name": {"type": "string"}, "type": {"type": "string", "enum": ["text", "money", "int", "date"]}},
        "required": ["label", "name", "type"], "additionalProperties": False}}},
    "required": ["fields"], "additionalProperties": False,
}


def name_fields(snap: Snapshot, draft: dict[str, Any], backend) -> tuple[dict[str, Any], list[str]]:
    """Ask a model for better field names/types. Only labels that exist in the draft are accepted."""
    labels = {(v.get("label") or {}).get("text", k).split(":")[0].strip(" ."): k for k, v in draft["fields"].items()}
    system = ("You name the fields of a legacy terminal screen for an API. For each label give a short snake_case name "
              "and a type (text, money, int, date). Answer only with JSON.")
    user = "Screen:\n" + snap.render() + "\n\nLabels:\n" + "\n".join(f"- {lab}" for lab in labels)
    r = backend.complete(system, user, NAME_SCHEMA)
    notes, renamed = [], {}
    for item in r.data.get("fields", []):
        lab = str(item.get("label", "")).split(":")[0].strip(" .")
        if lab not in labels:
            notes.append(f"ignored unknown label '{lab}'")
            continue
        old = labels[lab]
        new = snake(str(item.get("name", ""))) or old
        if new in renamed.values():
            new = old
        renamed[old] = new
        if item.get("type") in ("money", "int", "date") and not draft["fields"][old].get("input"):
            draft["fields"][old]["type"] = item["type"]
    draft["fields"] = {renamed.get(k, k): v for k, v in draft["fields"].items()}
    return draft, notes


def score_against(draft: dict[str, Any], truth: dict[str, Any]) -> dict[str, float]:
    """Position recall/precision of a draft against a hand-written screen definition."""
    t = [(f.row, f.col) for f in truth.fields.values()]
    d = [(v["row"], v["col"]) for v in draft["fields"].values()]
    hit = len(set(t) & set(d))
    ttypes = {(f.row, f.col): f.type for f in truth.fields.values()}
    type_ok = sum(1 for v in draft["fields"].values() if ttypes.get((v["row"], v["col"])) == v.get("type", "text"))
    return {"truth": len(t), "proposed": len(d), "matched": hit,
            "recall": hit / len(t) if t else 1.0, "precision": hit / len(d) if d else 1.0,
            "type_accuracy": type_ok / hit if hit else 0.0}
