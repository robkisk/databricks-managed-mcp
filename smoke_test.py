#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "databricks-sdk>=0.60.0",
# ]
# ///
"""End-to-end smoke test for the warehouse-pinned MCP server.

Verifies the pin actually works using a *state-delta* methodology that doesn't
rely on parsing query results:

  1. Capture all warehouses in the workspace + their current state.
  2. Spawn `server.py` as a subprocess with DATABRICKS_WAREHOUSE_ID pinned.
  3. Send JSON-RPC `initialize` then `tools/call execute_sql` for SELECT 1.
  4. Capture warehouse states again.
  5. Compare: the pinned warehouse must have transitioned to RUNNING.
     Other warehouses' states must not have changed.

This is the *same methodology* we used to verify pinning works end-to-end —
it's robust because it doesn't require parsing implementation-specific response
shapes, just observable side effects on the Databricks control plane.

Run with:

    export DATABRICKS_CONFIG_PROFILE=<your-profile>      # e.g. DEFAULT
    export DATABRICKS_WAREHOUSE_ID=<your-warehouse-id>   # 16-hex-char ID
    uv run smoke_test.py
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, NoReturn, cast

from databricks.sdk import WorkspaceClient

SERVER = Path(__file__).resolve().parent / "server.py"


def fail(msg: str) -> NoReturn:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def snapshot_warehouses(w: WorkspaceClient) -> dict[str, str]:
    """Return {warehouse_id: state_name}. State is e.g. RUNNING, STOPPED, STARTING."""
    return {wh.id: (wh.state.value if wh.state else "UNKNOWN") for wh in w.warehouses.list()}


def read_jsonrpc(proc: subprocess.Popen[bytes], expect_id: int, timeout: float = 90.0) -> dict | None:
    """Read JSON-RPC responses from stdout until we see one with the expected id."""
    stdout = cast(IO[bytes], proc.stdout)  # we set stdin/stdout/stderr=PIPE at spawn
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        ready, _, _ = select.select([stdout], [], [], 1.0)
        if not ready:
            continue
        line = stdout.readline()
        if not line:
            continue
        text = line.decode(errors="replace").strip()
        if not text.startswith("{"):
            continue  # non-JSON line (banner, log) — ignore
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == expect_id:
            return msg
    return None


def send(proc: subprocess.Popen[bytes], msg: dict) -> None:
    stdin = cast(IO[bytes], proc.stdin)
    body = (json.dumps(msg) + "\n").encode()
    stdin.write(body)
    stdin.flush()


def main() -> None:
    profile = os.environ.get("DATABRICKS_CONFIG_PROFILE", "").strip()
    pinned = os.environ.get("DATABRICKS_WAREHOUSE_ID", "").strip()
    if not profile or not pinned:
        fail(
            "Both DATABRICKS_CONFIG_PROFILE and DATABRICKS_WAREHOUSE_ID must be set.\n"
            "  export DATABRICKS_CONFIG_PROFILE=<your-profile>     # e.g. DEFAULT\n"
            "  export DATABRICKS_WAREHOUSE_ID=<your-warehouse-id>  # 16-hex-char ID"
        )

    print(f"=== Smoke test: pinning warehouse {pinned} via profile {profile} ===")

    # 1. Pre-test warehouse snapshot
    w = WorkspaceClient(profile=profile)
    pre = snapshot_warehouses(w)
    if pinned not in pre:
        fail(f"Warehouse {pinned!r} not found in workspace for profile {profile!r}.")
    print("\nPre-test states:")
    for wid, state in sorted(pre.items()):
        marker = "  <-- pinned" if wid == pinned else ""
        print(f"  {wid}  {state}{marker}")

    # 2. Spawn the server with our env
    env = os.environ.copy()
    env["DATABRICKS_CONFIG_PROFILE"] = profile
    env["DATABRICKS_WAREHOUSE_ID"] = pinned

    print(f"\nLaunching: uv run {SERVER}")
    proc = subprocess.Popen(
        ["uv", "run", str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        bufsize=0,
    )
    time.sleep(3)  # let the server boot

    try:
        # 3a. initialize
        send(proc, {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "1.0"},
            },
            "id": 1,
        })
        init_resp = read_jsonrpc(proc, expect_id=1, timeout=30)
        if not init_resp:
            fail("Server did not respond to initialize within 30s.")
        print(f"\nServer: {init_resp['result']['serverInfo']['name']} "
              f"v{init_resp['result']['serverInfo'].get('version','?')}")

        # 3b. initialized notification (no response)
        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        time.sleep(0.5)

        # 3c. tools/call execute_sql — intentionally omit warehouse_id arg so
        # the env-var fallback is what we're testing.
        send(proc, {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "execute_sql",
                "arguments": {"query": "SELECT 1 AS x"},
            },
            "id": 2,
        })
        query_resp = read_jsonrpc(proc, expect_id=2, timeout=90)
        if not query_resp:
            fail("Server did not respond to tools/call within 90s.")

        # The tool's response is embedded in content[0].text as JSON.
        try:
            content_text = query_resp["result"]["content"][0]["text"]
            tool_result = json.loads(content_text) if content_text.startswith("{") else {"raw": content_text}
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            fail(f"Could not parse tool response: {e}\nRaw: {query_resp}")

        print(f"\nTool response: status={tool_result.get('status')} "
              f"warehouse_id={tool_result.get('warehouse_id')} "
              f"rows={tool_result.get('row_count')}")

        if tool_result.get("error"):
            fail(f"Tool returned error: {tool_result['error']}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    # 4. Post-test warehouse snapshot
    time.sleep(2)  # let any state transition settle
    post = snapshot_warehouses(w)
    print("\nPost-test states:")
    for wid, state in sorted(post.items()):
        marker = "  <-- pinned" if wid == pinned else ""
        delta = "" if pre.get(wid) == state else f"  ({pre.get(wid)} -> {state})"
        print(f"  {wid}  {state}{marker}{delta}")

    # 5. Verdict
    pinned_pre = pre.get(pinned, "UNKNOWN")
    pinned_post = post.get(pinned, "UNKNOWN")

    if pinned_pre == "RUNNING":
        # Warehouse was already running before test — can't distinguish via state
        # delta. Fall back to: response echoed pinned warehouse_id.
        if tool_result.get("warehouse_id") == pinned:
            print(f"\nPASS: query ran on pinned warehouse {pinned} "
                  f"(echoed in response; warehouse already RUNNING pre-test).")
            return
        fail(f"Response warehouse_id={tool_result.get('warehouse_id')} != pinned {pinned}")

    # Pinned was STOPPED pre-test — clean delta test
    other_started = [wid for wid, state in post.items()
                     if wid != pinned and state == "RUNNING" and pre.get(wid) != "RUNNING"]
    if other_started:
        fail(f"Wrong warehouse(s) started: {other_started}. Pin did NOT work.")
    if pinned_post not in ("RUNNING", "STARTING"):
        fail(f"Pinned warehouse {pinned} did not start (state={pinned_post}). "
             f"Pin did not route the query here.")

    print(f"\nPASS: query woke up pinned warehouse {pinned} "
          f"({pinned_pre} -> {pinned_post}); no other warehouse was touched.")


if __name__ == "__main__":
    main()
