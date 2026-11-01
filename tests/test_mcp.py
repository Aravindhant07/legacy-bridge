import json
import sys

import anyio
from mcp import Client, StdioServerParameters

from legacy_bridge.mcp_server import build_server

from .conftest import ROOT, make_bridge


def test_mcp_in_process(tmp_path, accounts):
    br = make_bridge(tmp_path)
    src, dst = accounts["active"][6:8]

    async def main():
        async with Client(build_server(br, rate_per_minute=5)) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            assert set(tools) == {"get_account", "search_customers", "get_transactions", "transfer_funds", "confirm_action"}
            assert tools["get_account"].annotations.read_only_hint
            assert tools["transfer_funds"].annotations.destructive_hint
            assert tools["get_account"].input_schema["properties"]["account_no"]["pattern"] == "^\\d{10}$"
            r = await c.call_tool("get_account", {"account_no": src})
            assert not r.is_error and r.structured_content["ok"]
            r = await c.call_tool("get_account", {"account_no": "1234567890"})
            assert r.is_error and json.loads(r.content[0].text.split(": ", 1)[1])["error"] == "not_found"
            p = await c.call_tool("transfer_funds", {"from_account": src, "to_account": dst, "amount": "3"})
            code = p.structured_content["confirmation_code"]
            r = await c.call_tool("confirm_action", {"code": code})
            assert r.structured_content["status"] == "completed"
            for _ in range(6):  # limit is 5 per minute per tool
                r = await c.call_tool("search_customers", {"last_name": "PATEL"})
            assert r.is_error and "rate_limited" in r.content[0].text

    anyio.run(main)
    br.close()


def test_mcp_writes_off_hides_write_tools(tmp_path):
    br = make_bridge(tmp_path, writes="off")

    async def main():
        async with Client(build_server(br)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert "transfer_funds" not in names and "confirm_action" not in names

    anyio.run(main)
    br.close()


def test_mcp_over_stdio(tmp_path, accounts):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "legacy_bridge.cli", "serve", "--audit", str(tmp_path / "stdio_audit.jsonl"), "--writes", "off"],
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
    )

    async def main():
        async with Client(params) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert "get_account" in names
            r = await c.call_tool("get_account", {"account_no": accounts["active"][0]})
            assert r.structured_content["ok"]

    anyio.run(main)
