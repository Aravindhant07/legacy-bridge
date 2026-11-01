"""Northwind Savings Core Banking: a demo legacy terminal application (fictional).

An 80x24 full-screen VT100 program in the style of a mainframe/AS400 green screen:
screen IDs top-left, fixed-position fields, Tab between input fields, Enter to submit,
F3 back, F7/F8 page, F10 confirm, F12 cancel, message line on row 23, key legend on row 24.

Run it in a terminal:  python -m legacy_bridge.demo.bank_app
Fault injection (for robustness tests):
  --slow SECONDS     delay before each response screen
  --drift N          shift the account-inquiry output fields N columns right
  --popup RATE       chance of a system broadcast screen interrupting a transaction
  --timeout SECONDS  sign the user off after this long idle
"""

from __future__ import annotations

import argparse
import os
import signal
import random
import select
import sys
import termios
import time
import tty
from dataclasses import dataclass, field

from .data import Bank

ROWS, COLS = 24, 80
KEYS = {
    "\x1bOP": "F1", "\x1bOQ": "F2", "\x1bOR": "F3", "\x1bOS": "F4", "\x1b[15~": "F5", "\x1b[17~": "F6",
    "\x1b[18~": "F7", "\x1b[19~": "F8", "\x1b[20~": "F9", "\x1b[21~": "F10", "\x1b[23~": "F11", "\x1b[24~": "F12",
    "\x1b[Z": "BACKTAB", "\r": "ENTER", "\n": "ENTER", "\t": "TAB", "\x7f": "BACKSPACE", "\x08": "BACKSPACE",
}
USER = os.environ.get("BANK_USER", "TELLER01")
PASSWORD = os.environ.get("BANK_PASS", "PASS2024")


@dataclass
class Field:
    name: str
    row: int
    col: int
    length: int
    value: str = ""
    hidden: bool = False


@dataclass
class Screen:
    id: str
    title: str
    text: dict[tuple[int, int], str] = field(default_factory=dict)  # (row, col) -> static text
    inputs: list[Field] = field(default_factory=list)
    legend: str = ""
    message: str = ""

    def put(self, row: int, col: int, s: str) -> None:
        self.text[(row, col)] = s

    def field(self, name: str) -> Field:
        return next(f for f in self.inputs if f.name == name)


def money(x: float) -> str:
    return f"{x:,.2f}" if x >= 0 else f"{-x:,.2f}-"


class App:
    def __init__(self, slow: float = 0.0, drift: int = 0, popup: float = 0.0, timeout: float = 0.0, seed: int = 7):
        self.bank = Bank()
        self.slow, self.drift, self.popup, self.timeout = slow, drift, popup, timeout
        self.rng = random.Random(seed)
        self.out = sys.stdout
        self.cur = 0
        self.last_key = time.monotonic()
        self.signed_on = False
        self.pending: dict | None = None  # transfer awaiting confirmation
        self.after_popup = None
        self.page = 0
        self.screen = self.signon()

    # -- rendering --------------------------------------------------------
    def render(self) -> None:
        s = self.screen
        buf = ["\x1b[2J\x1b[H"]

        def at(r: int, c: int, text: str) -> None:
            buf.append(f"\x1b[{r};{c}H{text[: COLS - c + 1]}")

        at(1, 2, s.id)
        at(1, (COLS - len("NORTHWIND SAVINGS - CORE BANKING")) // 2, "NORTHWIND SAVINGS - CORE BANKING")
        at(1, 63, "2026-10-30 09:41")
        at(2, (COLS - len(s.title)) // 2, s.title)
        for (r, c), t in sorted(s.text.items()):
            at(r, c, t)
        for f in s.inputs:
            shown = ("*" * len(f.value)) if f.hidden else f.value
            at(f.row, f.col, shown.ljust(f.length, "_"))
        if s.message:
            at(23, 2, s.message)
        at(24, 2, s.legend)
        if s.inputs:
            f = s.inputs[self.cur]
            buf.append(f"\x1b[{f.row};{f.col + min(len(f.value), f.length - 1)}H")
        else:
            buf.append("\x1b[24;80H")
        self.out.write("".join(buf))
        self.out.flush()

    def show(self, screen: Screen, delay: bool = True) -> None:
        if delay and self.slow:
            time.sleep(self.slow)
        if delay and self.popup and self.signed_on and self.rng.random() < self.popup:
            self.after_popup = screen
            screen = self.broadcast()
        self.screen = screen
        self.cur = 0

    # -- screens ----------------------------------------------------------
    def signon(self, message: str = "") -> Screen:
        s = Screen("SIGNON", "SIGN ON", legend="ENTER=Sign on   F3=Exit", message=message)
        s.put(6, 20, "USER ID  . . . . :")
        s.put(8, 20, "PASSWORD . . . . :")
        s.inputs = [Field("user", 6, 40, 10), Field("password", 8, 40, 10, hidden=True)]
        return s

    def menu(self, message: str = "") -> Screen:
        s = Screen("MAIN", "MAIN MENU", legend="ENTER=Select   F3=Sign off", message=message)
        for i, t in enumerate(["ACCOUNT INQUIRY", "CUSTOMER SEARCH", "TRANSACTION HISTORY", "FUNDS TRANSFER"], 1):
            s.put(5 + i, 20, f"{i}. {t}")
        s.put(13, 20, "SELECTION  . . . :")
        s.inputs = [Field("selection", 13, 40, 1)]
        return s

    def broadcast(self) -> Screen:
        s = Screen("SYSBRD", "*** SYSTEM BROADCAST ***", legend="ENTER=Continue")
        s.put(8, 10, "SCHEDULED MAINTENANCE TONIGHT 23:00-23:30 EST.")
        s.put(10, 10, "PLEASE COMPLETE OPEN TRANSACTIONS BEFORE 22:45.")
        return s

    def inquiry(self, acct: str = "", message: str = "") -> Screen:
        s = Screen("ACIN01", "ACCOUNT INQUIRY", legend="ENTER=Inquire   F3=Menu", message=message)
        s.put(4, 2, "ACCOUNT NUMBER:")
        s.inputs = [Field("account_no", 4, 18, 10, acct)]
        a = self.bank.accounts.get(acct) if acct and not message else None
        if a:
            c = self.bank.customers[a.customer_id]
            d = self.drift
            for row, label, value in [
                (6, "ACCOUNT HOLDER:", c.name), (7, "CUSTOMER ID   :", c.id), (8, "ACCOUNT TYPE  :", a.kind),
                (9, "STATUS        :", a.status), (11, "LEDGER BALANCE:", money(a.balance).rjust(15)),
                (12, "AVAILABLE BAL :", money(a.available).rjust(15)), (13, "HOLDS         :", money(a.hold).rjust(15)),
                (15, "LAST ACTIVITY :", a.last_activity.isoformat()), (16, "BRANCH        :", a.branch),
            ]:
                s.put(row, 2 + d, label)
                s.put(row, 18 + d, value)
        return s

    def search(self, last: str = "", message: str = "") -> Screen:
        s = Screen("CUSR01", "CUSTOMER SEARCH", legend="ENTER=Search   F3=Menu", message=message)
        s.put(4, 2, "LAST NAME . . :")
        s.inputs = [Field("last_name", 4, 18, 20, last)]
        if last and not message:
            rows = self.bank.search(last)
            s.put(6, 2, "CUST ID   NAME                    BRANCH  ACCOUNT     TYPE      STATUS")
            for i, (c, a) in enumerate(rows[:14]):
                s.put(7 + i, 2, f"{c.id:<10}{c.name[:22]:<24}{c.branch:<8}{a.number:<12}{a.kind:<10}{a.status}")
            s.message = (f"{len(rows)} ACCOUNT(S) FOUND" + (" - FIRST 14 SHOWN" if len(rows) > 14 else "")) if rows else "NO CUSTOMERS FOUND"
        return s

    def history(self, acct: str = "", message: str = "") -> Screen:
        s = Screen("TXHS01", "TRANSACTION HISTORY", legend="ENTER=Show   F7=Prev   F8=Next   F3=Menu", message=message)
        s.put(4, 2, "ACCOUNT NUMBER:")
        s.inputs = [Field("account_no", 4, 18, 10, acct)]
        a = self.bank.accounts.get(acct) if acct and not message else None
        if a:
            txns = list(reversed(a.txns))
            pages = max(1, (len(txns) + 11) // 12)
            self.page = min(self.page, pages - 1)
            s.put(6, 2, "DATE        DESCRIPTION                       AMOUNT         BALANCE")
            for i, t in enumerate(txns[self.page * 12:(self.page + 1) * 12]):
                s.put(7 + i, 2, f"{t.day.isoformat():<12}{t.description[:28]:<28}{money(t.amount):>14}{money(t.balance):>16}")
            s.put(20, 2, f"PAGE {self.page + 1} OF {pages}")
        return s

    def transfer(self, values: dict | None = None, message: str = "") -> Screen:
        v = values or {}
        s = Screen("FNTR01", "FUNDS TRANSFER", legend="ENTER=Continue   F3=Menu", message=message)
        s.put(5, 2, "FROM ACCOUNT  :")
        s.put(7, 2, "TO ACCOUNT    :")
        s.put(9, 2, "AMOUNT        :")
        s.put(11, 2, "MEMO          :")
        s.inputs = [Field("from_account", 5, 18, 10, v.get("from_account", "")), Field("to_account", 7, 18, 10, v.get("to_account", "")),
                    Field("amount", 9, 18, 12, v.get("amount", "")), Field("memo", 11, 18, 30, v.get("memo", ""))]
        return s

    def confirm(self, p: dict) -> Screen:
        s = Screen("FNTR02", "CONFIRM FUNDS TRANSFER", legend="F10=Confirm   F12=Cancel")
        s.put(5, 2, f"FROM ACCOUNT  : {p['src']}")
        s.put(7, 2, f"TO ACCOUNT    : {p['dst']}")
        s.put(9, 2, f"AMOUNT        : {money(p['amount'])}")
        s.put(11, 2, f"MEMO          : {p['memo']}")
        s.put(14, 2, "PRESS F10 TO CONFIRM THIS TRANSFER OR F12 TO CANCEL.")
        return s

    def done(self, ref: str, p: dict) -> Screen:
        s = Screen("FNTR03", "TRANSFER COMPLETE", legend="ENTER=Menu", message="TRANSFER POSTED")
        s.put(6, 2, f"REFERENCE     : {ref}")
        s.put(8, 2, f"AMOUNT        : {money(p['amount'])}")
        s.put(10, 2, f"NEW BALANCE   : {money(self.bank.accounts[p['src']].balance)}")
        return s

    # -- input handling ---------------------------------------------------
    def values(self) -> dict[str, str]:
        return {f.name: f.value.strip() for f in self.screen.inputs}

    def key(self, k: str) -> bool:
        """Handle one key. Returns False to exit."""
        now = time.monotonic()
        if self.timeout and self.signed_on and now - self.last_key > self.timeout:
            self.signed_on = False
            self.show(self.signon("SESSION EXPIRED - PLEASE SIGN ON AGAIN"), delay=False)
            self.last_key = now
            return True
        self.last_key = now
        s = self.screen
        if k in ("TAB", "BACKTAB") and s.inputs:
            self.cur = (self.cur + (1 if k == "TAB" else -1)) % len(s.inputs)
        elif k == "BACKSPACE" and s.inputs:
            f = s.inputs[self.cur]
            f.value = f.value[:-1]
        elif len(k) == 1 and k.isprintable() and s.inputs:
            f = s.inputs[self.cur]
            if len(f.value) < f.length:
                f.value += k.upper() if f.name != "password" else k
            if len(f.value) == f.length and self.cur < len(s.inputs) - 1:
                self.cur += 1  # auto-skip to the next field, as on 3270/5250 terminals
        else:
            return self.action(k)
        return True

    def action(self, k: str) -> bool:
        s, v = self.screen, self.values()
        if s.id == "SYSBRD":
            if k == "ENTER" and self.after_popup:
                self.screen, self.after_popup = self.after_popup, None
            return True
        if s.id == "SIGNON":
            if k == "F3":
                return False
            if k == "ENTER":
                if v["user"] == USER and s.field("password").value == PASSWORD:
                    self.signed_on = True
                    self.show(self.menu(f"WELCOME {USER}"), delay=False)
                else:
                    self.show(self.signon("INVALID USER ID OR PASSWORD"), delay=False)
            return True
        if s.id == "MAIN":
            if k == "F3":
                self.signed_on = False
                self.show(self.signon("SIGNED OFF"), delay=False)
            elif k == "ENTER":
                target = {"1": self.inquiry, "2": self.search, "3": self.history, "4": self.transfer}.get(v["selection"])
                self.page = 0
                self.show(target() if target else self.menu("INVALID SELECTION"))
            return True
        if k == "F3" and s.id in ("ACIN01", "CUSR01", "TXHS01", "FNTR01"):
            self.show(self.menu(), delay=False)
            return True
        if s.id == "ACIN01" and k == "ENTER":
            n = v["account_no"]
            msg = "INVALID ACCOUNT NUMBER" if not (n.isdigit() and len(n) == 10) else ("" if n in self.bank.accounts else "ACCOUNT NOT FOUND")
            self.show(self.inquiry(n, msg))
        elif s.id == "CUSR01" and k == "ENTER":
            self.show(self.search(v["last_name"], "" if v["last_name"] else "ENTER A LAST NAME"))
        elif s.id == "TXHS01":
            n = v["account_no"]
            if k == "ENTER":
                self.page = 0
            elif k in ("F7", "F8") and n in self.bank.accounts:
                pages = max(1, (len(self.bank.accounts[n].txns) + 11) // 12)
                if k == "F8" and self.page >= pages - 1:
                    self.show(self.history(n), delay=False)
                    self.screen.message = "END OF LIST"
                    return True
                if k == "F7" and self.page == 0:
                    self.show(self.history(n), delay=False)
                    self.screen.message = "TOP OF LIST"
                    return True
                self.page += 1 if k == "F8" else -1
            elif k not in ("ENTER", "F7", "F8"):
                return True
            msg = "" if n in self.bank.accounts else ("INVALID ACCOUNT NUMBER" if not (n.isdigit() and len(n) == 10) else "ACCOUNT NOT FOUND")
            self.show(self.history(n, msg))
        elif s.id == "FNTR01" and k == "ENTER":
            try:
                amount = round(float(v["amount"].replace(",", "")), 2)
            except ValueError:
                amount = -1.0
            err = self.bank.transfer_check(v["from_account"], v["to_account"], amount)
            if err:
                self.show(self.transfer(v, err))
            else:
                self.pending = {"src": v["from_account"], "dst": v["to_account"], "amount": amount, "memo": v["memo"]}
                self.show(self.confirm(self.pending))
        elif s.id == "FNTR02":
            if k == "F10" and self.pending:
                p, self.pending = self.pending, None
                err = self.bank.transfer_check(p["src"], p["dst"], p["amount"])  # re-check at commit time
                self.show(self.transfer({}, err) if err else self.done(self.bank.transfer(p["src"], p["dst"], p["amount"], p["memo"]), p))
            elif k == "F12":
                self.pending = None
                self.show(self.transfer({}, "TRANSFER CANCELLED"), delay=False)
        elif s.id == "FNTR03" and k == "ENTER":
            self.show(self.menu(), delay=False)
        return True


def read_key(fd: int, timeout: float | None = None) -> str | None:
    r, _, _ = select.select([fd], [], [], timeout)
    if not r:
        return None
    raw = os.read(fd, 1)
    if not raw:
        raise EOFError  # the terminal went away: exit instead of spinning
    data = raw.decode(errors="ignore")
    if data == "\x1b":
        # escape sequence: read what is immediately available
        while True:
            r, _, _ = select.select([fd], [], [], 0.03)
            if not r:
                break
            data += os.read(fd, 1).decode(errors="ignore")
            if data in KEYS or (len(data) > 2 and data[-1] in "~ABCDPQRSZ"):
                break
    return KEYS.get(data, data if len(data) == 1 else None)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--slow", type=float, default=0.0)
    p.add_argument("--drift", type=int, default=0)
    p.add_argument("--popup", type=float, default=0.0)
    p.add_argument("--timeout", type=float, default=0.0)
    a = p.parse_args()
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(0))
    app = App(a.slow, a.drift, a.popup, a.timeout)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if os.isatty(fd) else None
    try:
        if old:
            tty.setraw(fd)
        app.render()
        while True:
            try:
                k = read_key(fd)
            except (EOFError, OSError):
                break
            if k is None:
                continue
            if not app.key(k):
                break
            app.render()
    finally:
        try:
            if old:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.flush()
        except OSError:
            pass  # terminal already closed


if __name__ == "__main__":
    main()
