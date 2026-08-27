"""
Stage C unit tests for ai_agent.options_strategy_mapper - the Proposal to
MLEG mapping.

Verifies: strategy -> option_type/builder mapping, width forwarding as a
selection *target* only, pair selection and leg building via the existing
mleg_builder (pure construction over plain contract dicts), NO_TRADE
having no mleg path, and that no AI-supplied number ever reaches a builder
output. Also structurally asserts the ai_agent package has no path toward
order execution. No network, no order API.
"""
import pathlib
import types

import pytest

import mleg_builder as mb
from alpaca.trading.requests import OptionLegRequest

from ai_agent.options_strategy_mapper import (
    STRATEGY_TO_BUILDER,
    STRATEGY_TO_OPTION_TYPE,
    ProposalMappingError,
    build_spread_from_proposal,
    build_vertical_spread_from_proposal,
    select_vertical_pair_from_proposal,
    strategy_spec_from_proposal,
)
from ai_agent.proposal import OptionsStrategy, ProposalAction, parse_proposal

NOW = "2026-08-26T12:00:00+00:00"


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def make_trade(**overrides):
    base = {
        "action": "TRADE",
        "underlying": "SPY",
        "strategy": "bull_call_spread",
        "width": 5.0,
        "rationale": "bullish",
    }
    base.update(overrides)
    return base


def parse_trade(**overrides):
    return parse_proposal(make_trade(**overrides), now=NOW)


def make_contract(symbol, strike, ctype, expiration="2026-02-01", **overrides):
    base = {
        "symbol": symbol,
        "type": ctype,
        "status": "active",
        "tradable": True,
        "strike_price": str(strike),
        "expiration_date": expiration,
        "underlying_symbol": "SPY",
        "multiplier": "100",
        "size": "100",
    }
    base.update(overrides)
    return base


CALL_LONG = make_contract("SPY260201C00580000", 580, "call")
CALL_SHORT = make_contract("SPY260201C00585000", 585, "call")
PUT_LONG = make_contract("SPY260201P00585000", 585, "put")
PUT_SHORT = make_contract("SPY260201P00580000", 580, "put")


def parse_no_trade():
    return parse_proposal({"action": "NO_TRADE", "underlying": "SPY", "rationale": "no edge"}, now=NOW)


# ---------------------------------------------------------------------------
# strategy -> mleg mapping
# ---------------------------------------------------------------------------


def test_spec_mapping_bull_call_spread():
    proposal = parse_trade(strategy="bull_call_spread", width=7)
    spec = strategy_spec_from_proposal(proposal)
    assert spec["strategy"] == "bull_call_spread"
    assert spec["option_type"] == "call"
    assert spec["target_width"] == 7.0
    assert spec["builder"] is mb.build_vertical_call_spread


def test_spec_mapping_bear_put_spread():
    proposal = parse_trade(strategy="bear_put_spread", width=4)
    spec = strategy_spec_from_proposal(proposal)
    assert spec["strategy"] == "bear_put_spread"
    assert spec["option_type"] == "put"
    assert spec["target_width"] == 4.0
    assert spec["builder"] is mb.build_vertical_put_spread


def test_strategy_maps_are_in_lockstep_with_enum():
    assert set(STRATEGY_TO_OPTION_TYPE) == set(STRATEGY_TO_BUILDER) == set(OptionsStrategy)


def test_spec_rejects_no_trade():
    with pytest.raises(ProposalMappingError):
        strategy_spec_from_proposal(parse_no_trade())


def test_spec_rejects_unknown_strategy():
    fake = types.SimpleNamespace(action=ProposalAction.TRADE, strategy=object())
    with pytest.raises(ProposalMappingError):
        strategy_spec_from_proposal(fake)


# ---------------------------------------------------------------------------
# pair selection via mleg_builder
# ---------------------------------------------------------------------------


def test_select_vertical_pair_call():
    proposal = parse_trade(strategy="bull_call_spread", width=5)
    long_c, short_c = select_vertical_pair_from_proposal(proposal, [CALL_LONG, CALL_SHORT], spot_price=582.0)
    assert _field(long_c, "symbol") == "SPY260201C00580000"
    assert _field(short_c, "symbol") == "SPY260201C00585000"


def test_select_vertical_pair_put():
    proposal = parse_trade(strategy="bear_put_spread", width=5)
    long_c, short_c = select_vertical_pair_from_proposal(proposal, [PUT_SHORT, PUT_LONG], spot_price=582.0)
    assert _field(long_c, "symbol") == "SPY260201P00585000"
    assert _field(short_c, "symbol") == "SPY260201P00580000"


def test_width_is_forwarded_as_selection_target_only():
    # Same market data, different AI width targets -> different pair
    # choice; the AI width never becomes a price or quantity.
    wide = parse_trade(strategy="bull_call_spread", width=10)
    contracts = [CALL_LONG, CALL_SHORT, make_contract("SPY260201C00590000", 590, "call")]
    long_c, short_c = select_vertical_pair_from_proposal(wide, contracts, spot_price=582.0)
    assert _field(long_c, "strike_price") == "580"
    assert _field(short_c, "strike_price") == "590"


# ---------------------------------------------------------------------------
# leg building via mleg_builder
# ---------------------------------------------------------------------------


def test_build_vertical_spread_from_proposal_call():
    proposal = parse_trade(strategy="bull_call_spread", width=5)
    legs, summary = build_vertical_spread_from_proposal(proposal, CALL_LONG, CALL_SHORT)
    assert len(legs) == 2
    assert all(isinstance(leg, OptionLegRequest) for leg in legs)
    assert summary["strategy"] == "bull_call_spread"
    assert summary["long_symbol"] == "SPY260201C00580000"
    assert summary["short_symbol"] == "SPY260201C00585000"
    assert summary["strike_width"] == 5.0
    assert summary["multiplier"] == 100


def test_build_vertical_spread_from_proposal_put():
    proposal = parse_trade(strategy="bear_put_spread", width=5)
    legs, summary = build_vertical_spread_from_proposal(proposal, PUT_LONG, PUT_SHORT)
    assert len(legs) == 2
    assert summary["strategy"] == "bear_put_spread"
    assert summary["long_symbol"] == "SPY260201P00585000"
    assert summary["short_symbol"] == "SPY260201P00580000"
    assert summary["strike_width"] == 5.0


def test_build_spread_from_proposal_integration_call():
    proposal = parse_trade(strategy="bull_call_spread", width=5)
    legs, summary = build_spread_from_proposal(proposal, [CALL_LONG, CALL_SHORT], spot_price=582.0)
    assert len(legs) == 2
    assert summary["long_symbol"] == "SPY260201C00580000"
    assert summary["short_symbol"] == "SPY260201C00585000"


def test_no_trade_has_no_mleg_path():
    proposal = parse_no_trade()
    with pytest.raises(ProposalMappingError):
        strategy_spec_from_proposal(proposal)
    with pytest.raises(ProposalMappingError):
        build_spread_from_proposal(proposal, [CALL_LONG, CALL_SHORT], spot_price=582.0)


def test_builder_summary_contains_no_price_or_pl_numbers():
    proposal = parse_trade(strategy="bull_call_spread", width=5)
    _legs, summary = build_vertical_spread_from_proposal(proposal, CALL_LONG, CALL_SHORT)
    for bad in ("max_profit", "max_loss", "bid", "ask", "net_premium", "entry_price", "qty", "quantity"):
        assert bad not in summary, bad


def test_ai_rationale_numbers_never_reach_builder_output():
    proposal_a = parse_trade(strategy="bull_call_spread", width=5, rationale="one")
    proposal_b = parse_trade(
        strategy="bull_call_spread", width=5, rationale="different reasoning mentioning a fake price 9999"
    )
    _legs_a, summary_a = build_vertical_spread_from_proposal(proposal_a, CALL_LONG, CALL_SHORT)
    _legs_b, summary_b = build_vertical_spread_from_proposal(proposal_b, CALL_LONG, CALL_SHORT)
    assert summary_a == summary_b
    assert "9999" not in str(summary_a)


# ---------------------------------------------------------------------------
# structural: no path toward order execution in the ai_agent package
# ---------------------------------------------------------------------------


def test_ai_agent_package_has_no_execution_path():
    import ai_agent

    src = ""
    for f in sorted(pathlib.Path(ai_agent.__file__).parent.glob("*.py")):
        src += f.read_text(encoding="utf-8") + "\n"
    assert "execution_engine" not in src
    assert "alpaca.trading" not in src
    assert ("submit_" + "order(") not in src
    assert ("cancel_" + "order_by_id(") not in src
    assert ("close_" + "position(") not in src
