"""
Stage E unit tests for ai_agent.mcp_tool_client - the read-only MCP policy
client.

Verifies the fixed allowlist/deny-list invariants and that
ReadOnlyMcpClient fails closed: only allowlisted read-only tools are
forwarded to the transport, and every forbidden/unknown tool raises
McpForbiddenToolError before the transport is ever touched. Pure - no
network, no SDK, no order API.
"""
import pytest

from ai_agent.mcp_tool_client import (
    FORBIDDEN_MCP_TOOLS,
    READ_ONLY_MCP_TOOL_NAMES,
    READ_ONLY_MCP_TOOLSETS,
    READ_ONLY_TOOLSETS_ENV_VALUE,
    READ_ONLY_TOOL_BY_TOOLSET,
    SERVER_EXPOSED_BUT_CLIENT_BLOCKED_TOOLS,
    McpForbiddenToolError,
    ReadOnlyMcpClient,
    read_only_toolsets_env_value,
)


class RecordingTransport:
    """In-process transport double that records every call it receives."""

    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": f"ok:{name}"}], "isError": False}


# ---------------------------------------------------------------------------
# policy constants
# ---------------------------------------------------------------------------


def test_read_only_toolsets_exactly_stage_e_set():
    assert READ_ONLY_MCP_TOOLSETS == {"account", "assets", "stock-data", "options-data", "news"}


def test_read_only_toolsets_env_value_matches():
    assert read_only_toolsets_env_value() == "account,assets,stock-data,options-data,news"
    assert set(READ_ONLY_TOOLSETS_ENV_VALUE.split(",")) == READ_ONLY_MCP_TOOLSETS
    assert "trading" not in READ_ONLY_TOOLSETS_ENV_VALUE


def test_stage_e_forbidden_tokens_present_verbatim():
    for token in (
        "trading",
        "place_order",
        "place_option_order",
        "cancel_order",
        "replace_order",
        "close_position",
        "close_all_positions",
    ):
        assert token in FORBIDDEN_MCP_TOOLS


def test_every_allowlisted_tool_belongs_to_an_allowlisted_toolset():
    assert set(READ_ONLY_TOOL_BY_TOOLSET) == READ_ONLY_MCP_TOOLSETS
    union = frozenset().union(*READ_ONLY_TOOL_BY_TOOLSET.values())
    assert union == READ_ONLY_MCP_TOOL_NAMES


def test_allowlisted_tools_are_get_style():
    assert all(name.startswith("get_") for name in READ_ONLY_MCP_TOOL_NAMES)


def test_allowlist_and_denylist_are_disjoint():
    assert READ_ONLY_MCP_TOOL_NAMES.isdisjoint(FORBIDDEN_MCP_TOOLS)


def test_no_order_mutating_tool_is_allowlisted():
    for name in (
        "place_stock_order",
        "place_crypto_order",
        "place_order",
        "place_option_order",
        "cancel_order_by_id",
        "cancel_all_orders",
        "cancel_order",
        "replace_order_by_id",
        "replace_order",
        "close_position",
        "close_all_positions",
        "exercise_options_position",
        "do_not_exercise_options_position",
    ):
        assert name not in READ_ONLY_MCP_TOOL_NAMES


def test_server_exposed_but_client_blocked_are_not_allowlisted():
    assert SERVER_EXPOSED_BUT_CLIENT_BLOCKED_TOOLS <= FORBIDDEN_MCP_TOOLS
    assert SERVER_EXPOSED_BUT_CLIENT_BLOCKED_TOOLS.isdisjoint(READ_ONLY_MCP_TOOL_NAMES)


def test_trading_toolset_read_tools_are_forbidden():
    for name in (
        "get_orders",
        "get_order_by_id",
        "get_order_by_client_id",
        "get_all_positions",
        "get_open_position",
    ):
        assert name in FORBIDDEN_MCP_TOOLS
        assert name not in READ_ONLY_MCP_TOOL_NAMES


# ---------------------------------------------------------------------------
# ReadOnlyMcpClient behaviour
# ---------------------------------------------------------------------------


def test_available_tools_is_allowlist_only():
    client = ReadOnlyMcpClient(RecordingTransport())
    assert set(client.available_tools) == READ_ONLY_MCP_TOOL_NAMES
    assert client.list_tools() == sorted(READ_ONLY_MCP_TOOL_NAMES)


def test_call_tool_allowlisted_name_is_forwarded():
    transport = RecordingTransport()
    client = ReadOnlyMcpClient(transport)
    result = client.call_tool("get_news", {"symbols": ["SPY"]})
    assert result["content"][0]["text"] == "ok:get_news"
    assert transport.calls == [("get_news", {"symbols": ["SPY"]})]


def test_call_tool_without_arguments_defaults_to_empty_dict():
    transport = RecordingTransport()
    client = ReadOnlyMcpClient(transport)
    client.call_tool("get_clock")
    assert transport.calls == [("get_clock", {})]


@pytest.mark.parametrize(
    "name",
    [
        "trading",
        "place_order",
        "place_option_order",
        "cancel_order",
        "replace_order",
        "close_position",
        "close_all_positions",
        "place_stock_order",
        "cancel_order_by_id",
        "replace_order_by_id",
        "exercise_options_position",
        "get_orders",
        "get_all_positions",
    ],
)
def test_call_tool_forbidden_name_raises_without_touching_transport(name):
    transport = RecordingTransport()
    client = ReadOnlyMcpClient(transport)
    with pytest.raises(McpForbiddenToolError):
        client.call_tool(name)
    assert transport.calls == []


def test_call_tool_unknown_name_raises_fail_closed():
    transport = RecordingTransport()
    client = ReadOnlyMcpClient(transport)
    with pytest.raises(McpForbiddenToolError):
        client.call_tool("some_unknown_tool")
    assert transport.calls == []


def test_client_never_exposes_forbidden_tools_even_if_transport_has_them():
    class MisbehavingTransport:
        def call_tool(self, name, arguments=None):
            return {"content": [{"type": "text", "text": f"executed:{name}"}]}

    client = ReadOnlyMcpClient(MisbehavingTransport())
    exposed = set(client.list_tools())
    assert not (exposed & FORBIDDEN_MCP_TOOLS)
    with pytest.raises(McpForbiddenToolError):
        client.call_tool("place_order")
