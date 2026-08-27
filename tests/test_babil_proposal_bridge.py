"""
Stage G unit tests - BABIL Proposal Bridge.

Verifies the integrated read-only pipeline: Proposal -> read-only market
data (MarketAnalyst over a mock read-only client) -> mleg_builder ->
compute_spread_risk -> G0-G5 -> DRY-RUN ALLOW/REJECT. Proves the output
is a DRY-RUN decision only - never an order - and that the bridge never
imports execution_engine. Mock-only - no network, no order API.
"""
import ast
import datetime as dt
from pathlib import Path

import pytest

from ai_agent.market_analyst import MarketAnalyst
from ai_agent.proposal import parse_proposal

from babil_proposal_bridge import evaluate_proposal

WORKSPACE = Path(__file__).resolve().parent.parent


def _expiry(days=30):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def _sym(underlying, exp, type_, strike):
    strike_part = f"{int(strike * 1000):08d}"
    return f"{underlying}{exp.replace('-', '')}{type_.upper()}{strike_part}"


def make_contract(strike, exp=None, type_="call", underlying="SPY", **overrides):
    exp = exp or _expiry()
    base = {
        "symbol": _sym(underlying, exp, type_, strike),
        "underlying_symbol": underlying,
        "type": type_,
        "strike_price": strike,
        "expiration_date": exp,
        "status": "active",
        "tradable": True,
        "multiplier": "100",
    }
    base.update(overrides)
    return base


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
            return {"is_open": True, "next_open": "", "next_close": ""}
        if name == "get_account_info":
            return {"equity": "100000", "options_trading_level": 3, "status": "ACTIVE", "currency": "USD"}
        if name == "get_option_contracts":
            return {"option_contracts": list(self.contracts)}
        if name == "get_stock_latest_trade":
            return {"symbol": "SPY", "price": self.spot}
        if name == "get_option_latest_quote":
            sym = args.get("symbol_or_symbols")
            return self.quotes.get(sym, {"symbol": sym, "bid_price": None, "ask_price": None})
        raise AssertionError(f"unexpected tool {name!r}")


def _analyst(contracts, spot=100.0, quotes=None):
    return MarketAnalyst(FakeReadOnlyClient(contracts=contracts, spot=spot, quotes=quotes or {}))


def _bull_call_fixture():
    exp = _expiry()
    long_c = make_contract(100.0, exp=exp, type_="call")
    short_c = make_contract(105.0, exp=exp, type_="call")
    quotes = {
        long_c["symbol"]: {"symbol": long_c["symbol"], "bid_price": 1.35, "ask_price": 1.50},
        short_c["symbol"]: {"symbol": short_c["symbol"], "bid_price": 1.30, "ask_price": 1.60},
    }
    return [long_c, short_c], quotes


def _bear_put_fixture():
    exp = _expiry()
    long_c = make_contract(100.0, exp=exp, type_="put")
    short_c = make_contract(95.0, exp=exp, type_="put")
    quotes = {
        long_c["symbol"]: {"symbol": long_c["symbol"], "bid_price": 1.35, "ask_price": 1.50},
        short_c["symbol"]: {"symbol": short_c["symbol"], "bid_price": 1.30, "ask_price": 1.60},
    }
    return [long_c, short_c], quotes


def _proposal(**overrides):
    raw = {
        "action": "TRADE",
        "underlying": "SPY",
        "strategy": "bull_call_spread",
        "width": 5.0,
        "rationale": "stage g bridge test",
    }
    raw.update(overrides)
    return parse_proposal(raw)


# ---------------------------------------------------------------------------
# decision outcomes
# ---------------------------------------------------------------------------


def test_no_trade_proposal_is_rejected():
    contracts, quotes = _bull_call_fixture()
    proposal = parse_proposal(
        {"action": "NO_TRADE", "underlying": "SPY", "rationale": "no edge"}
    )
    decision = evaluate_proposal(proposal, _analyst(contracts, quotes=quotes))
    assert decision["decision"] == "REJECT"
    assert "NO_TRADE" in decision["reason"]
    assert decision["intent"] is None
    assert decision["gates"] == []


def test_invalid_raw_proposal_is_rejected():
    contracts, quotes = _bull_call_fixture()
    decision = evaluate_proposal({"action": "TRADE"}, _analyst(contracts, quotes=quotes))
    assert decision["decision"] == "REJECT"
    assert "parse" in decision["reason"]
    assert decision["intent"] is None


def test_bull_call_allow_when_all_gates_pass():
    contracts, quotes = _bull_call_fixture()
    decision = evaluate_proposal(_proposal(), _analyst(contracts, quotes=quotes))
    assert decision["decision"] == "ALLOW"
    assert all(g["passed"] for g in decision["gates"])
    intent = decision["intent"]
    assert intent is not None
    assert intent["mode"] == "DRY_RUN"
    assert len(intent["legs"]) == 2
    assert intent["simulation_status"] == "DRY-RUN / NO ORDER SUBMITTED"
    assert intent["max_loss"] > 0
    assert intent["max_profit"] > 0


def test_bear_put_allow_when_all_gates_pass():
    contracts, quotes = _bear_put_fixture()
    proposal = _proposal(strategy="bear_put_spread")
    decision = evaluate_proposal(proposal, _analyst(contracts, quotes=quotes))
    assert decision["decision"] == "ALLOW"
    assert decision["strategy"] == "bear_put_spread"


def test_wide_spread_rejected_g2():
    contracts, quotes = _bull_call_fixture()
    long_sym = contracts[0]["symbol"]
    quotes[long_sym] = {"symbol": long_sym, "bid_price": 1.00, "ask_price": 2.00}
    decision = evaluate_proposal(_proposal(), _analyst(contracts, quotes=quotes))
    assert decision["decision"] == "REJECT"
    names = [g["name"] for g in decision["gates"]]
    assert any("G2" in n for n in names)


def test_g5_economic_dominance_rejected():
    contracts, quotes = _bull_call_fixture()
    long_sym = contracts[0]["symbol"]
    short_sym = contracts[1]["symbol"]
    quotes[long_sym] = {"symbol": long_sym, "bid_price": 4.60, "ask_price": 4.90}
    quotes[short_sym] = {"symbol": short_sym, "bid_price": 0.20, "ask_price": 0.30}
    decision = evaluate_proposal(_proposal(), _analyst(contracts, quotes=quotes))
    assert decision["decision"] == "REJECT"
    names = [g["name"] for g in decision["gates"]]
    assert any("G5" in n for n in names)


def test_no_contracts_rejected():
    decision = evaluate_proposal(_proposal(), _analyst([], quotes={}))
    assert decision["decision"] == "REJECT"
    assert "contracts" in decision["reason"].lower()


def test_closed_market_rejected_g0():
    contracts, quotes = _bull_call_fixture()

    class ClosedFake(FakeReadOnlyClient):
        def call_tool(self, name, arguments=None):
            if name == "get_clock":
                return {"is_open": False, "next_open": "2099-01-01T09:30:00-04:00", "next_close": ""}
            return super().call_tool(name, arguments)

    decision = evaluate_proposal(_proposal(), MarketAnalyst(ClosedFake(contracts=contracts, quotes=quotes)))
    assert decision["decision"] == "REJECT"
    names = [g["name"] for g in decision["gates"]]
    assert any("G0" in n for n in names)


# ---------------------------------------------------------------------------
# no execution path
# ---------------------------------------------------------------------------


def test_bridge_never_imports_execution_engine():
    path = WORKSPACE / "babil_proposal_bridge.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    assert "execution_engine" not in mods
    assert "alpaca" not in mods
