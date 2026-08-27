"""
Stage G unit tests - Market Analyst (read-only market context provider).

Verifies normalization of read-only MCP responses into the canonical
shapes consumed by babil_proposal_bridge, that the analyst only ever
requests allowlisted read-only tools, and that the LLM context string
carries market structure while never exposing bid/ask, premiums, order
data, or account balances. Mock-only - no network, no SDK order API.
"""
import datetime as dt
from types import SimpleNamespace

import pytest

from ai_agent.market_analyst import (
    MarketAnalyst,
    normalize_clock,
    normalize_contract,
    normalize_quote,
)
from ai_agent.mcp_tool_client import READ_ONLY_MCP_TOOL_NAMES


def make_contract(**overrides):
    base = {
        "symbol": "SPY260918C10000000",
        "underlying_symbol": "SPY",
        "type": "call",
        "strike_price": 100.0,
        "expiration_date": "2026-09-18",
        "status": "active",
        "tradable": True,
        "multiplier": "100",
    }
    base.update(overrides)
    return base


CLOCK = {"is_open": True, "next_open": "2026-08-28T09:30:00-04:00", "next_close": "2026-08-28T16:00:00-04:00"}
ACCOUNT = {"equity": "100000", "options_trading_level": 3, "status": "ACTIVE", "currency": "USD"}


class FakeReadOnlyClient:
    def __init__(self, contracts=None, spot=100.0, quotes=None):
        self.contracts = contracts or []
        self.spot = spot
        self.quotes = quotes or {}
        self.calls = []

    def call_tool(self, name, arguments=None):
        args = arguments or {}
        self.calls.append(name)
        if name == "get_clock":
            return dict(CLOCK)
        if name == "get_account_info":
            return dict(ACCOUNT)
        if name == "get_option_contracts":
            return {"option_contracts": list(self.contracts)}
        if name == "get_stock_latest_trade":
            return {"symbol": "SPY", "price": self.spot}
        if name == "get_option_latest_quote":
            sym = args.get("symbol_or_symbols")
            return self.quotes.get(sym, {"symbol": sym, "bid_price": None, "ask_price": None})
        if name == "get_news":
            return {"news": [{"headline": "h1"}, {"headline": "h2"}]}
        raise AssertionError(f"unexpected tool {name!r}")


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------


def test_normalize_contract_from_dict_and_model_are_equal():
    raw = make_contract()
    model = SimpleNamespace(**raw)
    assert normalize_contract(raw) == normalize_contract(model)
    norm = normalize_contract(raw)
    assert norm["strike_price"] == 100.0
    assert norm["type"] == "call"
    assert norm["multiplier"] == 100


def test_normalize_contract_uses_size_fallback():
    raw = make_contract(multiplier=None, size="100")
    assert normalize_contract(raw)["multiplier"] == 100


def test_normalize_quote_dict_and_quote_wrapper():
    q = normalize_quote({"symbol": "S", "bid_price": "1.20", "ask_price": "1.50"})
    assert q["bid_price"] == 1.20 and q["ask_price"] == 1.50


# ---------------------------------------------------------------------------
# analyst methods
# ---------------------------------------------------------------------------


def test_market_clock_and_account_summary():
    analyst = MarketAnalyst(FakeReadOnlyClient())
    assert analyst.market_clock()["is_open"] is True
    assert analyst.account_summary()["equity"] == 100000.0
    assert analyst.account_summary()["options_trading_level"] == 3


def test_option_contracts_normalized():
    analyst = MarketAnalyst(FakeReadOnlyClient(contracts=[make_contract()]))
    contracts = analyst.option_contracts("SPY", "call", "2026-08-28", "2026-10-01")
    assert len(contracts) == 1
    assert contracts[0]["type"] == "call"
    assert contracts[0]["strike_price"] == 100.0


def test_spot_price_and_quote():
    analyst = MarketAnalyst(FakeReadOnlyClient(spot=580.12, quotes={"S1": {"bid_price": 1.2, "ask_price": 1.5}}))
    assert analyst.spot_price("SPY") == 580.12
    assert analyst.option_quote("S1")["bid_price"] == 1.2


def test_news_headlines():
    analyst = MarketAnalyst(FakeReadOnlyClient())
    assert analyst.news_headlines(["SPY"]) == ["h1", "h2"]


def test_gather_market_context():
    contracts = [make_contract()]
    analyst = MarketAnalyst(FakeReadOnlyClient(contracts=contracts, spot=100.0))
    ctx = analyst.gather_market_context("SPY", option_type="call", include_news=True)
    assert ctx["underlying"] == "SPY"
    assert ctx["spot_price"] == 100.0
    assert len(ctx["contracts"]) == 1
    assert ctx["clock"]["is_open"] is True
    assert ctx["news"] == ["h1", "h2"]


# ---------------------------------------------------------------------------
# LLM context is read-only structure, never decision inputs
# ---------------------------------------------------------------------------


def test_format_llm_context_omits_decision_inputs():
    contracts = [make_contract(strike_price=580.0)]
    analyst = MarketAnalyst(FakeReadOnlyClient(contracts=contracts, spot=580.0))
    ctx = analyst.gather_market_context("SPY", option_type="call")
    text = analyst.format_llm_context(ctx)
    assert "SPY" in text
    assert "reference spot" in text
    for token in (
        "bid_price",
        "ask_price",
        "premium",
        "quantity",
        "order_id",
        "buying_power",
        "equity",
        "max_loss",
        "max_profit",
    ):
        assert token not in text, f"LLM context must not expose {token!r}"


def test_analyst_only_requests_allowlisted_tools():
    client = FakeReadOnlyClient(contracts=[make_contract()], quotes={})
    analyst = MarketAnalyst(client)
    analyst.gather_market_context("SPY", option_type="call", include_news=True)
    analyst.market_clock()
    analyst.account_summary()
    assert set(client.calls), "expected analyst to make read-only tool calls"
    assert set(client.calls) <= READ_ONLY_MCP_TOOL_NAMES
    assert set(client.calls) <= MarketAnalyst.REQUESTED_TOOLS
