import json
from pathlib import Path

import pytest

from legacy_bridge.audit import AuditLog
from legacy_bridge.bridge import Bridge
from legacy_bridge.demo.data import Bank
from legacy_bridge.driver import VTDriver, demo_bank_argv
from legacy_bridge.screenmap import ScreenMap
from legacy_bridge.tools import ToolSet

ROOT = Path(__file__).resolve().parents[1]
MAP = ScreenMap.load(ROOT / "maps" / "northwind_bank.yaml")
TOOLS = ToolSet.load(ROOT / "tools" / "northwind_bank.yaml")
ENV = {"PYTHONPATH": str(ROOT)}


def make_bridge(tmp_path, *flags, writes="confirm", relocate=False, creds=("TELLER01", "PASS2024")):
    return Bridge(MAP, TOOLS, lambda: VTDriver(demo_bank_argv(*flags), env=ENV), AuditLog(tmp_path / f"audit{abs(hash(flags))}.jsonl"),
                  credentials=creds, writes=writes, relocate_drift=relocate)


@pytest.fixture(scope="module")
def accounts():
    b = Bank()
    active = [a.number for a in b.accounts.values() if a.status == "ACTIVE" and a.available > 500]
    frozen = next(a.number for a in b.accounts.values() if a.status == "FROZEN")
    return {"active": active, "frozen": frozen, "bank": b}


@pytest.fixture
def bridge(tmp_path):
    br = make_bridge(tmp_path)
    yield br
    br.close()


def audit_events(br):
    return [r["event"] for r in br.audit.records()]


def audit_text(br):
    return json.dumps(br.audit.records())
