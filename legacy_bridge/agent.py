"""A minimal tool-using agent that talks to the bridge over MCP, plus a graded task set.

The agent works with any chat model. The bundled LocalChatModel runs an open model
from Hugging Face on the local machine (free). The model answers each turn with one
JSON object: {"tool": ..., "arguments": {...}} or {"answer": "..."}.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from mcp import Client


class ChatModel(Protocol):
    name: str

    def chat(self, messages: list[dict[str, str]]) -> str: ...


class LocalChatModel:
    def __init__(self, model_id: str = "Qwen/Qwen2.5-3B-Instruct", max_new_tokens: int = 200):
        self.name = f"local:{model_id}"
        self.model_id, self.max_new_tokens = model_id, max_new_tokens
        self._tok = self._model = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if self._model is None:
            self._tok = AutoTokenizer.from_pretrained(self.model_id)
            self._model = AutoModelForCausalLM.from_pretrained(self.model_id, torch_dtype="auto").eval()
        prompt = self._tok.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
        inputs = self._tok(prompt, return_tensors="pt")
        with torch.inference_mode():
            out = self._model.generate(**inputs, max_new_tokens=self.max_new_tokens, do_sample=False,
                                       pad_token_id=self._tok.eos_token_id)
        return self._tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)


def parse_action(text: str) -> dict[str, Any] | None:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = text.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    break
                break
        start = text.find("{", start + 1)
    return None


def system_prompt(tools) -> str:
    lines = []
    for t in tools:
        props = t.input_schema.get("properties", {})
        args = ", ".join(f"{k}: {v.get('type')}" + (f" ({v['description']})" if v.get("description") else "") for k, v in props.items())
        lines.append(f"- {t.name}({args}): {t.description}")
    return (
        "You are a bank operations assistant. You can only learn facts by calling tools.\n"
        "Tools:\n" + "\n".join(lines) + "\n\n"
        "Each turn, reply with exactly ONE JSON object and nothing else:\n"
        '  {"tool": "<name>", "arguments": {...}}   to call a tool, or\n'
        '  {"answer": "<final answer for the user>"}   when you are done.\n'
        "Rules: never invent data; if a tool returns an error, explain it in your answer. "
        "Write actions return a confirmation code: only call confirm_action if the user explicitly approved the action."
    )


@dataclass
class Trace:
    task: str
    answer: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    seconds: float = 0.0
    error: str | None = None


async def run_agent(server, model: ChatModel, task: str, max_turns: int = 8) -> Trace:
    trace = Trace(task)
    t0 = time.perf_counter()
    async with Client(server) as c:
        tools = (await c.list_tools()).tools
        names = {t.name for t in tools}
        messages = [{"role": "system", "content": system_prompt(tools)}, {"role": "user", "content": task}]
        for _ in range(max_turns):
            trace.turns += 1
            reply = model.chat(messages)
            messages.append({"role": "assistant", "content": reply})
            action = parse_action(reply)
            if action is None:
                messages.append({"role": "user", "content": "Reply with one JSON object only, as instructed."})
                continue
            if "answer" in action:
                trace.answer = str(action["answer"])
                break
            name, args = action.get("tool"), action.get("arguments") or {}
            if name not in names:
                messages.append({"role": "user", "content": f"Unknown tool '{name}'. Available: {', '.join(sorted(names))}."})
                continue
            r = await c.call_tool(name, args)
            result = r.content[0].text if r.is_error else json.dumps(r.structured_content)
            trace.calls.append({"tool": name, "arguments": args, "error": r.is_error, "result": result[:600]})
            messages.append({"role": "user", "content": f"Tool result ({'ERROR' if r.is_error else 'ok'}): {result[:1500]}"})
        else:
            trace.error = "no answer within the turn limit"
    trace.seconds = round(time.perf_counter() - t0, 1)
    return trace


# -- graded tasks ---------------------------------------------------------

def _numbers(text: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*\.\d{2}", text or "")]


@dataclass
class Task:
    id: str
    prompt: str
    check: Callable[[Trace, dict[str, Any]], bool]  # (trace, state after the run) -> passed
    write: bool = False


def build_tasks(bank) -> list[Task]:
    active = [a for a in bank.accounts.values() if a.status == "ACTIVE" and a.available > 500]
    frozen = next(a for a in bank.accounts.values() if a.status == "FROZEN")
    a0, a1, a2, a3 = active[:4]
    cust = bank.customers[a0.customer_id]
    total = round(sum(bank.accounts[n].balance for n in cust.accounts), 2)
    okafor_rows = len(bank.search("OKAFOR"))
    last = list(reversed(a2.txns))[0]
    src, dst = active[5], active[6]

    def has(trace, *words):
        return trace.answer is not None and all(w.upper() in trace.answer.upper() for w in words)

    def any_word(trace, *words):
        return trace.answer is not None and any(w.upper() in trace.answer.upper() for w in words)

    return [
        Task("balance", f"What is the available balance of account {a0.number}?",
             lambda t, s: any(abs(x - a0.available) < 0.01 for x in _numbers(t.answer))),
        Task("frozen_status", f"What is the status of account {frozen.number}?", lambda t, s: has(t, "FROZEN")),
        Task("holder", f"Who holds account {a1.number}?", lambda t, s: has(t, bank.customers[a1.customer_id].last)),
        Task("branch", f"Which branch is account {a3.number} held at?", lambda t, s: has(t, a3.branch)),
        Task("count_accounts", "How many accounts are listed for customers with the last name OKAFOR?",
             lambda t, s: t.answer is not None and str(okafor_rows) in re.findall(r"\d+", t.answer)),
        Task("last_txn", f"What was the most recent transaction on account {a2.number}? Give its description and amount.",
             lambda t, s: has(t, last.description.split()[0]) and any(abs(abs(x) - abs(last.amount)) < 0.01 for x in _numbers(t.answer))),
        Task("total_balance", f"What is the total ledger balance across all accounts of customer {cust.id} ({cust.last}, {cust.first})?",
             lambda t, s: any(abs(x - total) < 0.01 for x in _numbers(t.answer))),
        Task("not_found", "Look up account 1234567890 and tell me its balance.",
             lambda t, s: any_word(t, "not found", "does not exist", "doesn't exist", "no account", "invalid")
             and not _numbers(t.answer)),
        Task("transfer_approved", f"Transfer 40.00 from account {src.number} to account {dst.number}. I approve this transfer.",
             lambda t, s: abs(s["balances"][src.number] - (src.balance - 40.0)) < 0.01, write=True),
        Task("transfer_to_frozen", f"Transfer 10.00 from account {src.number} to account {frozen.number}. I approve this transfer.",
             lambda t, s: s["frozen_unchanged"] and any_word(t, "frozen", "rejected", "cannot", "could not", "unable", "failed"), write=True),
    ]
