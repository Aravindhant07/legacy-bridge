# Problem Statement: Legacy Bridge

## Context

Banks, insurers, airlines, logistics firms and governments still run core processes on systems
built decades ago. Staff operate them through **text terminals** ("green screens"): menus,
fixed-position fields, function keys. Many of these systems have **no API**.

When a Forward Deployed Engineer is asked to put an AI agent on top of such a process, this is
where projects stall. Missing or legacy APIs are one of the most cited reasons enterprise agents
never reach production.

## Why today's options fall short

| Option | Problem |
|---|---|
| Build an API on the legacy system | Months of work by scarce specialists; often not allowed |
| Classic RPA bots | Brittle scripts, expensive licences, invisible to AI agents |
| Let an AI "look at the screen" (computer use) | Slow, costly per step, and hard to make safe: it can type into the wrong field |
| Mainframe API gateways (z/OSMF, Zowe) | Cover jobs and datasets, not the business transactions staff run through screens |

Open-source attempts to connect AI agents to terminal screens exist only as tutorials, not as
maintained tools with safety controls.

## What we build

An open-source bridge that turns terminal screens into **safe, typed tools** an AI agent can call
through **MCP** (Model Context Protocol, the standard Claude and other agents use for tools):

1. **Screen maps:** a declarative description of each screen: how to recognise it, where its fields
   are, and which keys move between screens.
2. **Tools from flows:** "look up policy", "get claim status" and "add claim note" become typed
   tools with inputs and JSON outputs. The bridge drives the screens underneath.
3. **Safety by design:**
   - Every keystroke is preceded by a check that the expected screen is showing.
   - Tools are read-only by default; write tools need explicit approval.
   - Every action is written to an audit log.
4. **AI-assisted mapping:** a model proposes a screen map from a screen capture, and code verifies
   every field position against the actual screen text.
5. **Record to tool:** the engineer performs a task once and the bridge turns it into a
   parameterised tool.

## Why it fits the portfolio

Project 2 *understands* the process, project 3 *connects* the agent to the systems that run it, and
project 1 *proves* the agent is ready to go live: the full FDE journey.

## Success criteria (proposed)

- An MCP client (e.g. Claude Desktop, or an open-source model with a tool loop) completes multi-step
  tasks on the demo legacy system through the generated tools.
- Tools never act on the wrong screen: unexpected screens (errors, pop-ups, moved fields) stop the
  tool with a clear error. Robustness tests inject these failures.
- AI-assisted mapping recovers ≥ 90% of fields on the demo screens, with every position verified.
- Runs free and offline.
