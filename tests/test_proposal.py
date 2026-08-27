"""
Stage C unit tests for ai_agent.proposal - the fixed AI Proposal schema.

Verifies: valid TRADE / NO_TRADE parsing, JSON-string input, action and
strategy normalization, rejection of malformed output, rejection of every
AI decision input (strike, bid/ask, price, quantity, max loss/profit,
expected return, order id, account/position size, contract), and the
guarantee that the Proposal object itself carries no decision numbers.
Pure - no network, no SDK, no order API.
"""
import json

import pytest

from ai_agent.proposal import (
    AI_DECISION_INPUT_KEYS,
    ALLOWED_KEYS,
    DEFAULT_ALLOWED_STRATEGIES,
    MAX_RATIONALE_LEN,
    MAX_WIDTH_INCLUSIVE,
    OptionsStrategy,
    Proposal,
    ProposalAction,
    ProposalValidationError,
    parse_proposal,
    parse_proposal_safe,
)

NOW = "2026-08-26T12:00:00+00:00"


def make_trade(**overrides):
    base = {
        "action": "TRADE",
        "underlying": "SPY",
        "strategy": "bull_call_spread",
        "width": 5.0,
        "rationale": "bullish on SPY, market structure supportive",
    }
    base.update(overrides)
    return base


def make_no_trade(**overrides):
    base = {
        "action": "NO_TRADE",
        "underlying": "SPY",
        "rationale": "no edge: low conviction, wide market spread",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# valid parsing
# ---------------------------------------------------------------------------


def test_parse_valid_trade_proposal():
    p = parse_proposal(make_trade(), now=NOW)
    assert isinstance(p, Proposal)
    assert p.action is ProposalAction.TRADE
    assert p.underlying == "SPY"
    assert p.strategy is OptionsStrategy.BULL_CALL_SPREAD
    assert p.width == 5.0
    assert p.rationale == "bullish on SPY, market structure supportive"
    assert p.generated_at == NOW


def test_parse_valid_no_trade_proposal():
    p = parse_proposal(make_no_trade(), now=NOW)
    assert p.action is ProposalAction.NO_TRADE
    assert p.strategy is None
    assert p.width is None
    assert p.generated_at == NOW


def test_parse_json_string_input():
    payload = make_trade(strategy="bear_put_spread")
    p = parse_proposal(json.dumps(payload), now=NOW)
    assert p.strategy is OptionsStrategy.BEAR_PUT_SPREAD


def test_action_normalisation_is_case_insensitive():
    assert parse_proposal(make_trade(action="trade"), now=NOW).action is ProposalAction.TRADE
    assert parse_proposal(make_no_trade(action="no_trade"), now=NOW).action is ProposalAction.NO_TRADE


def test_underlying_is_uppercased_and_stripped():
    p = parse_proposal(make_trade(underlying="  spy "), now=NOW)
    assert p.underlying == "SPY"


def test_width_accepts_int_and_converts_to_float():
    p = parse_proposal(make_trade(width=5), now=NOW)
    assert p.width == 5.0


def test_allowed_strategies_match_mleg_builder_surface():
    assert DEFAULT_ALLOWED_STRATEGIES == {"bull_call_spread", "bear_put_spread"}


def test_to_dict_contains_only_schema_keys():
    p = parse_proposal(make_trade(), now=NOW)
    assert set(p.to_dict()) == {"action", "underlying", "strategy", "width", "rationale", "generated_at"}


def test_proposal_object_has_no_decision_number_fields():
    fields = {f.name for f in Proposal.__dataclass_fields__.values()}
    assert fields == {"action", "underlying", "strategy", "width", "rationale", "generated_at"}
    for bad in AI_DECISION_INPUT_KEYS:
        assert bad not in fields
    p = parse_proposal(make_trade(), now=NOW)
    for bad in AI_DECISION_INPUT_KEYS:
        assert bad not in p.to_dict()


def test_generated_at_defaults_to_utc_isoformat():
    p = parse_proposal(make_trade())
    assert "T" in p.generated_at
    assert p.generated_at.endswith("+00:00")


def test_parse_proposal_safe_ok():
    proposal, err = parse_proposal_safe(make_trade(), now=NOW)
    assert err is None
    assert proposal is not None
    assert proposal.width == 5.0


# ---------------------------------------------------------------------------
# rejection: action / strategy / width / underlying / rationale
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["BUY", "SELL", "HOLD", "TRADE_BIG", "no trade", "", 1, None, True])
def test_reject_invalid_action(bad):
    with pytest.raises(ProposalValidationError):
        parse_proposal(make_trade(action=bad), now=NOW)


def test_reject_missing_action():
    with pytest.raises(ProposalValidationError):
        parse_proposal({"underlying": "SPY", "strategy": "bull_call_spread", "width": 5, "rationale": "x"}, now=NOW)


@pytest.mark.parametrize("bad", ["iron_condor", "straddle", "call", "", 1, None])
def test_reject_unknown_or_missing_strategy(bad):
    payload = make_trade(strategy=bad)
    with pytest.raises(ProposalValidationError):
        parse_proposal(payload, now=NOW)


def test_reject_strategy_not_in_custom_allowlist():
    payload = make_trade(strategy="bull_call_spread")
    with pytest.raises(ProposalValidationError):
        parse_proposal(payload, allowed_strategies={"bear_put_spread"}, now=NOW)


def test_reject_no_trade_carrying_strategy():
    with pytest.raises(ProposalValidationError):
        parse_proposal(make_no_trade(strategy="bull_call_spread"), now=NOW)


def test_reject_no_trade_carrying_width():
    with pytest.raises(ProposalValidationError):
        parse_proposal(make_no_trade(width=5.0), now=NOW)


def test_reject_missing_width_for_trade():
    payload = {k: v for k, v in make_trade().items() if k != "width"}
    with pytest.raises(ProposalValidationError):
        parse_proposal(payload, now=NOW)


@pytest.mark.parametrize(
    "bad",
    [0, -1, -0.01, "abc", "5.5x", float("inf"), float("-inf"), float("nan"), True, False, None],
)
def test_reject_invalid_width(bad):
    with pytest.raises(ProposalValidationError):
        parse_proposal(make_trade(width=bad), now=NOW)


def test_reject_width_above_sane_max():
    with pytest.raises(ProposalValidationError):
        parse_proposal(make_trade(width=MAX_WIDTH_INCLUSIVE + 1), now=NOW)


def test_reject_width_at_zero_boundary():
    with pytest.raises(ProposalValidationError):
        parse_proposal(make_trade(width=0.0), now=NOW)


def test_reject_invalid_underlying():
    for bad in ["", "   ", 123, None, "THIS_IS_WAY_TOO_LONG_12345", "SP Y", "SPY!!"]:
        with pytest.raises(ProposalValidationError):
            parse_proposal(make_trade(underlying=bad), now=NOW)


def test_reject_missing_underlying():
    payload = {k: v for k, v in make_trade().items() if k != "underlying"}
    with pytest.raises(ProposalValidationError):
        parse_proposal(payload, now=NOW)


def test_reject_missing_or_empty_rationale():
    with pytest.raises(ProposalValidationError):
        parse_proposal(make_trade(rationale=""), now=NOW)
    with pytest.raises(ProposalValidationError):
        parse_proposal({k: v for k, v in make_trade().items() if k != "rationale"}, now=NOW)
    with pytest.raises(ProposalValidationError):
        parse_proposal(make_trade(rationale="   "), now=NOW)


def test_reject_rationale_too_long():
    with pytest.raises(ProposalValidationError):
        parse_proposal(make_trade(rationale="x" * (MAX_RATIONALE_LEN + 1)), now=NOW)


def test_reject_rationale_not_string():
    with pytest.raises(ProposalValidationError):
        parse_proposal(make_trade(rationale=12345), now=NOW)


# ---------------------------------------------------------------------------
# rejection: AI decision inputs / unexpected fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_key", sorted(AI_DECISION_INPUT_KEYS))
def test_reject_each_ai_decision_input_key(bad_key):
    payload = make_trade()
    payload[bad_key] = 123.45
    with pytest.raises(ProposalValidationError) as exc:
        parse_proposal(payload, now=NOW)
    assert bad_key in str(exc.value)


def test_reject_any_unexpected_field():
    with pytest.raises(ProposalValidationError):
        parse_proposal(make_trade(my_custom_price=99.9), now=NOW)


def test_ai_decision_input_list_covers_the_forbidden_set():
    required = {
        "strike",
        "strike_price",
        "bid",
        "ask",
        "entry_price",
        "price",
        "quantity",
        "qty",
        "max_loss",
        "max_profit",
        "expected_return",
        "order_id",
        "client_order_id",
        "account_size",
        "position_size",
        "contract_symbol",
        "option_symbol",
    }
    assert required <= AI_DECISION_INPUT_KEYS


# ---------------------------------------------------------------------------
# rejection: malformed container / JSON
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [[], [1, 2], 5, 5.0, None, object()])
def test_reject_non_dict_or_string(raw):
    with pytest.raises(ProposalValidationError):
        parse_proposal(raw, now=NOW)


def test_reject_invalid_json_string():
    with pytest.raises(ProposalValidationError):
        parse_proposal("{not json", now=NOW)


def test_reject_json_string_that_is_not_an_object():
    with pytest.raises(ProposalValidationError):
        parse_proposal("[1,2,3]", now=NOW)


def test_parse_proposal_safe_returns_reason_on_bad_input():
    proposal, err = parse_proposal_safe({"action": "BUY"}, now=NOW)
    assert proposal is None
    assert err is not None
    assert "action" in err
