"""Synthetic customers, accounts and transactions for the demo core-banking system."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

FIRST = ["ANA", "BEN", "CHEN", "DIEGO", "ELENA", "FATIMA", "GEORGE", "HANA", "IVAN", "JUNE", "KOFI", "LEILA",
         "MARCO", "NADIA", "OMAR", "PRIYA", "QUINN", "RAVI", "SARA", "TOMAS"]
LAST = ["PATEL", "OKAFOR", "GARCIA", "CHEN", "NOVAK", "SMITH", "KOWALSKI", "MENDES", "TANAKA", "OSEI",
        "ROSSI", "IBRAHIM", "MILLER", "SINGH", "DUBOIS", "MURPHY"]
BRANCHES = ["NYC01", "BOS02", "CHI03", "SFO04"]
DESCRIPTIONS = ["PAYROLL DEPOSIT", "CARD PURCHASE GROCERY", "CARD PURCHASE FUEL", "ATM WITHDRAWAL", "UTILITY BILL",
                "RENT PAYMENT", "ONLINE TRANSFER IN", "ONLINE TRANSFER OUT", "INTEREST CREDIT", "INSURANCE PREMIUM"]


@dataclass
class Txn:
    day: date
    description: str
    amount: float  # positive = credit
    balance: float


@dataclass
class Account:
    number: str
    customer_id: str
    kind: str  # CHECKING | SAVINGS
    status: str  # ACTIVE | FROZEN | CLOSED
    branch: str
    balance: float
    hold: float = 0.0
    txns: list[Txn] = field(default_factory=list)

    @property
    def available(self) -> float:
        return round(self.balance - self.hold, 2)

    @property
    def last_activity(self) -> date:
        return self.txns[-1].day if self.txns else date(2026, 1, 2)


@dataclass
class Customer:
    id: str
    first: str
    last: str
    branch: str
    accounts: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.last}, {self.first}"


class Bank:
    DAILY_LIMIT = 5000.00

    def __init__(self, seed: int = 42, today: date = date(2026, 10, 30)):
        rng = random.Random(seed)
        self.today = today
        self.customers: dict[str, Customer] = {}
        self.accounts: dict[str, Account] = {}
        self.transferred_today: dict[str, float] = {}
        self.next_ref = 4100
        acct_no = 1000200300
        for i in range(40):
            cid = f"C{10001 + i}"
            c = Customer(cid, rng.choice(FIRST), LAST[i % len(LAST)] if i < 32 else rng.choice(LAST), rng.choice(BRANCHES))
            self.customers[cid] = c
            for kind in (["CHECKING", "SAVINGS"] if rng.random() < 0.6 else [rng.choice(["CHECKING", "SAVINGS"])]):
                acct_no += rng.randint(7, 97)
                status = "FROZEN" if rng.random() < 0.06 else ("CLOSED" if rng.random() < 0.03 else "ACTIVE")
                a = Account(str(acct_no), cid, kind, status, c.branch, 0.0)
                bal = round(rng.uniform(200, 9000 if kind == "CHECKING" else 40000), 2)
                day = today - timedelta(days=90)
                running = bal
                for _ in range(rng.randint(14, 40)):
                    day += timedelta(days=rng.randint(0, 5))
                    if day > today:
                        break
                    desc = rng.choice(DESCRIPTIONS)
                    credit = "DEPOSIT" in desc or desc.endswith(" IN") or "CREDIT" in desc
                    amt = round(rng.uniform(20, 1500 if credit else min(2500, max(25, running * 0.3))), 2)
                    if not credit and running - amt < 50:
                        desc, credit = "PAYROLL DEPOSIT", True  # keep balances realistic
                    amt = amt if credit else -amt
                    running += amt
                    a.txns.append(Txn(day, desc, amt, round(running, 2)))
                a.balance = round(running, 2)
                if rng.random() < 0.15:
                    a.hold = round(min(a.balance, rng.uniform(50, 800)), 2) if a.balance > 0 else 0.0
                self.accounts[a.number] = a
                c.accounts.append(a.number)

    def search(self, last: str) -> list[tuple[Customer, Account]]:
        last = last.strip().upper()
        rows = []
        for c in sorted(self.customers.values(), key=lambda c: (c.last, c.first, c.id)):
            if c.last.startswith(last):
                rows += [(c, self.accounts[n]) for n in c.accounts]
        return rows

    def transfer_check(self, src: str, dst: str, amount: float) -> str | None:
        """Return an error message, or None if the transfer is allowed."""
        if src not in self.accounts:
            return "FROM ACCOUNT NOT FOUND"
        if dst not in self.accounts:
            return "TO ACCOUNT NOT FOUND"
        if src == dst:
            return "FROM AND TO ACCOUNTS MUST DIFFER"
        if amount <= 0:
            return "INVALID AMOUNT"
        a, b = self.accounts[src], self.accounts[dst]
        for acct in (a, b):
            if acct.status != "ACTIVE":
                return f"ACCOUNT {acct.number} IS {acct.status}"
        if amount > a.available:
            return "INSUFFICIENT AVAILABLE FUNDS"
        if self.transferred_today.get(src, 0) + amount > self.DAILY_LIMIT:
            return f"DAILY LIMIT EXCEEDED (MAX {self.DAILY_LIMIT:,.2f})"
        return None

    def transfer(self, src: str, dst: str, amount: float, memo: str) -> str:
        a, b = self.accounts[src], self.accounts[dst]
        a.balance = round(a.balance - amount, 2)
        b.balance = round(b.balance + amount, 2)
        a.txns.append(Txn(self.today, f"TRANSFER TO {dst}", -amount, a.balance))
        b.txns.append(Txn(self.today, f"TRANSFER FROM {src}", amount, b.balance))
        self.transferred_today[src] = self.transferred_today.get(src, 0) + amount
        self.next_ref += 1
        return f"TRF{self.next_ref:07d}"
