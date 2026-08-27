"""
Stage E tests for ai_agent.real_mcp_transport - the gated real MCP stdio
transport.

Verifies:
  - real connections are disabled in Stage E (fail-closed, no subprocess),
  - the competition paper account gate,
  - the server env restricts ALPACA_TOOLSETS to read-only toolsets and
    forces paper mode,
  - a full in-process round trip against a local fake MCP server proves
    the JSON-RPC handshake, tools/list exposure verification, and
    read-only tools/call flow work, and that the client never issues any
    order-related MCP request.

The fake server is a throwaway script written to tmp_path - it never
touches Alpaca. No network, no order API.
"""
import os
import subprocess
import sys

import pytest

from ai_agent import real_mcp_transport as rmt
from ai_agent.mcp_tool_client import (
    READ_ONLY_MCP_TOOL_NAMES,
    READ_ONLY_TOOLSETS_ENV_VALUE,
    McpForbiddenToolError,
    ReadOnlyMcpClient,
)
from ai_agent.real_mcp_transport import (
    REAL_CONNECTION_READY,
    AlpacaMcpStdioTransport,
    McpUnavailableError,
    competition_paper_account_confirmed,
    connect_read_only_client,
)

DUMMY_KEY = "dummy-paper-key"
DUMMY_SECRET = "dummy-paper-secret"


FAKE_SERVER_SCRIPT = r'''import json
import os
import sys

allowed = [t for t in os.environ.get("FAKE_TOOLS", "").split(",") if t]
log_path = os.environ.get("FAKE_LOG")

def log(method):
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(method + "\n")

for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    if method == "initialize":
        log(method)
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "fake-alpaca-mcp", "version": "0.0.0"}}}
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        log(method)
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {
            "tools": [{"name": n, "description": "fake",
                       "inputSchema": {"type": "object", "properties": {}}}
                      for n in allowed]}}
    elif method == "tools/call":
        log(method)
        name = (msg.get("params") or {}).get("name")
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": "ok:" + str(name)}],
            "isError": False}}
    else:
        log(method)
        reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                 "error": {"code": -32601, "message": "method not found"}}
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
'''


def write_fake_server(tmp_path):
    path = tmp_path / "fake_mcp_server.py"
    path.write_text(FAKE_SERVER_SCRIPT, encoding="utf-8")
    return path


def _fake_server_env(monkeypatch, tmp_path, tools_csv, methods_log=None):
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", DUMMY_KEY)
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", DUMMY_SECRET)
    monkeypatch.setenv("FAKE_TOOLS", tools_csv)
    if methods_log is not None:
        monkeypatch.setenv("FAKE_LOG", str(methods_log))


# ---------------------------------------------------------------------------
# Stage E fail-closed gates
# ---------------------------------------------------------------------------


def test_real_connection_disabled_in_stage_e():
    assert REAL_CONNECTION_READY is False


def test_connect_read_only_client_fails_closed_without_subprocess(monkeypatch):
    def _forbidden_popen(*_args, **_kwargs):
        raise AssertionError("subprocess.Popen must never be called in Stage E")

    monkeypatch.setattr(subprocess, "Popen", _forbidden_popen)
    with pytest.raises(McpUnavailableError):
        connect_read_only_client()


def test_transport_start_fails_closed_when_not_allowed():
    transport = AlpacaMcpStdioTransport()  # connect_allowed defaults to REAL_CONNECTION_READY
    assert transport.connect_allowed is False
    with pytest.raises(McpUnavailableError):
        transport.start()


def test_transport_start_requires_account_even_when_allowed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # guarantee no .env.paper in cwd
    monkeypatch.delenv("ALPACA_PAPER_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET_KEY", raising=False)

    def _forbidden_popen(*_args, **_kwargs):
        raise AssertionError("subprocess.Popen must not be reached without credentials")

    monkeypatch.setattr(subprocess, "Popen", _forbidden_popen)
    transport = AlpacaMcpStdioTransport(connect_allowed=True)
    with pytest.raises(McpUnavailableError):
        transport.start()


# ---------------------------------------------------------------------------
# competition paper account confirmation
# ---------------------------------------------------------------------------


def test_competition_paper_account_confirmed_false_without_creds(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALPACA_PAPER_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_SECRET_KEY", raising=False)
    assert competition_paper_account_confirmed() is False


def test_competition_paper_account_confirmed_true_with_creds(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", DUMMY_KEY)
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", DUMMY_SECRET)
    assert competition_paper_account_confirmed() is True


def test_server_env_restricts_toolsets_and_forces_paper(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", DUMMY_KEY)
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", DUMMY_SECRET)
    env = rmt._server_env()
    assert env["ALPACA_TOOLSETS"] == READ_ONLY_TOOLSETS_ENV_VALUE
    assert "trading" not in env["ALPACA_TOOLSETS"]
    assert env["ALPACA_PAPER_TRADE"] == "true"
    assert env[rmt._SERVER_API_KEY_ENV] == DUMMY_KEY
    assert env[rmt._SERVER_SECRET_KEY_ENV] == DUMMY_SECRET


# ---------------------------------------------------------------------------
# in-process verification against a fake MCP server (no real Alpaca)
# ---------------------------------------------------------------------------


def test_in_process_read_only_round_trip(tmp_path, monkeypatch):
    methods_log = tmp_path / "methods.log"
    _fake_server_env(
        monkeypatch,
        tmp_path,
        ",".join(sorted(READ_ONLY_MCP_TOOL_NAMES)),
        methods_log=methods_log,
    )
    fake = write_fake_server(tmp_path)

    transport = AlpacaMcpStdioTransport(
        command=(sys.executable, str(fake)),
        connect_allowed=True,
        timeout=15,
    )
    transport.start()
    try:
        # transport-level tools/list round trip matches server exposure
        assert set(transport.list_tools()) == READ_ONLY_MCP_TOOL_NAMES

        client = ReadOnlyMcpClient(transport)
        assert set(client.list_tools()) == READ_ONLY_MCP_TOOL_NAMES

        result = client.call_tool("get_news", {"symbols": ["SPY"]})
        assert result["content"][0]["text"] == "ok:get_news"
        assert client.call_tool("get_clock")["content"][0]["text"] == "ok:get_clock"
        assert client.call_tool("get_account_info")["content"][0]["text"] == "ok:get_account_info"

        with pytest.raises(McpForbiddenToolError):
            client.call_tool("place_order")
    finally:
        transport.close()

    methods = set(methods_log.read_text(encoding="utf-8").splitlines())
    assert methods <= {"initialize", "tools/list", "tools/call"}


def test_verify_exposure_rejects_order_tool_even_remotely(tmp_path, monkeypatch):
    _fake_server_env(monkeypatch, tmp_path, "get_news,place_order")
    fake = write_fake_server(tmp_path)

    transport = AlpacaMcpStdioTransport(
        command=(sys.executable, str(fake)),
        connect_allowed=True,
        timeout=15,
    )
    with pytest.raises(McpForbiddenToolError):
        transport.start()
    assert not transport.running


def test_call_tool_after_close_raises_transport_error(tmp_path, monkeypatch):
    _fake_server_env(
        monkeypatch,
        tmp_path,
        ",".join(sorted(READ_ONLY_MCP_TOOL_NAMES)),
    )
    fake = write_fake_server(tmp_path)

    transport = AlpacaMcpStdioTransport(
        command=(sys.executable, str(fake)),
        connect_allowed=True,
        timeout=15,
    )
    transport.start()
    transport.close()
    with pytest.raises(rmt.McpTransportError):
        transport.call_tool("get_news")
