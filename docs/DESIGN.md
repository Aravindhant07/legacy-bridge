# Design: Legacy Bridge (draft for approval)

Approved choices: **core banking** demo system · **VT100/ANSI first**, 3270 later through the same
driver interface · repo **legacy-bridge** · free local open models by default.

## Architecture

```
  AI agent (Claude Desktop, any MCP client, or the bundled local-model agent)
        │  MCP: list_tools / call_tool
        ▼
  ┌──────────────────────── MCP server ────────────────────────┐
  │  Safety gate: allowlist · input validation · rate limit ·   │
  │  read-only by default · write tools need confirmation       │
  ├──────────────────────── Tool executor ─────────────────────┤
  │  Runs a tool's steps: goto · expect · fill · press · read · │
  │  read_table (with paging) · fail_if. Asserts the screen     │
  │  before every keystroke; stops on anything unexpected.      │
  ├──────────── Screen maps (YAML) + navigation graph ─────────┤
  │  identify screens by anchor text · field positions ·        │
  │  tables · message line · how to move between screens        │
  ├──────────────────────── Terminal driver ───────────────────┤
  │  VT100 driver: pseudo-terminal + pyte emulator (80x24)      │
  │  (3270 driver plugs in here later)                          │
  └─────────────────────────────┬──────────────────────────────┘
                                │ keystrokes / screen updates
                                ▼
            Legacy app: "Northwind Savings Core Banking" (demo)
  Every tool call, keystroke and screen read → hash-chained audit log
```

## Components

**1. Demo legacy system.** A realistic 80x24 terminal application in Python, run in a
pseudo-terminal exactly like a real legacy app. It has screen IDs top-left, date and time top-right,
a message line on row 23 and a function-key legend on row 24. Tab moves between input fields, Enter
submits, and F3, F7, F8 and F10 work as on real systems.

| Screen | Purpose |
|---|---|
| `SIGNON` | User ID and password |
| `MAIN` | Menu: 1 Account inquiry · 2 Customer search · 3 Transaction history · 4 Funds transfer |
| `ACIN01` | Account inquiry: holder, type, status, balance, available balance, last activity |
| `CUSR01` | Customer search by last name: a list of customers and their accounts |
| `TXHS01` | Transaction history with paging (F7/F8) |
| `FNTR01` → `FNTR02` → `FNTR03` | Funds transfer: enter → confirm with F10 → reference number |

Business rules produce real error screens: account not found, insufficient funds, frozen account,
daily transfer limit. Synthetic data: about 40 customers and 80 accounts.
**Fault injection** for robustness tests: `--slow` (delayed screens), `--drift` (a field moves),
`--popup` (a random system broadcast screen), `--timeout` (the session expires).

**2. Terminal driver.** The interface is `start · send(keys) · screen() · wait(predicate, timeout) · close`.
The VT100 driver runs the app in a pseudo-terminal (Python standard library) and feeds its output into
`pyte`, a pure-Python terminal emulator, to get the 80x24 screen and cursor. It waits for the screen
to settle before reading. A 3270 driver would implement the same five methods.

**3. Screen maps** (`maps/*.yaml`):
```yaml
ACIN01:
  identify: [{row: 1, col: 2, text: "ACIN01"}]
  fields:
    account_no: {row: 4, col: 22, len: 10, input: true, pattern: "^\\d{10}$"}
    holder:     {row: 6, col: 22, len: 30}
    balance:    {row: 9, col: 22, len: 15, type: money}
  message: {row: 23}
  nav: {back: F3}
```

**4. Tools** (`tools/*.yaml`) are built from steps:
```yaml
get_account_balance:
  description: Look up an account's balance and status.
  mode: read
  inputs: {account_no: {type: string, pattern: "^\\d{10}$"}}
  steps:
    - goto: ACIN01                          # navigation graph finds the path from the current screen
    - fill: {account_no: "{account_no}"}
    - press: ENTER
    - expect: ACIN01
      fail_if_message: ["NOT FOUND", "INVALID"]
    - read: [holder, type, status, balance, available]
```
The executor asserts the expected screen before **every** keystroke. Any surprise (an error message,
a pop-up, a field that is not where the map says, a timeout) stops the tool and returns a structured
error that includes the screen text. It never types blind. The session manager signs on using
credentials from environment variables and recovers after a timeout.

**5. Safety.**
- Only listed tools are exposed, and inputs are validated against their patterns.
- Rate limits apply per tool.
- **Write tools use two phases:** `transfer_funds` fills the screens up to the confirmation page and
  returns a preview with a one-time code. Only `confirm_transfer(code)` presses F10. The server runs in
  one of three modes: `--writes off | confirm | on`.

**6. MCP server.** Uses the official `mcp` Python SDK. Each tool is registered with a JSON schema
generated from its YAML. It supports stdio (Claude Desktop config included) and HTTP transports.

**7. Audit log.** A hash chain over every tool call, keystroke (passwords masked) and screen read,
with a `verify` command (the same approach as Shadow Eval).

**8. AI-assisted mapping.** `lbridge map-screen` captures a screen, and a model proposes fields as
labels plus value positions. Code verifies each proposal: the label must exist at that position, the
value region must be inside the screen and must not overlap a label. Rejected proposals are listed
for the engineer.
**Benchmark:** field recall and precision against the hand-written maps of the demo screens, for each model.

**9. Record to tool.** `lbridge record` runs the app in your own terminal and captures the keystrokes
and screens. It then writes a draft tool YAML: values you typed become parameters, screens become
`expect` steps, and the fields on the final screen become outputs.

**10. Agent demo and evaluation.** A small tool-calling loop lets a **local open model** use the MCP
tools. A set of about 12 banking tasks is scored end to end, for example:
- "What is the available balance on account …?"
- "Total balance across all of Patel's accounts"
- "Transfer $50 from … to …", which must go through confirmation.

Each task records success, tool calls and safety-gate refusals. Claude can use the same server
through any MCP client.

## Tech

Python 3.11+, `pyte`, `mcp`, Pydantic, Typer, PyYAML. Optional `torch` + `transformers` for local models.
Tests drive the real demo app through a pseudo-terminal, offline.

## Milestones

1. Demo banking terminal app + fault injection
2. VT100 driver (pseudo-terminal + pyte)
3. Screen maps, navigation, tool executor, sign-on and session recovery
4. Safety gate, audit log, MCP server
5. Robustness tests (wrong screen, error, drift, slow, timeout, pop-up)
6. AI-assisted mapping with verification + benchmark
7. Record to tool
8. Local open-model agent + task evaluation; README with real results
