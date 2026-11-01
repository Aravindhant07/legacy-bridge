"""Run the graded banking tasks with a model, through the MCP server, against a fresh demo system."""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import anyio

from .agent import ChatModel, build_tasks, run_agent
from .audit import AuditLog
from .bridge import Bridge
from .demo.data import Bank
from .driver import VTDriver, demo_bank_argv
from .mcp_server import build_server
from .screenmap import ScreenMap
from .tools import ToolSet

ROOT = Path(__file__).resolve().parents[1]


def fresh_bridge(audit_dir: Path) -> Bridge:
    return Bridge(ScreenMap.load(ROOT / "maps/northwind_bank.yaml"), ToolSet.load(ROOT / "tools/northwind_bank.yaml"),
                  lambda: VTDriver(demo_bank_argv(), env={"PYTHONPATH": str(ROOT)}), AuditLog(audit_dir / "audit.jsonl"),
                  credentials=("TELLER01", "PASS2024"))


def evaluate(model: ChatModel, out_dir: str | Path | None = None, only: list[str] | None = None, on_task=None) -> dict[str, Any]:
    bank = Bank()  # same seed as the demo app, so expected answers match what the screens show
    tasks = [t for t in build_tasks(bank) if not only or t.id in only]
    results = []
    tmp = Path(tempfile.mkdtemp())
    for task in tasks:
        br = fresh_bridge(tmp / task.id)  # each task starts from a fresh system
        trace = anyio.run(run_agent, build_server(br, rate_per_minute=0), model, task.prompt)
        # Inspect the system state after the run, through the bridge itself.
        state: dict[str, Any] = {"balances": {}}
        accts = [a for a in bank.accounts.values() if a.status == "ACTIVE" and a.available > 500]
        frozen = next(a for a in bank.accounts.values() if a.status == "FROZEN")
        for n in (accts[5].number, accts[6].number):
            state["balances"][n] = br.call("get_account", {"account_no": n})["balance"]
        state["frozen_unchanged"] = br.call("get_account", {"account_no": frozen.number})["balance"] == frozen.balance
        br.close()
        passed = bool(task.check(trace, state))
        results.append({"task": task.id, "passed": passed, **asdict(trace)})
        if on_task:
            on_task(task.id, passed, trace)
    summary = {
        "model": model.name,
        "passed": sum(r["passed"] for r in results),
        "total": len(results),
        "tool_calls": sum(len(r["calls"]) for r in results),
        "tool_errors": sum(1 for r in results for c in r["calls"] if c["error"]),
        "seconds": round(sum(r["seconds"] for r in results), 1),
        "results": results,
    }
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        slug = model.name.replace("/", "_").replace(":", "_")
        (out / f"agent_eval_{slug}.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary
