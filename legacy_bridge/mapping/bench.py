"""Benchmark: draft screen maps for the demo screens and compare with the hand-written map."""

from __future__ import annotations

from typing import Any

from ..bridge import Bridge
from ..driver import Snapshot
from .assist import draft_screen, name_fields, score_against


def capture_demo_screens(br: Bridge, accounts: list[str]) -> dict[str, Snapshot]:
    """Drive the demo app into a representative state for each screen."""
    caps: dict[str, Snapshot] = {}
    caps["SIGNON"] = br.where()[1]
    br.goto("MAIN")
    caps["MAIN"] = br.where()[1]
    br.goto("ACIN01")
    br._fill("ACIN01", {"account_no": accounts[0]})
    br._press("ENTER")
    caps["ACIN01"] = br._expect(["ACIN01"])[1]
    br.goto("CUSR01")
    br._fill("CUSR01", {"last_name": "PATEL"})
    br._press("ENTER")
    caps["CUSR01"] = br._expect(["CUSR01"])[1]
    br.goto("TXHS01")
    br._fill("TXHS01", {"account_no": accounts[0]})
    br._press("ENTER")
    caps["TXHS01"] = br._expect(["TXHS01"])[1]
    br.goto("FNTR01")
    caps["FNTR01"] = br.where()[1]
    br._fill("FNTR01", {"from_account": accounts[0], "to_account": accounts[1], "amount": "5", "memo": "X"})
    br._press("ENTER")
    caps["FNTR02"] = br._expect(["FNTR02"])[1]
    br._press("F10")
    caps["FNTR03"] = br._expect(["FNTR03"])[1]
    return caps


def table_score(draft: dict[str, Any], truth_screen) -> float | None:
    """Share of true table columns found at the same column position."""
    if not truth_screen.tables:
        return None
    truth_cols = {c.col for t in truth_screen.tables.values() for c in t.columns.values()}
    got = {c["col"] for t in draft.get("tables", {}).values() for c in t["columns"].values()}
    return len(truth_cols & got) / len(truth_cols)


def run(br: Bridge, accounts: list[str], backend=None) -> dict[str, Any]:
    caps = capture_demo_screens(br, accounts)
    per_screen, tot = {}, {"truth": 0, "matched": 0, "proposed": 0}
    for sid, snap in caps.items():
        draft = draft_screen(snap, sid)
        notes: list[str] = []
        if backend is not None:
            draft, notes = name_fields(snap, draft, backend)
        s = score_against(draft, br.map.screens[sid])
        s["table_columns"] = table_score(draft, br.map.screens[sid])
        s["names"] = list(draft["fields"])
        s["notes"] = notes
        per_screen[sid] = s
        for k in tot:
            tot[k] += s[k]
    return {
        "namer": getattr(backend, "name", "none (layout only)"),
        "field_recall": tot["matched"] / tot["truth"],
        "field_precision": tot["matched"] / tot["proposed"] if tot["proposed"] else 1.0,
        "screens": per_screen,
    }
