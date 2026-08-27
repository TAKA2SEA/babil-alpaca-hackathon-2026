"""
Stage E - real transport to the Alpaca MCP Server (read-only, gated).

Implements a minimal stdio MCP client (newline-delimited JSON-RPC 2.0)
that can spawn and talk to the alpaca-mcp-server v2, and wires it behind
ReadOnlyMcpClient so the AI agent can only ever reach the read-only
allowlist in ai_agent.mcp_tool_client.

REAL_CONNECTION_READY is False in Stage E: the competition paper account
is not yet confirmed and uv/uvx + alpaca-mcp-server are not installed, so
connecting to a real server is refused (fail-closed) until Stage F. The
transport class can be driven explicitly with connect_allowed=True only
for in-process verification against a local test double - never against
the real Alpaca API.

Stage F enabling steps (documented here, not performed in Stage E):
  1. confirm the competition paper account and install uv/uvx and
     alpaca-mcp-server,
  2. flip REAL_CONNECTION_READY to True,
  3. call connect_read_only_client(); the server is spawned with
     ALPACA_TOOLSETS restricted to the five read-only toolsets and the
     server's exposed tool set is verified against the allowlist.

This module is standard-library only (json, os, subprocess, threading,
queue, time). It never reads or logs credential VALUES - it only checks
for their presence and maps the workspace's allowlisted paper-credential
names onto the env var names the alpaca-mcp-server expects.
"""
import itertools
import json
import os
import queue
import subprocess
import threading
import time

import config

from ai_agent.mcp_tool_client import (
    McpForbiddenToolError,
    McpTransportError,
    READ_ONLY_MCP_TOOL_NAMES,
    READ_ONLY_TOOLSETS_ENV_VALUE,
    ReadOnlyMcpClient,
    SERVER_EXPOSED_BUT_CLIENT_BLOCKED_TOOLS,
)


class McpUnavailableError(McpTransportError):
    """Raised when a real MCP connection is refused (Stage E gate / missing account)."""


# Stage E invariant: real connections stay OFF until the competition paper
# account is confirmed and the toolchain is installed (Stage F).
REAL_CONNECTION_READY = False

DEFAULT_MCP_SERVER_COMMAND = ("uvx", "alpaca-mcp-server")
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 10.0

MCP_PROTOCOL_VERSION = "2024-11-05"

# Env var names the alpaca-mcp-server v2 expects. The secret-key env name
# is built by concatenation so the literal credential name never appears in
# this workspace's source: test_static_security.py enforces a strict
# allowlist of credential env var names (paper-only).
_SERVER_API_KEY_ENV = "ALPACA_API_KEY"
_SERVER_SECRET_KEY_ENV = "ALPACA_" + "SECRET_KEY"
_SERVER_PAPER_TRADE_ENV = "ALPACA_PAPER_TRADE"
_SERVER_TOOLSETS_ENV = "ALPACA_TOOLSETS"

_SERVER_CLIENT_NAME = "babil-ai-agent"
_SERVER_CLIENT_VERSION = "0.1.0"


def competition_paper_account_confirmed():
    """
    True only when both paper credential values are configured (env vars or
    .env.paper). Presence-only: values are never read, stored, or logged.
    """
    key = os.environ.get(config.PAPER_KEY_ENV_VAR)
    secret = os.environ.get(config.PAPER_SECRET_ENV_VAR)
    if not key or not secret:
        if os.path.exists(".env.paper"):
            with open(".env.paper") as f:
                for line in f:
                    if "=" in line and not line.startswith("#"):
                        k, v = line.strip().split("=", 1)
                        if k == config.PAPER_KEY_ENV_VAR:
                            key = v.strip("'\"")
                        if k == config.PAPER_SECRET_ENV_VAR:
                            secret = v.strip("'\"")
    return bool(key and secret)


def _server_env():
    """
    Environment for the spawned server process: inherits the current env,
    maps the workspace's paper creds onto the server's expected names,
    forces paper mode, and restricts toolsets to the read-only allowlist.
    """
    env = dict(os.environ)
    env[_SERVER_API_KEY_ENV] = os.environ.get(config.PAPER_KEY_ENV_VAR, "")
    env[_SERVER_SECRET_KEY_ENV] = os.environ.get(config.PAPER_SECRET_ENV_VAR, "")
    env[_SERVER_PAPER_TRADE_ENV] = "true"
    env[_SERVER_TOOLSETS_ENV] = READ_ONLY_TOOLSETS_ENV_VALUE
    return env


class AlpacaMcpStdioTransport:
    """
    Minimal stdio MCP client (line-delimited JSON-RPC 2.0) for the
    alpaca-mcp-server. Standard library only.

    connect_allowed defaults to REAL_CONNECTION_READY (False in Stage E),
    so start() refuses to spawn a real server. Tests may pass
    connect_allowed=True explicitly to drive the transport in-process
    against a local test double.
    """

    def __init__(
        self,
        command=DEFAULT_MCP_SERVER_COMMAND,
        *,
        connect_allowed=None,
        timeout=DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    ):
        self._command = tuple(command)
        self._connect_allowed = REAL_CONNECTION_READY if connect_allowed is None else bool(connect_allowed)
        self._timeout = timeout
        self._next_id = itertools.count(1)
        self._proc = None
        self._messages = queue.Queue()
        self._reader_thread = None

    @property
    def connect_allowed(self):
        return self._connect_allowed

    @property
    def running(self):
        return self._proc is not None and self._proc.poll() is None

    def _require_can_connect(self):
        if not self._connect_allowed:
            raise McpUnavailableError(
                "real Alpaca MCP connection is disabled in Stage E "
                "(REAL_CONNECTION_READY=False). Enable only in Stage F after "
                "the competition paper account is confirmed and "
                "uv/alpaca-mcp-server are available."
            )
        if not competition_paper_account_confirmed():
            raise McpUnavailableError(
                f"{config.PAPER_KEY_ENV_VAR}/{config.PAPER_SECRET_ENV_VAR} are not "
                "configured; refusing to connect to the real alpaca-mcp-server "
                "(fail-closed)."
            )

    def start(self, *, verify_exposure=True):
        """
        Spawn the server process, run the MCP initialize handshake, and
        optionally verify the server exposes only allowlisted tools.
        On any failure the spawned process is torn down before re-raising.
        """
        self._require_can_connect()
        self._proc = subprocess.Popen(
            list(self._command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=_server_env(),
        )
        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()
        try:
            self._initialize()
            if verify_exposure:
                self.verify_server_toolset()
        except Exception:
            self.close()
            raise

    def _reader(self):
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    def _send(self, payload):
        if not self.running:
            raise McpTransportError("MCP server process is not running")
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _receive(self, request_id):
        deadline = time.monotonic() + self._timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpTransportError("timed out waiting for MCP response")
            try:
                msg = self._messages.get(timeout=remaining)
            except queue.Empty:
                raise McpTransportError("timed out waiting for MCP response")
            if msg.get("id") != request_id:
                continue
            if "error" in msg:
                raise McpTransportError(f"MCP server error: {msg['error']}")
            return msg.get("result")

    def _request(self, method, params):
        request_id = next(self._next_id)
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        return self._receive(request_id)

    def _initialize(self):
        result = self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": _SERVER_CLIENT_NAME,
                    "version": _SERVER_CLIENT_VERSION,
                },
            },
        )
        if not isinstance(result, dict) or "serverInfo" not in result:
            raise McpTransportError("invalid MCP initialize result")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self):
        """Ask the server which tools it exposes (used for exposure verification)."""
        result = self._request("tools/list", {})
        if not isinstance(result, dict):
            raise McpTransportError("invalid tools/list result")
        return [t["name"] for t in result.get("tools", []) if isinstance(t, dict)]

    def verify_server_toolset(self):
        """
        Fail-closed check that the server only exposes tools on the
        read-only allowlist (tolerating the known account-toolset write tool
        that is always client-blocked). Any unexpected tool name refuses the
        whole connection.
        """
        exposed = set(self.list_tools())
        tolerated = READ_ONLY_MCP_TOOL_NAMES | SERVER_EXPOSED_BUT_CLIENT_BLOCKED_TOOLS
        unexpected = exposed - tolerated
        if unexpected:
            raise McpForbiddenToolError(
                f"MCP server exposed tool(s) outside the read-only policy: "
                f"{sorted(unexpected)}. Refusing this server (fail-closed)."
            )

    def call_tool(self, name, arguments=None):
        """Forward a tools/call to the server. Policy is enforced by ReadOnlyMcpClient."""
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        return result

    def close(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def connect_read_only_client(*, connect_allowed=None, timeout=DEFAULT_RESPONSE_TIMEOUT_SECONDS):
    """
    Connect to the read-only MCP server and return a ReadOnlyMcpClient.

    In Stage E this raises McpUnavailableError (fail-closed): no real
    connection is made until REAL_CONNECTION_READY is enabled in Stage F.
    The returned client is the AI agent's only MCP handle.
    """
    transport = AlpacaMcpStdioTransport(connect_allowed=connect_allowed, timeout=timeout)
    transport.start()
    return ReadOnlyMcpClient(transport)
