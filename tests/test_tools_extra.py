"""Screen-map drafting and record-to-tool."""

import yaml

from legacy_bridge.driver import VTDriver, demo_bank_argv
from legacy_bridge.mapping.bench import run as mapping_bench
from legacy_bridge.recorder import Recorder, keys_from_terminal
from legacy_bridge.tools import ToolSet

from .conftest import ENV, MAP, TOOLS, make_bridge


def test_layout_mapping_recovers_hand_written_fields(tmp_path, accounts):
    br = make_bridge(tmp_path)
    r = mapping_bench(br, accounts["active"])
    br.close()
    assert r["field_recall"] == 1.0 and r["field_precision"] == 1.0
    assert r["screens"]["CUSR01"]["table_columns"] == 1.0
    assert r["screens"]["ACIN01"]["type_accuracy"] == 1.0


def test_record_to_tool_produces_a_working_tool(tmp_path, accounts):
    d = VTDriver(demo_bank_argv(), env=ENV)
    d.start()
    d.wait_for(lambda s: "SIGNON" in s.lines[0], 10)
    rec = Recorder(MAP, d)
    for kind, v in keys_from_terminal("TELLER01\tPASS2024\r1\r" + accounts["active"][0] + "\r"):
        rec.send(v) if kind == "text" else rec.press(v)
    d.close()
    tool = rec.to_tool("balance_lookup")["balance_lookup"]
    assert tool["steps"][0] == {"goto": "ACIN01"} and list(tool["inputs"]) == ["account_no"]
    assert {"read": [n for n, f in MAP.screens["ACIN01"].fields.items() if not f.input]} in tool["steps"]
    # The draft runs as-is through the bridge.
    ts = ToolSet.model_validate({"system": "x", "tools": {**TOOLS.model_dump()["tools"], "balance_lookup": tool}})
    br = make_bridge(tmp_path)
    br.toolset = ts
    out = br.call("balance_lookup", {"account_no": accounts["active"][1]})
    br.close()
    assert out["ok"] and out["balance"] == accounts["bank"].accounts[accounts["active"][1]].balance
    assert "REVIEW" in yaml.safe_load(rec.to_yaml("x").split("\n", 3)[3])["tools"]["x"]["description"]


def test_keys_from_terminal_splits_function_keys():
    assert keys_from_terminal("12\x1b[21~a\r") == [("text", "12"), ("key", "F10"), ("text", "a"), ("key", "ENTER")]
