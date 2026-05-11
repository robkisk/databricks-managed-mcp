# Databricks SQL MCP with Warehouse Pinning

A minimal, working solution for **pinning a Databricks SQL MCP server to a
specific SQL warehouse**. Run it locally as a stdio MCP, or deploy it as a
Databricks App and reach it through the included OAuth proxy.

> **Prefer the visual guide?** Open [`guide.html`](guide.html) for a
> branded, self-contained version of this content with the architecture
> diagrams and side-by-side option comparison.

## The problem this solves

The Databricks-managed MCP endpoint at `/api/2.0/mcp/sql` does **not** let
you pin queries to a specific warehouse from the client. URL path, query
string, HTTP headers, and JSON-RPC arguments are all silently ignored:

| Attempted client-side pin mechanism        | Result                                              |
| ------------------------------------------ | --------------------------------------------------- |
| `/api/2.0/mcp/sql/<warehouse_id>` in path  | HTTP 404 — not implemented                          |
| `?warehouse_id=<id>` in query string       | HTTP 200, parameter silently dropped                |
| `warehouse_id` in `tools/call` arguments   | Server ignores unknown args (not in tool schema)    |
| Custom HTTP headers                        | No documented path; never observed honored          |

By default the endpoint picks a warehouse server-side using internal
heuristics (any RUNNING warehouse > any STOPPED, serverless first,
"shared"-named first, etc.). When you need **predictable warehouse
selection** — for cost attribution, team isolation, performance SLAs, or
compliance — you have two options.

## Two warehouse pinning approaches

### Option A — Per-user default warehouse override (native, no custom code)

The Databricks SQL Warehouses API lets you configure a default warehouse
per user. The managed `/api/2.0/mcp/sql` endpoint **honors this override**
when routing that user's queries. Verified end-to-end with state-delta
testing.

```bash
# Set the override for the current user
databricks warehouses create-default-warehouse-override \
  me CUSTOM \
  --warehouse-id <warehouse-id> \
  --profile <your-profile>

# Read current setting
databricks warehouses get-default-warehouse-override \
  default-warehouse-overrides/me --profile <your-profile>

# Admins can set overrides for any user (replace 'me' with numeric user ID)
databricks warehouses create-default-warehouse-override \
  <user-id> CUSTOM \
  --warehouse-id <warehouse-id> \
  --profile <your-profile>
```

Type values:
- `CUSTOM` — pin to a specific `--warehouse-id`
- `LAST_SELECTED` — use the user's most recently selected warehouse

**Use this when:** every query from a given user should route to the same
warehouse. Common for per-team isolation, cost attribution, or
single-agent-per-user workflows. No code changes needed in your
`.mcp.json` — the managed MCP picks up the override automatically.

Docs:
[admin SQL settings](https://docs.databricks.com/aws/en/admin/sql/) ·
[updateDefaultWarehouseOverride API](https://docs.databricks.com/api/workspace/warehouses/updatedefaultwarehouseoverride)

### Option B — Custom MCP server (this repo)

Run your own MCP server that calls the Databricks SDK directly with an
explicit `warehouse_id`. The rest of this README describes this approach.

**Use this when:**
- You need **per-agent routing** — different agents on the same user's
  machine routing to different warehouses
- You don't have admin access to set overrides for other users
- You want pinning that follows the agent, not the user (e.g., a shared
  agent service principal)
- You need behavior beyond `execute_sql` — custom result shaping,
  additional tools, server-side logging, structured response trimming

The custom server is small (~145 lines) and demonstrates the pattern
end-to-end including Databricks Apps deployment.

## How the custom MCP pins the warehouse (two-layer model)

The custom server reads **two independent environment variables** at startup:

| Env var                       | Purpose         | Used by                                                 |
| ----------------------------- | --------------- | ------------------------------------------------------- |
| `DATABRICKS_CONFIG_PROFILE`   | **Auth**        | Databricks SDK → `WorkspaceClient()` (host + OAuth)     |
| `DATABRICKS_WAREHOUSE_ID`     | **Compute**     | Passed into `statement_execution.execute_statement()`   |

The server fully controls compute routing because **it makes the SDK call**.
There's no client-side trick a calling LLM could use to bypass the pin — the
warehouse is decided where the SDK invocation happens, inside this server's
process. Contrast with the managed MCP, where your client only forwards
JSON-RPC frames to a Databricks-hosted endpoint and has no say in routing
beyond the per-user override Option A configures.

---

# Getting Started

A complete working setup in under 5 minutes.

## Prerequisites

| Tool              | Install                                                                  |
| ----------------- | ------------------------------------------------------------------------ |
| **uv**            | `curl -LsSf https://astral.sh/uv/install.sh \| sh`                       |
| **Databricks CLI**| `brew install databricks/tap/databricks` (or see Databricks docs)        |
| A Databricks workspace + a SQL warehouse you have CAN_USE on             |

You do **not** need to install anything in this repo — `uv run` resolves
dependencies on demand via [PEP 723](https://peps.python.org/pep-0723/)
inline declarations in each script.

## Step 1 — Clone

```bash
git clone https://github.com/robkisk/databricks-mcp-warehouse-pin.git
cd databricks-mcp-warehouse-pin
```

## Step 2 — Authenticate to your Databricks workspace

```bash
databricks auth login --host https://<your-workspace>.cloud.databricks.com
# follow the browser flow; creates a profile in ~/.databrickscfg
```

By default this creates a profile named `DEFAULT`. To use a different
profile name, pass `--profile <name>`. Note which name you used — you'll
reference it everywhere below.

## Step 3 — Find your warehouse ID

```bash
databricks warehouses list --profile DEFAULT
```

Copy the `id` of the warehouse you want pinned (a 16-character hex string,
e.g. `abcdef1234567890`).

## Step 4 — Set environment variables

```bash
cp .env.example .env
# Edit .env and fill in your real profile name + warehouse ID
```

Or just export them in your current shell:

```bash
export DATABRICKS_CONFIG_PROFILE=DEFAULT
export DATABRICKS_WAREHOUSE_ID=<your-warehouse-id>
```

## Step 5 — Verify pinning works (smoke test)

```bash
uv run smoke_test.py
```

The smoke test boots `server.py`, sends a JSON-RPC `execute_sql` call, and
checks **observable side effects on the Databricks control plane** — it
confirms the pinned warehouse transitioned to RUNNING and no other warehouse
was touched.

Expected output (when pinning works):

```
=== Smoke test: pinning warehouse abcdef1234567890 via profile DEFAULT ===

Pre-test states:
  abcdef1234567890  STOPPED  <-- pinned
  fedcba0987654321  STOPPED

Launching: uv run /path/to/server.py

Server: pinned-sql-mcp v3.2.4

Tool response: status=SUCCEEDED warehouse_id=abcdef1234567890 rows=1

Post-test states:
  abcdef1234567890  RUNNING  <-- pinned  (STOPPED -> RUNNING)
  fedcba0987654321  STOPPED

PASS: query woke up pinned warehouse abcdef1234567890 (STOPPED -> RUNNING);
no other warehouse was touched.
```

If the test says PASS, **the pin works in your workspace**. Move on.

## Step 6 — Wire into Claude Code / Cursor

Open `.mcp.json.example`. Copy the `_LOCAL_STDIO_MODE` entry into your real
`.mcp.json` (project-level or `~/.claude/.mcp.json` for global). Replace
the placeholders with your real values:

```json
{
  "mcpServers": {
    "pinned-sql": {
      "command": "uv",
      "args": ["run", "/absolute/path/to/this/repo/server.py"],
      "env": {
        "DATABRICKS_CONFIG_PROFILE": "DEFAULT",
        "DATABRICKS_WAREHOUSE_ID": "<your-warehouse-id>"
      }
    }
  }
}
```

**Restart your MCP client** (Claude Code, Cursor, etc.) — MCP servers only
load at startup. After restart you'll see a tool named
`mcp__pinned-sql__execute_sql` in the tool list. Ask the agent to run a
query like `SELECT current_user(), now()` and it will land on your pinned
warehouse.

---

# What's in this repo

```
.
├── server.py            ← The MCP server. Dual transport (stdio + HTTP). 145 lines.
├── mcp_proxy.py         ← Stdio→HTTPS bridge with Databricks OAuth. For remote app.
├── smoke_test.py        ← End-to-end verifier (state-delta methodology).
├── app.yaml             ← Databricks Apps deployment config.
├── .env.example         ← Env var template.
├── .mcp.json.example    ← MCP client config templates (local + deployed).
├── .gitignore           ← Protects .env and .mcp.json from being committed.
├── LICENSE              ← MIT.
└── README.md            ← You are here.
```

All three Python files use [PEP 723](https://peps.python.org/pep-0723/)
inline dependency declarations — they run via `uv run` with no separate
install step and no shared virtualenv. That's what makes `.mcp.json`
entries portable across machines: the absolute path to a script is enough,
and dependencies resolve at launch.

---

# Two ways to run

The same `server.py` serves both modes — it auto-detects via the
`DATABRICKS_APP_PORT` env var (Databricks Apps injects it; local launch
doesn't).

| Mode | Transport | How clients reach it | When to use |
|---|---|---|---|
| **Local stdio** | stdin/stdout | `.mcp.json` launches `server.py` as a subprocess | Single-user dev; fastest setup; queries run as YOUR identity |
| **Databricks App** | HTTPS | `.mcp.json` launches `mcp_proxy.py` against the deployed URL | Multi-user; centralized warehouse permissions; queries run as the app's SP |

# Deploying as a Databricks App

When you want multiple users to share the same MCP without each running it
locally — or you want warehouse access governed centrally via the app's
service principal — deploy `server.py` to the Databricks Apps platform.

## Prerequisites

- Databricks CLI ≥ 0.250 (`databricks --version`)
- An authenticated profile (Step 2 above)
- Permission to create Databricks Apps in the target workspace

## Deploy command sequence

```bash
APP_NAME=pinned-sql-mcp
PROFILE=DEFAULT
WAREHOUSE_ID=<your-warehouse-id>
USER_NAME=$(databricks current-user me --profile $PROFILE -o json | jq -r .userName)
WORKSPACE_PATH=/Workspace/Users/$USER_NAME/$APP_NAME

# 1. Create the app shell. The platform provisions a service principal
#    and reserves the app's URL. Takes ~90 seconds to reach ACTIVE compute.
databricks apps create $APP_NAME --profile $PROFILE

# 2. Sync source into the workspace (one-shot by default — no --watch).
databricks sync . $WORKSPACE_PATH --profile $PROFILE

# 3. Bind the warehouse as a named resource. Two effects:
#     (a) Auto-injects DATABRICKS_WAREHOUSE_ID into the app env, matching
#         `valueFrom: warehouse` in app.yaml.
#     (b) Grants the app's SP CAN_USE on the warehouse automatically.
databricks apps update $APP_NAME --profile $PROFILE --json "$(cat <<EOF
{
  "name": "$APP_NAME",
  "description": "Warehouse-pinned DBSQL MCP",
  "resources": [{
    "name": "warehouse",
    "sql_warehouse": {"id": "$WAREHOUSE_ID", "permission": "CAN_USE"}
  }]
}
EOF
)"

# 4. Deploy the source code. Returns when the new revision starts running.
databricks apps deploy $APP_NAME \
  --source-code-path $WORKSPACE_PATH \
  --profile $PROFILE

# 5. Get the deployed URL.
databricks apps get $APP_NAME --profile $PROFILE -o json | jq -r .url
# → https://pinned-sql-mcp-<workspace-id>.<cloud>.databricksapps.com
```

## Wire the deployed app into Claude Code

Copy the `_DEPLOYED_APP_MODE` entry from `.mcp.json.example` into your real
`.mcp.json`. Substitute the deployed URL + your profile name:

```json
{
  "mcpServers": {
    "pinned-sql-remote": {
      "command": "uv",
      "args": [
        "run",
        "/absolute/path/to/this/repo/mcp_proxy.py",
        "--server-url",
        "https://pinned-sql-mcp-<workspace-id>.<cloud>.databricksapps.com/mcp",
        "--profile",
        "DEFAULT"
      ]
    }
  }
}
```

Note the URL path is `/mcp` (no trailing slash). The proxy obtains an OAuth
token from your `~/.databrickscfg` profile and forwards JSON-RPC frames with
`Authorization: Bearer <token>`. The Databricks Apps reverse proxy validates
the token, then routes to your server inside the container.

**No `env` block is needed** in this mode — the deployed app already has
its own `DATABRICKS_WAREHOUSE_ID` (from the bound warehouse resource), and
the proxy doesn't need any extra config beyond URL + profile.

## Local vs deployed — what changes for callers

The tool surface is identical (`execute_sql(query, warehouse_id?)`). What
changes is **who executes the query**:

| Aspect | Local stdio | Deployed app |
|---|---|---|
| SDK identity | Your user (via CLI profile) | App service principal |
| `current_user()` returns | Your email | SP UUID |
| Warehouse permission | Inherits your CAN_USE grants | SP needs CAN_USE (auto-granted by binding) |
| UC table grants | Yours | SP's (apply via SQL `GRANT` to SP UUID) |

For end-user-attributed SQL (each calling user runs as themselves), enable
the **Databricks Apps user-token passthrough** preview and switch to OBO
auth. This minimal example uses SP auth because it works without extra
preview flags.

---

# Code walkthrough

## `server.py` — the MCP server (~145 lines)

Five conceptual sections, all in one file:

1. **PEP 723 inline deps** — `fastmcp` and `databricks-sdk`. `uv run` reads
   these and creates an ephemeral env per invocation.
2. **Server creation** — `mcp = FastMCP("pinned-sql-mcp")`. The name shows
   up in MCP `serverInfo`.
3. **Env capture at startup** — `PINNED_WAREHOUSE_ID = os.environ.get(...)`.
   Logged to stderr so you can confirm pinning is wired correctly. stderr
   is safe; stdout is reserved for JSON-RPC frames on stdio transport.
4. **The tool** — `@mcp.tool def execute_sql(...)`. FastMCP introspects
   type hints and builds the JSON schema automatically. The body resolves
   the warehouse (arg → env-var fallback → error), calls the SDK, returns
   a plain dict that FastMCP serializes as the MCP tool result.
5. **Dual-transport entrypoint** — checks `DATABRICKS_APP_PORT`. If set
   (Databricks Apps), runs HTTP on that port at `/mcp`. If unset, runs
   stdio. Same code, two deployment modes.

## `mcp_proxy.py` — stdio→HTTPS bridge (~190 lines)

Why this exists: MCP clients (Claude Code, Cursor) speak stdio — they
launch servers as subprocesses and pipe JSON-RPC over stdin/stdout.
Databricks Apps are HTTP-only and require workspace OAuth. The proxy
bridges the two: reads JSON-RPC from stdin, POSTs to the deployed URL with
`Authorization: Bearer <token>` (from the Databricks SDK using your CLI
profile), writes responses back to stdout. Handles SSE response parsing
and session-ID lifecycle.

## `smoke_test.py` — the end-to-end verifier (~220 lines)

Spawns `server.py` as a subprocess, sends an `initialize` + `tools/call`
sequence over stdio, captures warehouse state before and after, and
reports PASS/FAIL based on the state delta. State-delta verification is
robust because it doesn't depend on parsing response shapes — just on
observable side effects on the Databricks control plane.

## `app.yaml` — Databricks Apps deployment config

```yaml
command: ["uv", "run", "server.py"]
env:
  - name: DATABRICKS_WAREHOUSE_ID
    valueFrom: warehouse
```

The `valueFrom: warehouse` line pulls `DATABRICKS_WAREHOUSE_ID` from a
bound warehouse resource. The binding (created via Step 3 of the deploy
sequence) does two things: auto-injects this env var, and grants the
app's SP `CAN_USE` on the warehouse.

---

# Extending

## Add another tool

```python
@mcp.tool
def list_warehouses() -> list[dict]:
    """List all SQL warehouses in the workspace."""
    w = WorkspaceClient()
    return [
        {"id": wh.id, "name": wh.name, "state": wh.state.value if wh.state else None}
        for wh in w.warehouses.list()
    ]
```

That's it. FastMCP picks up the decorator at module load.

## Polling for long-running queries

`execute_sql` returns within `wait_timeout="30s"`. For queries that take
longer, the SDK returns a `statement_id` you can pass to a follow-up tool:

```python
@mcp.tool
def poll_sql_result(statement_id: str) -> dict:
    """Fetch the result of a long-running query by its statement ID."""
    w = WorkspaceClient()
    resp = w.statement_execution.get_statement(statement_id)
    # ... shape the response the same way execute_sql does
```

## Service principal auth for deployed mode

When `WorkspaceClient()` runs inside a Databricks App, the SDK auto-detects
the SP context via injected env vars (`DATABRICKS_HOST`,
`DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`). The constructor is the
same — only the auth chain that wins changes. So the `execute_sql` body
works in both local and deployed modes with zero conditionals.

---

# Production considerations

- **Error handling** — the example returns `{"error": "..."}` dicts so the
  caller (LLM) can react. For production, also log the exception with full
  traceback to stderr; the dict response is for the agent's consumption.
- **Query timeouts** — `wait_timeout="30s"` is the SDK max. For longer
  queries, expose a `poll_sql_result` tool (see [Extending](#extending)).
- **Row limits** — the SDK call doesn't cap rows. Add `row_limit` and
  `byte_limit` to the SDK call if you need to cap response size for token
  efficiency.
- **Security** — `warehouse_id` is *not* user input the LLM should be
  trusted to set freely. If you expose this tool to untrusted contexts,
  validate that any supplied warehouse ID matches an allowlist, or drop
  the `warehouse_id` argument from the tool signature entirely so the env
  var pin is the only path.
- **Audit trails** — queries run from the deployed app appear in the SQL
  query history attributed to the app's service principal. If you need to
  trace queries back to the calling user, enable user-token passthrough
  (preview) and use `get_user_workspace_client()` instead of
  `WorkspaceClient()`.

---

# License

MIT. See [LICENSE](LICENSE).

# Contributing

Issues and PRs welcome. The example is intentionally minimal — if you have
a use case that needs more tools, more transports, or more auth flows,
fork it. The patterns here are meant to be a starting point, not a
framework.
