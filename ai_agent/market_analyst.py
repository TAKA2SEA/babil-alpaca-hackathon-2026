"""
Stage G - Market Analyst (read-only market context provider).

Gathers read-only market data through the Stage E read-only MCP client
(ReadOnlyMcpClient) and produces two things:

1. A normalized `market_context` dict (clock, account, spot, contracts)
   consumed by babil_proposal_bridge.py - the actual pipeline data.
2. A `format_llm_context()` string that provides READ-ONLY market context
   to an LLM so it can decide only action / underlying / strategy / width
   / rationale (the fixed Stage C Proposal schema).

The LLM is NEVER allowed to decide price, quantity, premium, order id,
strike, account size, max loss/profit, or any execution input - those are
recomputed downstream from real market data. format_llm_context therefore
exposes market structure only (open/close status, spot as reference,
DTE window, available strikes) and deliberately omits bid/ask, premiums,
order data, and account balances.

This module is read-only and pure with respect to transport: it depends
only on the duck-typed `call_tool(name, arguments)` interface of
ReadOnlyMcpClient. It never imports the order-execution module, never
places an order, and only ever requests allowlisted read-only MCP tools.
"""
import datetime as _dt


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_str(value):
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _as_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_contracts(raw):
    if isinstance(raw, dict):
        for key in ("option_contracts", "contracts"):
            val = raw.get(key)
            if isinstance(val, list):
                return val
    return []


def _extract_price(raw):
    for key in ("price", "last_price"):
        val = _field(raw, key, None)
        if val is not None:
            return _as_float(val)
    trades = _field(raw, "trades", None)
    if isinstance(trades, dict):
        for trade in trades.values():
            price = _field(trade, "price", None)
            if price is not None:
                return _as_float(price)
    trade = _field(raw, "latest_trade", None)
    if trade is not None:
        price = _field(trade, "price", None)
        if price is not None:
            return _as_float(price)
    return None


def _extract_quote(raw):
    for key in ("bid_price", "ask_price"):
        if _field(raw, key, None) is None:
            break
    else:
        return raw
    quotes = _field(raw, "quotes", None)
    if isinstance(quotes, dict):
        for q in quotes.values():
            if _field(q, "bid_price", None) is not None:
                return q
    latest = _field(raw, "latest_quote", None)
    if latest is not None:
        return latest
    return raw


def normalize_clock(raw):
    is_open = _field(raw, "is_open", None)
    return {
        "is_open": bool(is_open) if is_open is not None else None,
        "next_open": _as_str(_field(raw, "next_open", "")),
        "next_close": _as_str(_field(raw, "next_close", "")),
    }


def normalize_account(raw):
    return {
        "equity": _as_float(_field(raw, "equity", None)),
        "options_trading_level": _as_int(_field(raw, "options_trading_level", None)),
        "status": _as_str(_field(raw, "status", "")),
        "currency": _as_str(_field(raw, "currency", "")),
    }


def normalize_contract(raw):
    multiplier = _field(raw, "multiplier", None)
    if multiplier is None:
        multiplier = _field(raw, "size", None)
    return {
        "symbol": _as_str(_field(raw, "symbol", "")),
        "underlying_symbol": _as_str(_field(raw, "underlying_symbol", "")),
        "type": _as_str(_field(raw, "type", "")).lower(),
        "strike_price": _as_float(_field(raw, "strike_price", None)),
        "expiration_date": _as_str(_field(raw, "expiration_date", "")),
        "status": _as_str(_field(raw, "status", "")).lower(),
        "tradable": bool(_field(raw, "tradable", False)),
        "multiplier": _as_int(multiplier),
    }


def normalize_spot(raw):
    price = _extract_price(raw)
    return {"symbol": _as_str(_field(raw, "symbol", "")), "price": price}


def normalize_quote(raw):
    q = _extract_quote(raw)
    return {
        "symbol": _as_str(_field(q, "symbol", "")),
        "bid_price": _as_float(_field(q, "bid_price", None)),
        "ask_price": _as_float(_field(q, "ask_price", None)),
    }


class MarketAnalyst:
    """
    Read-only market context provider over a ReadOnlyMcpClient.

    `client` must expose call_tool(name, arguments) (i.e. the Stage E
    ReadOnlyMcpClient or any transport-compatible double).
    """

    # The only MCP tool names this analyst requests - a curated subset of
    # READ_ONLY_MCP_TOOL_NAMES (account/assets/stock-data/options-data).
    REQUESTED_TOOLS = frozenset(
        {
            "get_clock",
            "get_account_info",
            "get_option_contracts",
            "get_stock_latest_trade",
            "get_option_latest_quote",
            "get_news",
        }
    )

    def __init__(self, client):
        self._client = client

    def market_clock(self):
        return normalize_clock(self._client.call_tool("get_clock"))

    def account_summary(self):
        return normalize_account(self._client.call_tool("get_account_info"))

    def option_contracts(self, underlying, option_type, exp_gte, exp_lte):
        raw = self._client.call_tool(
            "get_option_contracts",
            {
                "underlying_symbols": [underlying],
                "type": option_type,
                "status": "active",
                "expiration_date_gte": exp_gte,
                "expiration_date_lte": exp_lte,
            },
        )
        return [normalize_contract(c) for c in _extract_contracts(raw)]

    def spot_price(self, underlying):
        raw = self._client.call_tool("get_stock_latest_trade", {"symbols": underlying})
        price = _extract_price(raw)
        if price is None:
            raise ValueError(f"no spot price returned for {underlying}")
        return price

    def option_quote(self, symbol):
        raw = self._client.call_tool("get_option_latest_quote", {"symbol_or_symbols": symbol})
        return normalize_quote(raw)

    def news_headlines(self, symbols, limit=5):
        raw = self._client.call_tool("get_news", {"symbols": symbols, "limit": limit})
        headlines = []
        if isinstance(raw, dict):
            items = raw.get("news", []) or raw.get("articles", [])
            for item in items:
                headline = _as_str(_field(item, "headline", ""))
                if headline:
                    headlines.append(headline)
        return headlines[:limit]

    def gather_market_context(
        self,
        underlying,
        option_type="call",
        exp_gte=None,
        exp_lte=None,
        include_news=False,
    ):
        if exp_gte is None or exp_lte is None:
            today = _dt.date.today()
            exp_gte = exp_gte or (today + _dt.timedelta(days=7)).isoformat()
            exp_lte = exp_lte or (today + _dt.timedelta(days=45)).isoformat()
        context = {
            "underlying": underlying,
            "option_type": option_type,
            "clock": self.market_clock(),
            "account": self.account_summary(),
            "spot_price": self.spot_price(underlying),
            "expiration_date_gte": exp_gte,
            "expiration_date_lte": exp_lte,
            "contracts": self.option_contracts(underlying, option_type, exp_gte, exp_lte),
        }
        if include_news:
            context["news"] = self.news_headlines([underlying])
        return context

    def format_llm_context(self, context):
        """
        READ-ONLY context for the LLM. Market structure only: open/close
        status, spot as reference, DTE window, available strikes, (news).
        Deliberately omits bid/ask, premiums, order data, quantities, and
        account balances so the AI can never decide those inputs.
        """
        clock = context.get("clock", {})
        account = context.get("account", {})
        spot = context.get("spot_price")
        contracts = context.get("contracts", [])
        option_type = context.get("option_type", "call")

        lines = []
        lines.append(f"Market status: {'OPEN' if clock.get('is_open') else 'CLOSED'}")
        if clock.get("next_open"):
            lines.append(f"Next open: {clock['next_open']}")
        lines.append(f"Account options trading level: {account.get('options_trading_level')}")
        underlying = context.get("underlying", "")
        spot_str = f"{spot:.2f}" if spot is not None else "N/A"
        lines.append(
            f"{underlying}: reference spot ~{spot_str} "
            f"(reference context only - not a decision input)"
        )
        gte = context.get("expiration_date_gte", "")
        lte = context.get("expiration_date_lte", "")
        lines.append(f"DTE window: {gte} .. {lte} ({option_type} contracts available: {len(contracts)})")
        strikes = []
        for c in contracts[:20]:
            strike = c.get("strike_price")
            if strike is not None:
                strikes.append(f"{strike:.2f}")
        if strikes:
            lines.append(f"Available strikes: {', '.join(strikes)}")
        if context.get("news"):
            lines.append("News headlines:")
            lines.extend(f"  - {h}" for h in context["news"])
        lines.append(
            "You may propose only action/underlying/strategy/width/rationale. "
            "Execution inputs (strikes, sizes, costs, order references, account "
            "totals) are never decided by the AI - they are recomputed from real "
            "market data."
        )
        return "\n".join(lines)
