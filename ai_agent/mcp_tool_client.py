"""
Stage E - read-only Alpaca MCP tool client (strict allowlist policy).

The AI agent's ONLY handle onto the Alpaca MCP Server is a
ReadOnlyMcpClient. That client refuses every tool outside a fixed,
curated, read-only allowlist (fail-closed): an order-mutating tool can
never be forwarded to any transport, no matter what the transport or the
server itself exposes. This is the "no order path from the AI agent"
guarantee at the MCP layer.

Stage E allows exactly the five read-only TOOLSETS of the
alpaca-mcp-server v2 (account, assets, stock-data, options-data, news)
and forbids the trading toolset plus every order/position/config-mutating
tool by name. The five allowlisted toolset names are also what this
workspace sets in the server's ALPACA_TOOLSETS environment variable
(see read_only_toolsets_env_value()) so the restriction is enforced
server-side as well as client-side.

This module is pure: standard library only, no network, no Alpaca SDK
import, no order API, no import of the execution engine.
"""

# ---------------------------------------------------------------------------
# Server-side toolset restriction (ALPACA_TOOLSETS). Exactly the five
# read-only toolsets allowed in Stage E - nothing else (the trading
# toolset, watchlists, crypto-data, corporate-actions, fixed-income-data,
# locates are all absent).
# ---------------------------------------------------------------------------
READ_ONLY_MCP_TOOLSETS = frozenset(
    {"account", "assets", "stock-data", "options-data", "news"}
)

# Canonical, human-readable ALPACA_TOOLSETS value. Must parse back to
# exactly READ_ONLY_MCP_TOOLSETS or the assert below fires at import.
READ_ONLY_TOOLSETS_ENV_VALUE = "account,assets,stock-data,options-data,news"

assert set(READ_ONLY_TOOLSETS_ENV_VALUE.split(",")) == READ_ONLY_MCP_TOOLSETS, (
    "READ_ONLY_TOOLSETS_ENV_VALUE does not match READ_ONLY_MCP_TOOLSETS - fail-closed."
)


def read_only_toolsets_env_value():
    """The exact ALPACA_TOOLSETS value that restricts the server to read-only toolsets."""
    return READ_ONLY_TOOLSETS_ENV_VALUE


# ---------------------------------------------------------------------------
# The read-only tool allowlist. Every tool the AI agent may call, grouped
# by the read-only toolset it belongs to. All of these are GET-style read
# calls against the Alpaca API.
# ---------------------------------------------------------------------------
_ACCOUNT_TOOLS = frozenset(
    {
        "get_account_info",
        "get_account_config",
        "get_portfolio_history",
        "get_account_activities",
        "get_account_activities_by_type",
    }
)

_ASSETS_TOOLS = frozenset(
    {
        "get_all_assets",
        "get_asset",
        "get_option_contracts",
        "get_option_contract",
        "get_calendar",
        "get_clock",
        "get_corporate_action_announcements",
        "get_corporate_action_announcement",
    }
)

_STOCK_DATA_TOOLS = frozenset(
    {
        "get_stock_bars",
        "get_stock_quotes",
        "get_stock_trades",
        "get_stock_latest_bar",
        "get_stock_latest_quote",
        "get_stock_latest_trade",
        "get_stock_snapshot",
        "get_most_active_stocks",
        "get_market_movers",
    }
)

_OPTIONS_DATA_TOOLS = frozenset(
    {
        "get_option_bars",
        "get_option_trades",
        "get_option_latest_trade",
        "get_option_latest_quote",
        "get_option_snapshot",
        "get_option_chain",
        "get_option_exchange_codes",
    }
)

_NEWS_TOOLS = frozenset({"get_news"})

# toolset -> the read-only tool names it exposes. Structural invariant:
# every allowlisted tool belongs to one of the allowlisted toolsets.
READ_ONLY_TOOL_BY_TOOLSET = {
    "account": _ACCOUNT_TOOLS,
    "assets": _ASSETS_TOOLS,
    "stock-data": _STOCK_DATA_TOOLS,
    "options-data": _OPTIONS_DATA_TOOLS,
    "news": _NEWS_TOOLS,
}

assert set(READ_ONLY_TOOL_BY_TOOLSET) == READ_ONLY_MCP_TOOLSETS, (
    "READ_ONLY_TOOL_BY_TOOLSET keys must equal READ_ONLY_MCP_TOOLSETS - fail-closed."
)

READ_ONLY_MCP_TOOL_NAMES = frozenset().union(*READ_ONLY_TOOL_BY_TOOLSET.values())

# ---------------------------------------------------------------------------
# The deny list. Anything here is unreachable through ReadOnlyMcpClient.
# Contains the Stage E forbidden tokens verbatim (trading,
# place_order, place_option_order, cancel_order, replace_order,
# close_position, close_all_positions) plus the real alpaca-mcp-server v2
# order/position/config-mutating tool names and the read-only tools that
# live inside the forbidden trading toolset.
# ---------------------------------------------------------------------------
FORBIDDEN_MCP_TOOLS = frozenset(
    {
        # Stage E forbidden tokens (verbatim).
        "trading",
        "place_order",
        "place_option_order",
        "cancel_order",
        "replace_order",
        "close_position",
        "close_all_positions",
        # Real alpaca-mcp-server v2 order/position/config-mutating tools.
        "place_stock_order",
        "place_crypto_order",
        "cancel_order_by_id",
        "cancel_all_orders",
        "replace_order_by_id",
        "exercise_options_position",
        "do_not_exercise_options_position",
        "update_account_config",
        # Read-only tools inside the forbidden trading toolset (never exposed
        # server-side either, but denied here explicitly as a second net).
        "get_orders",
        "get_order_by_id",
        "get_order_by_client_id",
        "get_all_positions",
        "get_open_position",
    }
)

# Tools the real server WILL expose inside an allowlisted toolset but which
# the client must never call (the account toolset bundles this write tool).
# Server-exposure verification tolerates these by name; the client still
# refuses them. Not order-related, but not read-only either.
SERVER_EXPOSED_BUT_CLIENT_BLOCKED_TOOLS = frozenset({"update_account_config"})

assert FORBIDDEN_MCP_TOOLS >= SERVER_EXPOSED_BUT_CLIENT_BLOCKED_TOOLS, (
    "every server-exposed-but-client-blocked tool must also be in FORBIDDEN_MCP_TOOLS."
)

assert READ_ONLY_MCP_TOOL_NAMES.isdisjoint(FORBIDDEN_MCP_TOOLS), (
    "allowlist and deny list overlap - fail-closed."
)

assert READ_ONLY_MCP_TOOL_NAMES.isdisjoint(SERVER_EXPOSED_BUT_CLIENT_BLOCKED_TOOLS), (
    "server-exposed-but-client-blocked tools must never be allowlisted."
)


class McpError(Exception):
    """Base class for all Stage E MCP errors."""


class McpForbiddenToolError(McpError):
    """Raised when a non-read-only (or unknown) MCP tool is requested."""


class McpTransportError(McpError):
    """Raised when the MCP transport itself fails (framing, handshake, timeout)."""


class ReadOnlyMcpClient:
    """
    The AI agent's only MCP handle.

    Wraps a transport object that must expose `call_tool(name, arguments)`
    (and optionally `list_tools()` and `close()`). The client never
    surfaces or forwards anything outside READ_ONLY_MCP_TOOL_NAMES, so no
    transport - however misbehaving - can be used to reach an
    order-mutating tool through this client.
    """

    def __init__(self, transport):
        self._transport = transport

    @property
    def available_tools(self):
        """The read-only tool surface this client exposes (sorted, for stable output)."""
        return sorted(READ_ONLY_MCP_TOOL_NAMES)

    def list_tools(self):
        """Same as available_tools; the client surface is the allowlist only."""
        return self.available_tools

    def call_tool(self, name, arguments=None):
        """
        Call a read-only MCP tool.

        Raises McpForbiddenToolError for any name outside the read-only
        allowlist BEFORE the transport is touched - a forbidden or unknown
        tool is never forwarded. Only allowlisted names reach the transport.
        """
        if name not in READ_ONLY_MCP_TOOL_NAMES:
            raise McpForbiddenToolError(
                f"tool {name!r} is not on the read-only allowlist. "
                f"Only GET-style tools from the {sorted(READ_ONLY_MCP_TOOLSETS)} "
                "toolsets may be called through the AI agent."
            )
        return self._transport.call_tool(name, arguments or {})

    def close(self):
        close = getattr(self._transport, "close", None)
        if close is not None:
            close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
