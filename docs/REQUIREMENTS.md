# Requirements: Legacy Bridge

## Users

| User | Needs |
|---|---|
| **FDE** | Connect an agent to a legacy terminal system in days, without changing the system |
| **Legacy system owner / IT security** | Assurance the agent can only do approved things, with a full audit trail |
| **AI agent** (via MCP) | Typed tools with clear inputs, JSON outputs and errors |

## Functional requirements (proposed)

| ID | Requirement |
|---|---|
| R1 | Drive a terminal application through a real terminal emulator (VT100/ANSI via `pyte`), with a pluggable driver interface so a 3270 driver can be added later |
| R2 | Screen maps in YAML: screen identification (anchor text at a position), fields (row, column, length, type), keys and navigation |
| R3 | Tool definitions in YAML: inputs, the screen flow (navigate, type, press, assert, read) and outputs |
| R4 | Every step asserts the expected screen before acting; unexpected screens stop the tool with an error that includes the screen text |
| R5 | Safety: read-only by default, `write` tools need an approval flag, allowlisted tools only, input validation, timeouts, rate limits |
| R6 | MCP server exposing every tool (official `mcp` Python SDK, stdio and HTTP transports) |
| R7 | Tamper-evident audit log of every tool call, keystroke and screen read |
| R8 | AI-assisted screen mapping: a model proposes fields from a screen capture; code verifies each position against the screen text |
| R9 | Record to tool: capture a manual session and generate a draft tool definition with parameters |
| R10 | A demo legacy system: a realistic terminal application with menus, lookups, error screens and a write transaction |
| R11 | Robustness tests: wrong screen, error message, moved field, slow response, session timeout |
| R12 | Agent demo: an open-source model (local, free) completes tasks through the MCP tools |

## Out of scope (v1)

- Real mainframe connectivity (TN3270 or TN5250)
- Graphical or desktop applications
- Credential vaulting beyond environment variables

## Confirmed decisions

| Question | Decision |
|---|---|
| Demo legacy system | **Core banking**: account inquiry, customer search, transaction history, funds transfer |
| Terminal scope | **VT100/ANSI first** (runs and is fully testable here); pluggable driver interface for 3270 later |
| Repo name | **legacy-bridge** |
| Models | Free local open models by default (Hugging Face transformers); Claude via any MCP client optional |
