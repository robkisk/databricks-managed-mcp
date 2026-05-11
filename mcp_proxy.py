#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.60.0"]
# ///
"""Stdio→HTTPS MCP proxy with Databricks OAuth.

Why this exists: MCP clients (Claude Code, Cursor) speak stdio — they launch
servers as subprocesses and pipe JSON-RPC over stdin/stdout. Databricks Apps,
however, are HTTP-only and require workspace OAuth on every request. This
proxy bridges the gap:

  client stdio  <-->  this proxy  <--HTTPS+OAuth-->  Databricks App

Run via `uv run mcp_proxy.py --server-url <url> --profile <name>`. PEP 723
inline dependencies mean no separate venv is needed.

Authentication uses the Databricks SDK's default chain via `--profile`,
which reads host + auth method from ~/.databrickscfg. On first call the
browser may open for the OAuth code flow; subsequent calls use the cached
token at ~/.databricks/token-cache.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from databricks.sdk import WorkspaceClient

log = logging.getLogger("mcp-proxy")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[mcp-proxy] %(levelname)s: %(message)s",
)

# ---------------------------------------------------------------------------
# CLI arguments
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="MCP stdio→HTTPS proxy with Databricks OAuth")
parser.add_argument("--server-url", help="Full MCP server URL (use this for deployed apps).")
parser.add_argument("--host", default="", help="Databricks workspace URL (alternative to --profile).")
parser.add_argument("--profile", default="", help="Profile name in ~/.databrickscfg.")
parser.add_argument("--path", default="",
                    help="API path appended to profile host (alternative to --server-url, "
                         "e.g. /api/2.0/mcp/sql for managed MCPs).")
args = parser.parse_args()


def _build_client() -> WorkspaceClient:
    if args.profile:
        return WorkspaceClient(profile=args.profile)
    kwargs: dict = {"auth_type": "external-browser"}
    if args.host:
        kwargs["host"] = args.host
    return WorkspaceClient(**kwargs)


# Build the workspace client up front. This triggers OAuth (possibly opening
# a browser) before we accept any stdin messages — simpler than threaded auth.
client = _build_client()

# Resolve the target URL
if args.server_url:
    SERVER_URL = args.server_url
elif args.path:
    SERVER_URL = client.config.host.rstrip("/") + args.path
else:
    parser.error("Pass --server-url for a custom MCP, or --path for a managed MCP endpoint.")

log.info("Proxying stdio → %s", SERVER_URL)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
mcp_session_id: str | None = None
last_init_params: dict | None = None


class SessionExpiredError(Exception):
    """Raised when the server returns 404/410 for an existing session."""


def send_http(body: bytes) -> list[str]:
    """POST one JSON-RPC frame to the remote MCP server, return response strings.

    Handles both SSE (text/event-stream) and plain-JSON responses. Captures
    Mcp-Session-Id from response headers so subsequent calls reuse the session.
    """
    global mcp_session_id

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    headers.update(client.config.authenticate())  # adds Authorization: Bearer ...
    if mcp_session_id:
        headers["Mcp-Session-Id"] = mcp_session_id

    req = Request(SERVER_URL, data=body, headers=headers, method="POST")
    try:
        resp = urlopen(req, timeout=120)
    except HTTPError as exc:
        if exc.code in (404, 410) and mcp_session_id:
            raise SessionExpiredError() from exc
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot reach {SERVER_URL}: {exc.reason}") from exc

    session = resp.headers.get("Mcp-Session-Id")
    if session:
        mcp_session_id = session

    if resp.status == 202:
        return []  # accepted, no body

    data = resp.read().decode()
    if "text/event-stream" in resp.headers.get("Content-Type", ""):
        # Parse SSE frames: lines starting "data:" are payload, blank lines separate events.
        events, buf = [], []
        for line in data.split("\n"):
            if line.startswith("data: "):
                buf.append(line[6:])
            elif line == "" and buf:
                events.append("\n".join(buf))
                buf = []
        if buf:
            events.append("\n".join(buf))
        return events

    return [data] if data.strip() else []


def reinitialize_session() -> bool:
    """Re-run `initialize` after the server invalidates our session."""
    global mcp_session_id
    mcp_session_id = None
    log.info("Re-initializing MCP session…")
    init = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": last_init_params or {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-proxy", "version": "1.0"},
        },
        "id": f"reinit-{uuid.uuid4()}",
    }
    try:
        send_http(json.dumps(init).encode())
        send_http(b'{"jsonrpc":"2.0","method":"notifications/initialized"}')
        log.info("Session re-initialized")
        return True
    except Exception as exc:
        log.error("Re-init failed: %s", exc)
        return False


def process_message(line: str) -> None:
    """Process one JSON-RPC frame from stdin and write the response to stdout."""
    global last_init_params
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        log.warning("Skipping malformed JSON: %.100s", line)
        return

    if request.get("method") == "initialize":
        last_init_params = request.get("params")

    try:
        responses = send_http(line.encode())
    except SessionExpiredError:
        if reinitialize_session():
            try:
                responses = send_http(line.encode())
            except Exception as exc:
                responses, err = None, str(exc)
        else:
            responses, err = None, "Session expired and re-init failed"
    except Exception as exc:
        responses, err = None, str(exc)

    if responses is not None:
        for resp in responses:
            sys.stdout.write(resp + "\n")
            sys.stdout.flush()
    elif "id" in request:
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "error": {"code": -32603, "message": err},
            "id": request["id"],
        }) + "\n")
        sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if line:
            process_message(line)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log.error("Fatal: %s", exc, exc_info=True)
        sys.exit(1)
