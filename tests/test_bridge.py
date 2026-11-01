import json
import time

import pytest

from legacy_bridge.audit import verify
from legacy_bridge.bridge import BridgeError
from legacy_bridge.screenmap import convert

from .conftest import MAP, audit_events, audit_text, make_bridge


def test_every_screen_is_identified(bridge, accounts):
    seen = set()
    sid, _ = bridge.where()
    seen.add(sid)
    for target in ("MAIN", "ACIN01", "CUSR01", "TXHS01", "FNTR01", "MAIN"):
        bridge.goto(target)
        seen.add(bridge.where()[0])
    assert {"SIGNON", "MAIN", "ACIN01", "CUSR01", "TXHS01", "FNTR01"} <= seen


def test_read_tools_return_typed_data(bridge, accounts):
    a = accounts["bank"].accounts[accounts["active"][0]]
    r = bridge.call("get_account", {"account_no": a.number})
    assert r["ok"] and r["balance"] == a.balance and r["available"] == a.available and r["status"] == "ACTIVE"
    r = bridge.call("search_customers", {"last_name": "PATEL"})
    assert r["results"] and all(row["name"].startswith("PATEL") for row in r["results"])
    r = bridge.call("search_customers", {"last_name": "ZZZ"})
    assert r["results"] == []
    r = bridge.call("get_transactions", {"account_no": a.number, "max_pages": 2})
    expected = list(reversed(a.txns))[:24]
    assert [t["amount"] for t in r["transactions"]] == [t.amount for t in expected]


def test_business_errors_are_structured(bridge):
    with pytest.raises(BridgeError) as e:
        bridge.call("get_account", {"account_no": "1234567890"})
    assert e.value.kind == "not_found" and "ACCOUNT NOT FOUND" in e.value.screen
    with pytest.raises(BridgeError) as e:
        bridge.call("get_account", {"account_no": "12AB"})
    assert e.value.kind == "invalid_input"
    with pytest.raises(BridgeError) as e:
        bridge.call("get_account", {"account_no": "1234567890", "extra": 1})
    assert e.value.kind == "invalid_input"


def test_transfer_two_phase_and_rules(bridge, accounts):
    src, dst = accounts["active"][:2]
    before = bridge.call("get_account", {"account_no": src})["balance"]
    p = bridge.call("transfer_funds", {"from_account": src, "to_account": dst, "amount": "12.34", "memo": "TEST"})
    assert p["status"] == "awaiting_confirmation" and p["preview"]["amount"] == 12.34
    with pytest.raises(BridgeError) as e:
        bridge.confirm("NOPE00")
    assert e.value.kind == "bad_code"
    done = bridge.confirm(p["confirmation_code"])
    assert done["status"] == "completed" and done["reference"].startswith("TRF")
    assert bridge.call("get_account", {"account_no": src})["balance"] == round(before - 12.34, 2)
    with pytest.raises(BridgeError) as e:
        bridge.call("transfer_funds", {"from_account": src, "to_account": accounts["frozen"], "amount": "1"})
    assert e.value.kind == "rejected" and "FROZEN" in e.value.message
    with pytest.raises(BridgeError) as e:
        bridge.call("transfer_funds", {"from_account": src, "to_account": dst, "amount": "9999999"})
    assert e.value.kind == "rejected"


def test_pending_write_is_cancelled_by_another_tool(bridge, accounts):
    src, dst = accounts["active"][2:4]
    p = bridge.call("transfer_funds", {"from_account": src, "to_account": dst, "amount": "5"})
    bridge.call("get_account", {"account_no": src})  # agent moved on
    with pytest.raises(BridgeError) as e:
        bridge.confirm(p["confirmation_code"])
    assert e.value.kind == "nothing_pending"
    assert "confirm.cancelled" in audit_events(bridge)


def test_writes_off_and_on(tmp_path, accounts):
    src, dst = accounts["active"][4:6]
    off = make_bridge(tmp_path, writes="off")
    with pytest.raises(BridgeError) as e:
        off.call("transfer_funds", {"from_account": src, "to_account": dst, "amount": "1"})
    assert e.value.kind == "writes_disabled"
    off.close()
    on = make_bridge(tmp_path, writes="on")
    r = on.call("transfer_funds", {"from_account": src, "to_account": dst, "amount": "1"})
    assert r["status"] == "completed"
    on.close()


def test_popups_are_dismissed_and_logged(tmp_path, accounts):
    br = make_bridge(tmp_path, "--popup", "0.7")
    for n in accounts["active"][:4]:
        assert br.call("get_account", {"account_no": n})["ok"]
    assert "interrupt.dismissed" in audit_events(br)
    br.close()


def test_slow_screens(tmp_path, accounts):
    br = make_bridge(tmp_path, "--slow", "0.7")
    t = time.monotonic()
    assert br.call("get_account", {"account_no": accounts["active"][0]})["ok"]
    assert time.monotonic() - t > 0.7
    br.close()


def test_layout_drift_refused_or_relocated(tmp_path, accounts):
    n = accounts["active"][0]
    strict = make_bridge(tmp_path, "--drift", "3")
    with pytest.raises(BridgeError) as e:
        strict.call("get_account", {"account_no": n})
    assert e.value.kind == "layout_drift" and "column 5" in e.value.message
    strict.close()
    lenient = make_bridge(tmp_path, "--drift", "3", relocate=True)
    r = lenient.call("get_account", {"account_no": n})
    assert r["balance"] == accounts["bank"].accounts[n].balance
    assert "drift.relocated" in audit_events(lenient)
    lenient.close()


def test_session_timeout_recovers(tmp_path, accounts):
    br = make_bridge(tmp_path, "--timeout", "1")
    n = accounts["active"][0]
    assert br.call("get_account", {"account_no": n})["ok"]
    time.sleep(1.3)
    assert br.call("get_account", {"account_no": n})["ok"]
    assert "session.recover" in audit_events(br) or audit_events(br).count("session.signed_on") >= 2
    br.close()


def test_bad_credentials(tmp_path):
    br = make_bridge(tmp_path, creds=("TELLER01", "WRONG"))
    with pytest.raises(BridgeError) as e:
        br.call("search_customers", {"last_name": "PATEL"})
    assert e.value.kind == "sign_on_failed" and "INVALID" in e.value.message
    br.close()


def test_audit_masks_passwords_and_detects_tampering(bridge, accounts):
    bridge.call("get_account", {"account_no": accounts["active"][0]})
    text = audit_text(bridge)
    assert "PASS2024" not in text and '"value": "***"' in text
    path = bridge.audit.path
    assert verify(path)[0]
    lines = path.read_text().splitlines()
    rec = json.loads(lines[3])
    rec["data"]["tampered"] = True
    lines[3] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")
    ok, n, reason = verify(path)
    assert not ok and "edited" in reason


def test_convert_money_and_paths():
    assert convert("     1,234.56-", "money") == -1234.56
    assert convert("0.00", "money") == 0.0
    assert [e.to for e in MAP.path("ACIN01", "FNTR01")] == ["MAIN", "FNTR01"]
    assert MAP.path("FNTR03", "TXHS01")[-1].to == "TXHS01"
