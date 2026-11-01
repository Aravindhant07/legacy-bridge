"""`lbridge` command-line interface."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Turn legacy terminal applications into safe, typed tools for AI agents (MCP).")

ROOT = Path(__file__).resolve().parents[1]
MapOpt = typer.Option(ROOT / "maps" / "northwind_bank.yaml", "--map", help="Screen map YAML.")
ToolsOpt = typer.Option(ROOT / "tools" / "northwind_bank.yaml", "--tools", help="Tool definitions YAML.")
CmdOpt = typer.Option(None, "--command", "-c", help="Terminal program to drive (default: the bundled demo bank).")
AuditOpt = typer.Option(Path("audit.jsonl"), "--audit", help="Audit log file.")


def make_bridge(map_path: Path, tools_path: Path, command: Optional[str], audit: Path, writes: str = "confirm",
                relocate_drift: bool = False, demo_flags: str = ""):
    from .audit import AuditLog
    from .bridge import Bridge
    from .driver import VTDriver, demo_bank_argv
    from .screenmap import ScreenMap
    from .tools import ToolSet

    argv = shlex.split(command) if command else demo_bank_argv(*shlex.split(demo_flags))
    env = {"PYTHONPATH": str(ROOT)} if not command else {}
    creds = None
    if not command:  # demo credentials, unless the environment provides others
        import os

        creds = (os.environ.get("BANK_USER", "TELLER01"), os.environ.get("BANK_PASS", "PASS2024"))
    return Bridge(ScreenMap.load(map_path), ToolSet.load(tools_path), lambda: VTDriver(argv, env=env), AuditLog(audit),
                  credentials=creds, writes=writes, relocate_drift=relocate_drift)


@app.command()
def serve(
    map_path: Path = MapOpt, tools_path: Path = ToolsOpt, command: Optional[str] = CmdOpt, audit: Path = AuditOpt,
    writes: str = typer.Option("confirm", "--writes", help="off | confirm | on"),
    transport: str = typer.Option("stdio", "--transport", help="stdio | streamable-http"),
    port: int = typer.Option(8200, "--port"),
    rate: int = typer.Option(30, "--rate", help="Max calls per tool per minute (0 = unlimited)."),
    relocate_drift: bool = typer.Option(False, "--relocate-drift", help="Read moved fields relative to their label instead of refusing."),
    demo_flags: str = typer.Option("", "--demo-flags", help="Fault-injection flags for the demo app, e.g. '--popup 0.3'."),
) -> None:
    """Run the MCP server (stdio for Claude Desktop and other local clients, or streamable HTTP)."""
    from .mcp_server import build_server

    bridge = make_bridge(map_path, tools_path, command, audit, writes, relocate_drift, demo_flags)
    server = build_server(bridge, rate)
    try:
        if transport == "stdio":
            server.run("stdio")
        else:
            print(f"MCP server on http://127.0.0.1:{port}/mcp", file=sys.stderr)
            server.run("streamable-http", host="127.0.0.1", port=port)
    finally:
        bridge.close()


@app.command()
def call(
    tool: str = typer.Argument(..., help="Tool name."),
    args: list[str] = typer.Argument(None, help="Arguments as key=value."),
    map_path: Path = MapOpt, tools_path: Path = ToolsOpt, command: Optional[str] = CmdOpt, audit: Path = AuditOpt,
    yes: bool = typer.Option(False, "--yes", help="Confirm write actions without asking."),
) -> None:
    """Run one tool from the command line (write tools show a preview and ask before confirming)."""
    from .bridge import BridgeError

    kwargs = dict(a.split("=", 1) for a in (args or []))
    bridge = make_bridge(map_path, tools_path, command, audit)
    try:
        out = bridge.call(tool, kwargs)
        typer.echo(json.dumps(out, indent=2))
        if out.get("status") == "awaiting_confirmation":
            if yes or typer.confirm("Execute this action?"):
                typer.echo(json.dumps(bridge.confirm(out["confirmation_code"]), indent=2))
    except BridgeError as e:
        typer.echo(json.dumps(e.as_dict(), indent=2))
        raise typer.Exit(1)
    finally:
        bridge.close()


@app.command("list-tools")
def list_tools(tools_path: Path = ToolsOpt) -> None:
    """Show the tools an agent would see."""
    from .tools import ToolSet

    for name, t in ToolSet.load(tools_path).tools.items():
        typer.echo(f"{name} [{t.mode}]: {t.description}")
        for a, d in t.inputs.items():
            typer.echo(f"    {a}: {d.type}{' ' + d.pattern if d.pattern else ''}{' (default ' + str(d.default) + ')' if d.default is not None else ''}")


@app.command("demo-app")
def demo_app(flags: str = typer.Argument("", help="e.g. '--popup 0.3 --slow 0.5'")) -> None:
    """Open the demo legacy banking system in this terminal (sign on with TELLER01 / PASS2024)."""
    import os

    os.execvp(sys.executable, [sys.executable, "-m", "legacy_bridge.demo.bank_app", *shlex.split(flags)])


@app.command("verify-audit")
def verify_audit(audit: Path = AuditOpt) -> None:
    """Check the hash-chained audit log for edits or deletions."""
    from .audit import verify

    ok, n, reason = verify(audit)
    typer.echo(f"{'OK' if ok else 'TAMPERED'}: {n} records, {reason}")
    if not ok:
        raise typer.Exit(1)


@app.command("claude-config")
def claude_config() -> None:
    """Print a Claude Desktop config snippet for this server."""
    cfg = {"mcpServers": {"legacy-bridge": {"command": sys.executable, "args": ["-m", "legacy_bridge.cli", "serve"],
                                            "env": {"PYTHONPATH": str(ROOT)}}}}
    typer.echo(json.dumps(cfg, indent=2))


if __name__ == "__main__":
    app()
