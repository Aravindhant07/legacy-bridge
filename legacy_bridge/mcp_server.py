"""MCP server: exposes every tool in the tool file to AI agents (Claude Desktop, any MCP client)."""

from __future__ import annotations

import inspect
import json
import threading
import time
from collections import defaultdict, deque
from typing import Annotated, Any

import anyio
from mcp.server.mcpserver import MCPServer
from pydantic import Field
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .bridge import Bridge, BridgeError
from .tools import ToolDef

PY_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool}


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.calls: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, name: str) -> None:
        if self.per_minute <= 0:
            return
        now = time.monotonic()
        with self._lock:
            q = self.calls[name]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                raise BridgeError("rate_limited", f"more than {self.per_minute} calls to '{name}' in the last minute")
            q.append(now)


def _error(e: BridgeError) -> ToolError:
    d = e.as_dict()
    if d.get("screen"):
        d["screen"] = d["screen"][-1500:]
    return ToolError(json.dumps(d))


def _make_tool_fn(bridge: Bridge, limiter: RateLimiter, name: str, tool: ToolDef):
    async def fn(**kwargs: Any) -> dict[str, Any]:
        try:
            limiter.check(name)
            return await anyio.to_thread.run_sync(bridge.call, name, kwargs)
        except BridgeError as e:
            raise _error(e) from None

    params = []
    for arg, d in tool.inputs.items():
        default = inspect.Parameter.empty if d.default is None else d.default
        constraints: dict[str, Any] = {"description": d.description or None}
        if d.pattern:
            constraints["pattern"] = d.pattern
        if d.minimum is not None:
            constraints["ge"] = d.minimum
        if d.maximum is not None:
            constraints["le"] = d.maximum
        ann = Annotated[PY_TYPES[d.type], Field(**{k: v for k, v in constraints.items() if v is not None})]
        params.append(inspect.Parameter(arg, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=ann))
    fn.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    fn.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict[str, Any]}
    fn.__name__ = name
    fn.__doc__ = tool.description
    return fn


def build_server(bridge: Bridge, rate_per_minute: int = 30) -> MCPServer:
    limiter = RateLimiter(rate_per_minute)
    server = MCPServer(
        "legacy-bridge",
        instructions=(
            f"Tools for the '{bridge.toolset.system}' legacy terminal system. Every tool drives the real screens "
            "and verifies each screen before acting. Write tools return a preview and a confirmation code; show "
            "the preview to the user and call confirm_action only after they agree."
        ),
    )
    for name, tool in bridge.toolset.tools.items():
        if tool.mode == "write" and bridge.writes == "off":
            continue
        server.add_tool(
            _make_tool_fn(bridge, limiter, name, tool),
            name=name,
            description=tool.description,
            annotations=ToolAnnotations(readOnlyHint=tool.mode == "read", destructiveHint=tool.mode == "write",
                                        idempotentHint=tool.mode == "read", openWorldHint=False),
        )

    if bridge.writes == "confirm" and any(t.mode == "write" for t in bridge.toolset.tools.values()):
        async def confirm_action(code: str) -> dict[str, Any]:
            """Execute a write action previously previewed by a write tool, using its confirmation code."""
            try:
                limiter.check("confirm_action")
                return await anyio.to_thread.run_sync(bridge.confirm, code)
            except BridgeError as e:
                raise _error(e) from None

        server.add_tool(confirm_action, name="confirm_action",
                        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
    return server
