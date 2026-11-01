"""The bridge: runs tool flows against a terminal application, safely.

Every step verifies where it is before acting. Anything unexpected (an unknown
screen, an error message, a field that is not where the map says, a timeout)
stops the tool with a structured error that includes the screen text.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .audit import AuditLog
from .driver import Snapshot, TerminalDriver
from .screenmap import FieldDef, ScreenDef, ScreenMap, convert
from .tools import InvalidInput, ToolDef, ToolSet, validate_inputs


class BridgeError(Exception):
    """A tool could not complete. `kind` is machine-readable; `screen` is the terminal text."""

    def __init__(self, kind: str, message: str, screen: str = "", screen_id: str | None = None):
        super().__init__(message)
        self.kind, self.message, self.screen, self.screen_id = kind, message, screen, screen_id

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.kind, "message": self.message, "screen_id": self.screen_id, "screen": self.screen}


@dataclass
class Pending:
    code: str
    tool: str
    args: dict[str, Any]
    preview: dict[str, Any]
    created: float = field(default_factory=time.monotonic)


class Bridge:
    def __init__(
        self,
        screen_map: ScreenMap,
        toolset: ToolSet,
        driver_factory: Callable[[], TerminalDriver],
        audit: AuditLog,
        credentials: tuple[str, str] | None = None,
        writes: str = "confirm",  # off | confirm | on
        timeout: float = 8.0,
        relocate_drift: bool = False,
        confirm_ttl: float = 300.0,
    ):
        self.map, self.toolset = screen_map, toolset
        self.driver_factory, self.audit = driver_factory, audit
        self.credentials = credentials or (os.environ.get("BANK_USER", ""), os.environ.get("BANK_PASS", ""))
        self.writes, self.timeout, self.relocate_drift, self.confirm_ttl = writes, timeout, relocate_drift, confirm_ttl
        self.driver: TerminalDriver | None = None
        self.pending: Pending | None = None
        self._lock = threading.Lock()  # one terminal session: one tool at a time

    # -- session ---------------------------------------------------------
    def _ensure_driver(self) -> TerminalDriver:
        if self.driver is None or not getattr(self.driver, "alive", True):
            self.driver = self.driver_factory()
            self.driver.start()
            self.audit.write("session.start")
        return self.driver

    def close(self) -> None:
        if self.driver:
            self.driver.close()
            self.driver = None
            self.audit.write("session.end")

    def _fail(self, kind: str, message: str, snap: Snapshot | None = None) -> BridgeError:
        sid = self.map.identify(snap) if snap else None
        err = BridgeError(kind, message, snap.render() if snap else "", sid)
        self.audit.write("tool.error", kind=kind, message=message, screen_id=sid)
        return err

    def where(self) -> tuple[str, Snapshot]:
        """Identify the current screen, dismissing known interrupts (pop-ups) first."""
        d = self._ensure_driver()
        snap = d.wait_for(lambda s: self.map.identify(s) is not None, timeout=self.timeout)
        for _ in range(3):
            if snap is None:
                raise self._fail("unknown_screen", "the terminal is showing a screen that is not in the map", d.snapshot())
            sid = self.map.identify(snap)
            sd = self.map.screens[sid]
            if not sd.interrupt:
                return sid, snap
            self.audit.write("interrupt.dismissed", screen_id=sid, text=snap.render()[:400])
            d.press(sd.dismiss or "ENTER")
            snap = d.wait_for(lambda s: (x := self.map.identify(s)) is not None and x != sid, timeout=self.timeout)
        raise self._fail("interrupt_loop", "pop-up screens kept appearing", snap)

    def sign_on(self) -> None:
        user, pw = self.credentials
        if not user or not pw:
            raise BridgeError("no_credentials", "set the system credentials (e.g. BANK_USER / BANK_PASS)")
        self._fill("SIGNON", {"user": user, "password": pw})
        self._press("ENTER")
        sid, snap = self._expect(["MAIN", "SIGNON"])
        if sid == "SIGNON":
            raise self._fail("sign_on_failed", self._message("SIGNON", snap) or "sign-on rejected", snap)
        self.audit.write("session.signed_on", user=user)

    def goto(self, target: str) -> Snapshot:
        sid, snap = self.where()
        if self.pending and sid == self.pending_screen():
            self._cancel_pending("another tool needed the terminal")
            sid, snap = self.where()
        if sid == "SIGNON":
            msg = self._message("SIGNON", snap)
            if msg:
                self.audit.write("session.recover", reason=msg)
            self.sign_on()
            sid, snap = self.where()
        route = self.map.path(sid, target)
        if route is None:
            raise self._fail("no_route", f"no known way from {sid} to {target}", snap)
        for edge in route:
            if edge.fill:
                self._fill(sid, edge.fill)
            self._press(edge.press)
            sid, snap = self._expect([edge.to])
        return snap

    # -- primitives (each verifies the screen first) ---------------------
    def _require(self, screen: str) -> Snapshot:
        sid, snap = self.where()
        if sid == "SIGNON" and screen != "SIGNON":
            raise self._fail("session_expired", self._message("SIGNON", snap) or "the session returned to the sign-on screen", snap)
        if sid != screen:
            raise self._fail("wrong_screen", f"expected {screen} but the terminal shows {sid}", snap)
        return snap

    def _field_at_cursor(self, sd: ScreenDef, snap: Snapshot) -> str | None:
        r, c = snap.cursor
        for name, f in sd.inputs():
            if f.row == r and f.col <= c < f.col + f.len:
                return name
        return None

    def _focus(self, screen: str, name: str) -> None:
        sd = self.map.screens[screen]
        d = self._ensure_driver()
        for _ in range(len(sd.inputs()) + 2):
            snap = self._require(screen)
            if self._field_at_cursor(sd, snap) == name:
                return
            before = snap.cursor
            d.press("TAB")
            d.wait_for(lambda s: s.cursor != before, timeout=self.timeout)
        raise self._fail("focus_failed", f"could not move the cursor to field '{name}' on {screen}", d.snapshot())

    def _fill(self, screen: str, values: dict[str, str]) -> None:
        sd = self.map.screens[screen]
        d = self._ensure_driver()
        for name, value in values.items():
            f = sd.fields.get(name)
            if f is None or not f.input:
                raise self._fail("bad_map", f"{screen} has no input field '{name}'")
            value = str(value)
            if len(value) > f.len:
                raise self._fail("invalid_input", f"'{name}' is longer than the field ({f.len})")
            if f.pattern and value and not re.fullmatch(f.pattern, value):
                raise self._fail("invalid_input", f"'{name}' does not match {f.pattern}")
            self._focus(screen, name)
            snap = d.snapshot()
            current = snap.text_at(f.row, f.col, f.len).rstrip("_ ").rstrip()
            if current:
                d.send("\x7f" * len(current) if sd.clear_key == "BACKSPACE" else "")
                d.wait_for(lambda s: s.text_at(f.row, f.col, f.len).strip("_ ") == "", timeout=self.timeout)
            if value:
                d.send(value)
                expect = value.upper() if not f.hidden else "*" * len(value)
                ok = d.wait_for(lambda s: s.text_at(f.row, f.col, f.len).rstrip("_ ").upper() == expect.upper(), timeout=self.timeout)
                if ok is None:
                    self._require(screen)  # raises session_expired if the system signed us off
                    raise self._fail("type_failed", f"typed value did not appear in '{name}'", d.snapshot())
            self.audit.write("key.fill", screen=screen, field=name, value="***" if f.hidden else value)

    def _press(self, key: str) -> None:
        self._ensure_driver().press(key)
        self.audit.write("key.press", key=key)

    def _expect(self, screens: list[str]) -> tuple[str, Snapshot]:
        d = self._ensure_driver()
        interrupts = {n for n, s in self.map.screens.items() if s.interrupt}
        deadline = time.monotonic() + self.timeout
        # First wait for the application's response to the key just pressed, so the
        # screen that was showing *before* the key is never mistaken for the result.
        d.settle(quiet=0.12, timeout=self.timeout, response_wait=self.timeout)
        while True:
            snap = d.wait_for(lambda s: self.map.identify(s) in set(screens) | interrupts, timeout=max(0.1, deadline - time.monotonic()))
            if snap is None:
                cur = d.snapshot()
                raise self._fail("unexpected_screen", f"expected {' or '.join(screens)}, got {self.map.identify(cur) or 'an unknown screen'}", cur)
            snap = d.settle(quiet=0.1, timeout=2, response_wait=0.0)
            sid = self.map.identify(snap)
            if sid in interrupts:
                sid, snap = self.where()
                if sid in screens:
                    return sid, snap
                continue
            if sid in screens:
                return sid, snap
            if sid == "SIGNON":
                raise self._fail("session_expired", self._message("SIGNON", snap) or "signed off", snap)

    def _message(self, screen: str, snap: Snapshot) -> str:
        row = self.map.screens[screen].message_row
        return snap.text_at(row, 1, self.map.cols).strip() if row else ""

    def _offset(self, screen: str, name: str, f: FieldDef, snap: Snapshot) -> int:
        """Verify a field's label; return the column offset if the layout drifted (or raise)."""
        if not f.label or snap.text_at(f.row, f.label.col, len(f.label.text)) == f.label.text:
            return 0
        line = snap.lines[f.row - 1]
        found = line.find(f.label.text)
        if found >= 0 and self.relocate_drift:
            off = found + 1 - f.label.col
            self.audit.write("drift.relocated", screen=screen, field=name, offset=off)
            return off
        detail = f"label '{f.label.text}' expected at row {f.row}, column {f.label.col}"
        detail += f" but found at column {found + 1}" if found >= 0 else " but it is not on the row"
        raise self._fail("layout_drift", f"screen layout changed on {screen}: {detail}. Update the screen map.", snap)

    def _read(self, screen: str, names: list[str], snap: Snapshot) -> dict[str, Any]:
        sd = self.map.screens[screen]
        out = {}
        for n in names:
            f = sd.fields[n]
            off = self._offset(screen, n, f, snap)
            out[n] = convert(snap.text_at(f.row, f.col + off, f.len), f.type)
        self.audit.write("screen.read", screen=screen, fields=names)
        return out

    def _read_table(self, screen: str, table: str, max_pages: int, snap: Snapshot) -> list[dict[str, Any]]:
        t = self.map.screens[screen].tables[table]
        rows: list[dict[str, Any]] = []
        for page in range(max(1, max_pages)):
            for r in range(t.first_row, t.last_row + 1):
                row = {n: convert(snap.text_at(r, c.col, c.len), c.type) for n, c in t.columns.items()}
                if row.get(t.key_column) not in (None, ""):
                    rows.append(row)
            if not t.next_page or page == max_pages - 1:
                break
            if t.page_indicator:
                m = re.search(r"PAGE (\d+) OF (\d+)", snap.lines[t.page_indicator.row - 1])
                if m and m.group(1) == m.group(2):
                    break
            self._press(t.next_page)
            prev = snap.lines
            d = self._ensure_driver()
            snap = d.wait_for(lambda s: s.lines != prev, timeout=self.timeout) or d.snapshot()
            if self.map.identify(snap) != screen:
                _, snap = self.where()
            if t.end_marker and t.end_marker in self._message(screen, snap):
                break
        self.audit.write("screen.read_table", screen=screen, table=table, rows=len(rows))
        return rows

    # -- tools -----------------------------------------------------------
    def pending_screen(self) -> str | None:
        if not self.pending:
            return None
        cd = self.toolset.tools[self.pending.tool].confirm
        return cd.screen if cd else None

    def _cancel_pending(self, reason: str) -> None:
        p = self.pending
        if not p:
            return
        self.pending = None
        cd = self.toolset.tools[p.tool].confirm
        try:
            sid, _ = self.where()
            if cd and sid == cd.screen:
                self._press(cd.cancel)
                self.where()
        finally:
            self.audit.write("confirm.cancelled", tool=p.tool, reason=reason)

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            tool = self.toolset.tools.get(name)
            if tool is None:
                raise BridgeError("unknown_tool", f"no tool named '{name}'")
            if tool.mode == "write" and self.writes == "off":
                self.audit.write("tool.refused", tool=name, reason="writes are disabled")
                raise BridgeError("writes_disabled", "this server is read-only; write tools are disabled")
            try:
                args = validate_inputs(tool, args)
            except InvalidInput as e:
                self.audit.write("tool.refused", tool=name, reason=str(e))
                raise BridgeError("invalid_input", str(e))
            self.audit.write("tool.call", tool=name, args=args, mode=tool.mode)
            if self.pending and name != "confirm_action":
                self._cancel_pending(f"new tool call: {name}")
            try:
                result = self._run(name, tool, args)
            except BridgeError as e:
                if e.kind != "session_expired":
                    raise
                self.audit.write("session.recover", tool=name, reason=e.message)
                result = self._run(name, tool, args)  # nothing was committed: safe to run again
            if tool.confirm:
                if self.writes == "on":
                    return self._commit(name, tool)
                code = secrets.token_hex(3).upper()
                self.pending = Pending(code, name, args, result)
                self.audit.write("confirm.requested", tool=name, code=code, preview=result)
                return {"ok": True, "status": "awaiting_confirmation", "confirmation_code": code, "preview": result,
                        "next": f"Call confirm_action with code {code} to execute, or any other tool to cancel."}
            return {"ok": True, **result}

    def confirm(self, code: str) -> dict[str, Any]:
        with self._lock:
            p = self.pending
            if not p:
                raise BridgeError("nothing_pending", "there is no action awaiting confirmation")
            if code.strip().upper() != p.code:
                self.audit.write("confirm.bad_code", tool=p.tool)
                raise BridgeError("bad_code", "confirmation code does not match")
            if time.monotonic() - p.created > self.confirm_ttl:
                self._cancel_pending("confirmation expired")
                raise BridgeError("expired", "confirmation expired; run the tool again")
            tool = self.toolset.tools[p.tool]
            assert tool.confirm
            # Re-check the confirmation screen still shows exactly what was previewed.
            snap = self._require(tool.confirm.screen)
            now = self._read(tool.confirm.screen, list(p.preview), snap)
            if now != p.preview:
                self._cancel_pending("screen changed since preview")
                raise self._fail("preview_mismatch", "the confirmation screen no longer matches the preview", snap)
            self.pending = None
            return self._commit(p.tool, tool)

    def _commit(self, name: str, tool: ToolDef) -> dict[str, Any]:
        cd = tool.confirm
        assert cd
        self._require(cd.screen)
        self._press(cd.key)
        sid, snap = self._expect(cd.result)
        if sid in cd.fail_if_screen:
            raise self._fail(cd.fail_if_screen[sid], self._message(sid, snap) or "rejected", snap)
        out = self._read(sid, cd.read, snap)
        self.audit.write("tool.committed", tool=name, result=out)
        return {"ok": True, "status": "completed", **out}

    def _run(self, name: str, tool: ToolDef, args: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        screen, snap = None, None

        def sub(v: Any) -> Any:
            if isinstance(v, str):
                return re.sub(r"\{(\w+)\}", lambda m: str(args.get(m.group(1), "")), v)
            return v

        for step in tool.steps:
            (op, arg), = step.items() if len(step) == 1 else (next(iter(step.items())),)
            if op == "goto":
                snap, screen = self.goto(arg), arg
            elif op == "fill":
                self._fill(screen, {k: sub(v) for k, v in arg.items()})
            elif op == "press":
                self._press(arg)
            elif op == "expect":
                screen, snap = self._expect(arg if isinstance(arg, list) else [arg])
            elif op == "fail_if_message":
                msg = self._message(screen, snap)
                for needle, kind in arg.items():
                    if needle in msg:
                        raise self._fail(kind, msg, snap)
            elif op == "fail_if_screen":
                if screen in arg:
                    raise self._fail(arg[screen], self._message(screen, snap) or f"rejected on {screen}", snap)
            elif op == "empty_if_message":
                msg = self._message(screen, snap)
                for needle, key in arg.items():
                    if needle in msg:
                        out[key] = []
                        return out
            elif op == "read":
                out.update(self._read(screen, arg, snap))
            elif op == "read_table":
                pages = int(sub(arg.get("max_pages", 1)))
                out[arg["table"]] = self._read_table(screen, arg["table"], pages, snap)
            elif op == "read_message":
                out[arg] = self._message(screen, snap)
            else:
                raise self._fail("bad_tool", f"unknown step '{op}' in tool {name}")
        return out
