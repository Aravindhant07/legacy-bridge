"""Append-only, hash-chained audit log (JSON lines). Editing or deleting any record breaks the chain."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _hash(prev: str, record: dict[str, Any]) -> str:
    body = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev + body).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._prev, self._seq = GENESIS, 0
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self._prev, self._seq = rec["hash"], rec["seq"]

    def write(self, event: str, **data: Any) -> None:
        with self._lock:
            self._seq += 1
            rec = {"seq": self._seq, "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": event, "data": data}
            h = _hash(self._prev, rec)
            with open(self.path, "a") as fh:
                fh.write(json.dumps({**rec, "prev": self._prev, "hash": h}, default=str) + "\n")
            self._prev = h

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


def verify(path: str | Path) -> tuple[bool, int, str]:
    """Return (ok, number of records, reason)."""
    prev, n, expected_seq = GENESIS, 0, 1
    p = Path(path)
    if not p.exists():
        return True, 0, "empty"
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        n += 1
        if rec.get("seq") != expected_seq:
            return False, n, f"sequence gap at record {n}"
        if rec.get("prev") != prev:
            return False, n, f"record {n} does not follow the previous record (deleted or reordered)"
        body = {k: rec[k] for k in ("seq", "ts", "event", "data")}
        if _hash(prev, body) != rec.get("hash"):
            return False, n, f"record {n} was edited"
        prev, expected_seq = rec["hash"], expected_seq + 1
    return True, n, "intact"
