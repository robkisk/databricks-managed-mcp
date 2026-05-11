#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastmcp>=3.2.0",
#     "databricks-sdk>=0.60.0",
# ]
# ///
"""Minimal warehouse-pinned Databricks SQL MCP server.

A single-file MCP server that demonstrates the two key patterns for building
a custom DBSQL MCP with deterministic warehouse selection:

  1. **Auth pattern** — `WorkspaceClient()` with no arguments uses the Databricks
     SDK default auth chain. The SDK reads `DATABRICKS_CONFIG_PROFILE` (or
     `DATABRICKS_HOST` + `DATABRICKS_TOKEN`) from the process environment and
     builds an authenticated client. The MCP client (Claude Code, Cursor) is
     responsible for setting that env var when it launches this server.

  2. **Warehouse pinning** — every call to `statement_execution.execute_statement`
     takes a required `warehouse_id` argument. We resolve it from (in order):
        a) The caller-supplied `warehouse_id` argument to the tool, if any.
        b) The `DATABRICKS_WAREHOUSE_ID` env var, set at server launch.
     The server controls compute routing — there is no client-side trick the
     calling LLM can use to bypass the pin (unlike the managed
     `/api/2.0/mcp/sql` endpoint, which silently ignores `?warehouse_id=`).

The tool surface is intentionally minimal — one tool, `execute_sql` — so the
example can be read in a single sitting and extended without ceremony.

Run directly with: `uv run server.py`
Or wire into `.mcp.json`: see README.md.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from databricks.sdk import WorkspaceClient
from fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------
# The server name appears in MCP `serverInfo` and is shown in client UIs.
mcp = FastMCP("pinned-sql-mcp")


# Read warehouse pin from env at startup. Log it once to stderr so users can
# confirm pinning is wired correctly. (stderr is safe — stdout is reserved
# for JSON-RPC frames over stdio transport.)
PINNED_WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "").strip()
PROFILE = os.environ.get("DATABRICKS_CONFIG_PROFILE", "").strip()

print(
    f"[pinned-sql-mcp] profile={PROFILE or '(default chain)'} "
    f"warehouse_id={PINNED_WAREHOUSE_ID or '(unset — tools require explicit warehouse_id)'}",
    file=sys.stderr,
    flush=True,
)


# ---------------------------------------------------------------------------
# Tool — execute_sql
# ---------------------------------------------------------------------------
@mcp.tool
def execute_sql(query: str, warehouse_id: str | None = None) -> dict[str, Any]:
    """Execute a SQL query on a Databricks SQL warehouse.

    Args:
        query: The SQL statement (e.g. "SELECT current_user()"). Use fully
            qualified Unity Catalog names (catalog.schema.table) for best
            results.
        warehouse_id: Optional override. When omitted, falls back to the
            `DATABRICKS_WAREHOUSE_ID` env var set at server launch.

    Returns:
        On success: `{status, warehouse_id, columns, rows, row_count}`
        On error: `{error: <message>}` with no row data.

    The `warehouse_id` field in the response echoes which warehouse handled the
    query — callers can use it to confirm pinning is working.
    """
    wh = (warehouse_id or PINNED_WAREHOUSE_ID).strip()
    if not wh:
        return {
            "error": (
                "warehouse_id is required. Pass it as a tool argument, or set "
                "DATABRICKS_WAREHOUSE_ID in the server's environment."
            )
        }

    try:
        w = WorkspaceClient()  # SDK default auth chain
        response = w.statement_execution.execute_statement(
            warehouse_id=wh,
            statement=query,
            wait_timeout="30s",
        )
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    status = response.status.state.value if response.status else "UNKNOWN"

    columns: list[str] = []
    if response.manifest and response.manifest.schema:
        columns = [c.name for c in response.manifest.schema.columns]

    rows: list[list[Any]] = []
    if response.result and response.result.data_array:
        rows = response.result.data_array

    return {
        "status": status,
        "warehouse_id": wh,  # echoed so the caller can verify the pin
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
    }


# ---------------------------------------------------------------------------
# Entrypoint — dual transport (stdio for local dev, HTTP for Databricks Apps)
# ---------------------------------------------------------------------------
# Auto-detect the runtime context:
#   - Databricks Apps platform sets DATABRICKS_APP_PORT in the container env.
#     When present, expose the MCP over HTTP on that port so the platform's
#     reverse proxy can route to it. Path is /mcp (FastMCP default).
#   - Otherwise, fall back to stdio transport, which is what MCP clients like
#     Claude Code and Cursor speak when launching a server as a subprocess.
if __name__ == "__main__":
    app_port = int(os.environ.get("DATABRICKS_APP_PORT", "0"))
    if app_port:
        print(f"[pinned-sql-mcp] HTTP transport on 0.0.0.0:{app_port}/mcp",
              file=sys.stderr, flush=True)
        mcp.run(transport="streamable-http", host="0.0.0.0", port=app_port)
    else:
        print("[pinned-sql-mcp] stdio transport", file=sys.stderr, flush=True)
        mcp.run()
