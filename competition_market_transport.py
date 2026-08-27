"""
Stage G - in-process READ-ONLY transport backed by the Competition paper
account.

Implements the transport `call_tool(name, arguments)` interface (the same
interface consumed by the Stage E ReadOnlyMcpClient) using direct GET-only
alpaca-py calls against the Competition paper account. This lets the
Market Analyst / Proposal Bridge run against REAL read-only market data
in the Stage G smoke without needing the real alpaca-mcp-server (which
stays disabled: REAL_CONNECTION_READY=False) and without touching the Dev
credential path.

Security:
- Only allowlisted read-only MCP tool names are accepted (anything else ->
  McpForbiddenToolError, fail-closed).
- Every handler is a GET call (get_clock, get_account, get_option_contracts,
  get_stock_latest_trade, get_option_latest_quote). No order-mutating API,
  no import of execution_engine, no trading-MCP tool.
- Credentials come only from the isolated Competition namespace
  (competition_account.py); values are never read out or logged.
"""
import os

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient

import competition_account as ca
import contract_discovery as discovery

from ai_agent.mcp_tool_client import (
    McpForbiddenToolError,
    McpTransportError,
    READ_ONLY_MCP_TOOL_NAMES,
)


def _iso(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value is None:
        return ""
    return str(value)


def _as_str(value):
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


class CompetitionReadOnlyTransport:
    """
    GET-only in-process transport over the Competition paper account.
    Constructed from the isolated Competition credential namespace.
    """

    def __init__(self, trading_client=None, data_client=None, stock_data_client=None):
        if not ca.competition_credentials_configured():
            raise ca.CompetitionAccountError(
                f"{ca.COMPETITION_KEY_ID_ENV}/{ca.COMPETITION_SECRET_KEY_ENV} not configured"
            )
        self._trading_client = trading_client or ca.make_competition_trading_client()
        self._data_client = data_client or self._make_data_client()
        self._stock_data_client = stock_data_client or self._make_stock_data_client()

    @staticmethod
    def _competition_credentials():
        return os.environ[ca.COMPETITION_KEY_ID_ENV], os.environ[ca.COMPETITION_SECRET_KEY_ENV]

    def _make_data_client(self):
        key, secret = self._competition_credentials()
        return OptionHistoricalDataClient(key, secret)

    def _make_stock_data_client(self):
        key, secret = self._competition_credentials()
        return StockHistoricalDataClient(key, secret)

    def list_tools(self):
        return sorted(READ_ONLY_MCP_TOOL_NAMES)

    def call_tool(self, name, arguments=None):
        if name not in READ_ONLY_MCP_TOOL_NAMES:
            raise McpForbiddenToolError(
                f"tool {name!r} is not on the read-only allowlist - refusing (fail-closed)"
            )
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise McpTransportError(
                f"tool {name!r} is not implemented by the in-process competition transport"
            )
        return handler(arguments or {})

    # --- allowlisted read-only tool handlers (all GET) --------------------

    def _tool_get_clock(self, args):
        clock = self._trading_client.get_clock()
        return {
            "is_open": bool(getattr(clock, "is_open", None)),
            "next_open": _iso(getattr(clock, "next_open", None)),
            "next_close": _iso(getattr(clock, "next_close", None)),
        }

    def _tool_get_account_info(self, args):
        account = self._trading_client.get_account()
        return {
            "equity": getattr(account, "equity", None),
            "options_trading_level": getattr(account, "options_trading_level", None),
            "status": _as_str(getattr(account, "status", None)),
            "currency": getattr(account, "currency", None),
        }

    def _tool_get_option_contracts(self, args):
        underlying = args.get("underlying_symbol") or args.get("underlying_symbols")
        if isinstance(underlying, (list, tuple)):
            underlying = underlying[0] if underlying else None
        if not underlying:
            raise McpTransportError("get_option_contracts requires underlying_symbols")
        contracts = discovery.fetch_active_contracts(
            underlying,
            trading_client=self._trading_client,
            limit=args.get("limit", 100),
            contract_type=args.get("type"),
            expiration_date_gte=args.get("expiration_date_gte"),
            expiration_date_lte=args.get("expiration_date_lte"),
        )
        return {"option_contracts": contracts}

    def _tool_get_stock_latest_trade(self, args):
        underlying = args.get("symbol") or args.get("symbols")
        if isinstance(underlying, (list, tuple)):
            underlying = underlying[0] if underlying else None
        if not underlying:
            raise McpTransportError("get_stock_latest_trade requires symbols")
        price = discovery.fetch_underlying_spot_price(underlying, stock_data_client=self._stock_data_client)
        return {"symbol": underlying, "price": price}

    def _tool_get_option_latest_quote(self, args):
        symbol = args.get("symbol") or args.get("symbol_or_symbols")
        if isinstance(symbol, (list, tuple)):
            symbol = symbol[0] if symbol else None
        if not symbol:
            raise McpTransportError("get_option_latest_quote requires symbol_or_symbols")
        quote = discovery.fetch_latest_quote(symbol, data_client=self._data_client)
        return {
            "symbol": symbol,
            "bid_price": getattr(quote, "bid_price", None),
            "ask_price": getattr(quote, "ask_price", None),
        }
