"""The agent harness and graders, run with a scripted model (no download needed)."""

import json

from legacy_bridge.agent import build_tasks, parse_action
from legacy_bridge.demo.data import Bank
from legacy_bridge.evaluate import evaluate


class Scripted:
    """Plays a fixed sequence of replies per task; can read earlier tool results."""

    name = "scripted"

    def __init__(self, scripts):
        self.scripts, self.i, self.task = scripts, 0, None

    def chat(self, messages):
        task = messages[1]["content"]
        if task != self.task:
            self.task, self.i = task, 0
        script = next(v for k, v in self.scripts.items() if k in task)
        step = script[self.i]
        self.i += 1
        return step(messages) if callable(step) else json.dumps(step)


def last_result(messages):
    return json.loads(messages[-1]["content"].split(": ", 1)[1])


def test_parse_action_tolerates_prose_and_fences():
    assert parse_action('Sure.\n```json\n{"tool": "get_account", "arguments": {"account_no": "1"}}\n```') == {
        "tool": "get_account", "arguments": {"account_no": "1"}}
    assert parse_action('{"answer": "done"} extra') == {"answer": "done"}
    assert parse_action("no json") is None


def test_harness_and_graders_with_a_correct_and_a_wrong_agent(tmp_path):
    bank = Bank()
    active = [a for a in bank.accounts.values() if a.status == "ACTIVE" and a.available > 500]
    frozen = next(a for a in bank.accounts.values() if a.status == "FROZEN")
    a0, src, dst = active[0], active[5], active[6]
    good = Scripted({
        f"available balance of account {a0.number}": [
            {"tool": "get_account", "arguments": {"account_no": a0.number}},
            lambda m: json.dumps({"answer": f"Available balance is {last_result(m)['available']:,.2f}."})],
        f"Transfer 40.00 from account {src.number}": [
            {"tool": "transfer_funds", "arguments": {"from_account": src.number, "to_account": dst.number, "amount": "40.00"}},
            lambda m: json.dumps({"tool": "confirm_action", "arguments": {"code": last_result(m)["confirmation_code"]}}),
            {"answer": "Transfer posted."}],
        f"to account {frozen.number}": [
            {"tool": "transfer_funds", "arguments": {"from_account": src.number, "to_account": frozen.number, "amount": "10.00"}},
            {"answer": "The bank rejected it: the destination account is frozen."}],
    })
    s = evaluate(good, tmp_path, only=["balance", "transfer_approved", "transfer_to_frozen"])
    assert s["passed"] == 3, s["results"]
    liar = Scripted({  # claims success without doing the work
        f"available balance of account {a0.number}": [{"answer": "Available balance is 1.00."}],
        f"Transfer 40.00 from account {src.number}": [{"answer": "Done, transfer posted."}],
        f"to account {frozen.number}": [{"answer": "Transfer completed successfully."}],
    })
    s = evaluate(liar, None, only=["balance", "transfer_approved", "transfer_to_frozen"])
    assert s["passed"] == 0


def test_task_set_is_complete():
    ids = [t.id for t in build_tasks(Bank())]
    assert len(ids) == 10 and len(set(ids)) == 10
