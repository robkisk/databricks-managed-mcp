# Agent guide

Project-specific instructions for AI coding agents (Claude Code, Cursor,
Codex, Aider, etc.) working in this repo. Compatible with the
[agents.md](https://agents.md) convention.

## What this project is

A single-purpose example: a custom Databricks SQL MCP server that pins
queries to a specific SQL warehouse, with the OAuth proxy needed to use
it through a Databricks App deployment. The whole point is to be the
smallest working solution to a single problem the managed
`/api/2.0/mcp/sql` endpoint can't solve today.

## Architectural principles

These are intentional design choices. Don't undo them.

- **One tool surface, one job.** `server.py` exposes one tool:
  `execute_sql`. Adding tools should be a deliberate decision, not
  scope creep. The whole repo is designed to be readable in one sitting.
- **PEP 723 inline deps, not pyproject.toml.** All three Python files
  declare their dependencies in a script header so `uv run` works
  without a project install step. Don't add a `pyproject.toml` — it
  defeats the portability story.
- **Single file per concern.** `server.py` is the MCP, `mcp_proxy.py` is
  the stdio→HTTPS bridge, `smoke_test.py` is the verifier. Don't split
  these into packages unless a real reason demands it.
- **State-delta tests, not response-parse tests.** `smoke_test.py`
  verifies pinning by observing warehouse state transitions on the
  Databricks control plane, not by parsing tool response JSON. A
  misbehaving server can return any string. Keep this methodology.

## File map

| File | Lines | Purpose |
|---|---|---|
| `server.py` | ~145 | The MCP server. Dual transport (stdio + HTTP via `DATABRICKS_APP_PORT`). |
| `mcp_proxy.py` | ~218 | Stdio→HTTPS bridge with Databricks OAuth. For wiring deployed apps into MCP clients. |
| `smoke_test.py` | ~221 | End-to-end verifier. State-delta methodology against the live Databricks control plane. |
| `app.yaml` | ~24 | Databricks Apps deployment config. Binds the warehouse resource. |
| `.env.example` | — | Template for the two required env vars. |
| `.mcp.json.example` | — | MCP client config template (local + deployed modes). |

## Setup

```bash
# Prerequisites: uv + Databricks CLI authenticated to a workspace
export DATABRICKS_CONFIG_PROFILE=DEFAULT
export DATABRICKS_WAREHOUSE_ID=<your-warehouse-id>
```

## Test

```bash
# One-shot verification — confirms the pin works in your workspace
uv run smoke_test.py
```

Output ends with `PASS` when pinning works. The test takes 30-90 seconds
since it cold-starts the warehouse.

## Style

- **Brickster voice in customer-facing prose** (README, .html guides).
  Direct, second-person, no AI vocabulary clusters. See the
  [Databricks style guide](https://brandguides.brandfolder.com/databricks-style-guide).
- **Sentence case headings**, not Title Case.
- **No "we tested" or "we verified" narrative.** Findings are
  authoritative; observations come from direct probing of the live
  service.
- **Code blocks use the minimum needed.** Don't add language tags unless
  syntax highlighting actually helps.

## Security

- **Never commit `.env`, `.databricks/`, or `.mcp.json`.** `.gitignore`
  protects all three. The `.env.example` and `.mcp.json.example`
  templates are committed because they have no secrets.
- **No customer names in any committed content.** This is a public
  reference repo. Customer names live only in local file paths and
  Databricks workspace metadata, never in code or docs.
- **`databricks sync` writes state to `.databricks/`** containing your
  workspace hostname and user email. Already in `.gitignore`. Never
  remove that entry.

## Common tasks

### Add a new tool

```python
@mcp.tool
def list_warehouses() -> list[dict]:
    """List all SQL warehouses in the workspace."""
    w = WorkspaceClient()
    return [{"id": wh.id, "name": wh.name} for wh in w.warehouses.list()]
```

FastMCP picks up the decorator at module load. No registration needed.

### Add a long-running-query tool

Expose `poll_sql_result(statement_id: str)` that calls
`w.statement_execution.get_statement(statement_id)` and shapes the
response the same way `execute_sql` does. Existing `wait_timeout="30s"`
limit is the SDK max.

### Deploy to a different workspace

The deployment commands in the README's "Deploying as a Databricks App"
section work against any workspace. Change `DATABRICKS_CONFIG_PROFILE`
and the warehouse ID, then run them from the cloned repo root.

## Anti-patterns

- **Don't add a `pyproject.toml`** — PEP 723 is the portability story.
- **Don't split files into packages** — one file per concern is the
  readability story.
- **Don't add framework dependencies (FastAPI, Flask, etc.)** for the
  HTTP transport — FastMCP's built-in `streamable-http` already covers
  it.
- **Don't replace `smoke_test.py`'s state-delta methodology with
  response parsing.** Response shapes can lie; warehouse state can't.
- **Don't change `wait_timeout="30s"`** without a clear reason — that's
  the SDK's max blocking wait. Longer-running queries need a separate
  polling tool, not a larger timeout.
- **Don't add a "Verification methodology" section to user-facing
  docs.** State-delta is mentioned briefly in the smoke test
  description; that's enough.

## When unsure

The README is the source of truth for user-facing patterns. The
inline comments in `server.py` and `mcp_proxy.py` are the source of
truth for implementation choices. If the README and the code disagree,
the code wins — open a PR to fix the README.
