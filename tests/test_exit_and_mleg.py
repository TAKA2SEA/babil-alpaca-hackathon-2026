"""
Phase 6 unit tests: exit_evaluator.py (pure logic, no imports of the
Alpaca SDK at all) and mleg_builder.py / execution_engine.py's MLEG
support (construction only - create_autospec proves submit_order is
never called even when a pre-built MLEG order_request is passed through
submit_and_confirm in DRY_RUN mode).
"""
from unittest.mock import create_autospec

import pytest

import execution_engine as engine
import exit_evaluator as exitmod
import mleg_builder as mb
from alpaca.trading.client import TradingClient

# ---------------------------------------------------------------------------
# exit_evaluator: evaluate_exit
# ---------------------------------------------------------------------------


def test_exit_holds_when_no_condition_met():
    result = exitmod.evaluate_exit(entry_price=5.00, current_price=5.20, dte=20)
    assert result.action == "HOLD"


def test_exit_take_profit_triggered_above_threshold():
    # +60% > default 50% target
    result = exitmod.evaluate_exit(entry_price=5.00, current_price=8.00, dte=20)
    assert result.action == "CLOSE"
    assert "take profit" in result.reason
    assert result.suggested_limit_price == 8.00


def test_exit_take_profit_boundary_exact():
    # exactly +50%
    result = exitmod.evaluate_exit(entry_price=5.00, current_price=7.50, dte=20, target_profit_pct=0.50)
    assert result.action == "CLOSE"
    assert "take profit" in result.reason


def test_exit_just_below_take_profit_threshold_holds():
    result = exitmod.evaluate_exit(entry_price=5.00, current_price=7.49, dte=20, target_profit_pct=0.50)
    assert result.action == "HOLD"


def test_exit_stop_loss_triggered_below_threshold():
    # -40% < default -30% stop
    result = exitmod.evaluate_exit(entry_price=5.00, current_price=3.00, dte=20)
    assert result.action == "CLOSE"
    assert "stop loss" in result.reason


def test_exit_stop_loss_boundary_exact():
    # exactly -30%
    result = exitmod.evaluate_exit(entry_price=5.00, current_price=3.50, dte=20, stop_loss_pct=0.30)
    assert result.action == "CLOSE"
    assert "stop loss" in result.reason


def test_exit_dte_forced_exit_overrides_profit():
    # deep in profit, but dte=1 forces exit anyway
    result = exitmod.evaluate_exit(entry_price=5.00, current_price=9.00, dte=1)
    assert result.action == "CLOSE"
    assert "DTE expiration risk" in result.reason


def test_exit_dte_forced_exit_overrides_loss():
    result = exitmod.evaluate_exit(entry_price=5.00, current_price=1.00, dte=0)
    assert result.action == "CLOSE"
    assert "DTE expiration risk" in result.reason


def test_exit_dte_priority_over_stop_loss_when_both_true():
    # -50% AND dte=1 both true - DTE reason must win (checked first)
    result = exitmod.evaluate_exit(entry_price=5.00, current_price=2.50, dte=1)
    assert result.action == "CLOSE"
    assert result.reason.startswith("DTE expiration risk")  # DTE is the primary reason, not stop loss


def test_exit_fails_safe_to_hold_on_invalid_entry_price():
    result = exitmod.evaluate_exit(entry_price=0, current_price=5.00, dte=20)
    assert result.action == "HOLD"
    assert "invalid entry_price" in result.reason


def test_exit_intent_equality():
    a = exitmod.ExitIntent("CLOSE", "reason", 5.0)
    b = exitmod.ExitIntent("CLOSE", "reason", 5.0)
    assert a == b


# ---------------------------------------------------------------------------
# mleg_builder: build_vertical_call_spread / build_vertical_put_spread
# ---------------------------------------------------------------------------


def make_option(symbol, strike, expiration="2026-09-18", multiplier="100"):
    return {"symbol": symbol, "strike_price": strike, "expiration_date": expiration, "multiplier": multiplier, "size": multiplier}


def test_build_vertical_call_spread_valid():
    long_leg = make_option("SPY260918C00700000", "700")
    short_leg = make_option("SPY260918C00710000", "710")
    legs, summary = mb.build_vertical_call_spread(long_leg, short_leg)

    assert len(legs) == 2
    assert legs[0].symbol == "SPY260918C00700000"
    assert legs[0].side.value == "buy"
    assert legs[1].symbol == "SPY260918C00710000"
    assert legs[1].side.value == "sell"
    assert summary["strike_width"] == 10.0
    assert summary["multiplier"] == 100


def test_build_vertical_call_spread_rejects_wrong_strike_order():
    long_leg = make_option("SPY260918C00710000", "710")
    short_leg = make_option("SPY260918C00700000", "700")  # short strike below long - invalid for bull call
    with pytest.raises(mb.SpreadBuilderError):
        mb.build_vertical_call_spread(long_leg, short_leg)


def test_build_vertical_call_spread_rejects_multiplier_mismatch():
    long_leg = make_option("SPY260918C00700000", "700", multiplier="100")
    short_leg = make_option("SPY260918C00710000", "710", multiplier="10")
    with pytest.raises(mb.SpreadBuilderError):
        mb.build_vertical_call_spread(long_leg, short_leg)


def test_build_vertical_call_spread_rejects_mismatched_expiration():
    long_leg = make_option("SPY260918C00700000", "700", expiration="2026-09-18")
    short_leg = make_option("SPY261016C00710000", "710", expiration="2026-10-16")
    with pytest.raises(mb.SpreadBuilderError):
        mb.build_vertical_call_spread(long_leg, short_leg)


def test_build_vertical_call_spread_rejects_same_symbol():
    leg = make_option("SPY260918C00700000", "700")
    with pytest.raises(mb.SpreadBuilderError):
        mb.build_vertical_call_spread(leg, dict(leg))


def test_build_vertical_put_spread_valid():
    long_leg = make_option("SPY260918P00700000", "700")
    short_leg = make_option("SPY260918P00690000", "690")
    legs, summary = mb.build_vertical_put_spread(long_leg, short_leg)

    assert legs[0].side.value == "buy"
    assert legs[1].side.value == "sell"
    assert summary["strike_width"] == 10.0


def test_build_vertical_put_spread_rejects_wrong_strike_order():
    long_leg = make_option("SPY260918P00690000", "690")
    short_leg = make_option("SPY260918P00700000", "700")  # short strike above long - invalid for bear put
    with pytest.raises(mb.SpreadBuilderError):
        mb.build_vertical_put_spread(long_leg, short_leg)


# ---------------------------------------------------------------------------
# mleg_builder: compute_spread_risk boundary math
# ---------------------------------------------------------------------------


def test_compute_spread_risk_debit_boundary_math():
    # net debit $4.50, width $10, multiplier 100 -> max_loss=$450, max_profit=$550
    result = mb.compute_spread_risk(net_premium=4.50, strike_width=10.0, multiplier=100, is_debit=True)
    assert result["max_loss"] == 450.0
    assert result["max_profit"] == 550.0
    assert result["width_value"] == 1000.0


def test_compute_spread_risk_credit_boundary_math():
    # net credit $3.00, width $10, multiplier 100 -> max_profit=$300, max_loss=$700
    result = mb.compute_spread_risk(net_premium=3.00, strike_width=10.0, multiplier=100, is_debit=False)
    assert result["max_profit"] == 300.0
    assert result["max_loss"] == 700.0


def test_compute_spread_risk_rejects_negative_premium():
    with pytest.raises(mb.SpreadBuilderError):
        mb.compute_spread_risk(net_premium=-1.0, strike_width=10.0, multiplier=100, is_debit=True)


# ---------------------------------------------------------------------------
# execution_engine: build_mleg_order_request
# ---------------------------------------------------------------------------


def _two_legs():
    long_leg = make_option("SPY260918C00700000", "700")
    short_leg = make_option("SPY260918C00710000", "710")
    legs, _summary = mb.build_vertical_call_spread(long_leg, short_leg)
    return legs


def test_build_mleg_order_request_valid_limit():
    legs = _two_legs()
    req = engine.build_mleg_order_request(legs, qty=2, order_type="limit", limit_price=4.55)
    assert req.order_class.value == "mleg"
    assert req.symbol is None
    assert req.side is None
    assert req.qty == 2
    assert len(req.legs) == 2
    assert req.limit_price == 4.55


def test_build_mleg_order_request_valid_market():
    legs = _two_legs()
    req = engine.build_mleg_order_request(legs, qty=1, order_type="market")
    assert req.order_class.value == "mleg"
    assert req.qty == 1


def test_build_mleg_order_request_rejects_single_leg():
    with pytest.raises(engine.ExecutionError):
        engine.build_mleg_order_request(_two_legs()[:1], qty=1, order_type="market")


def test_build_mleg_order_request_rejects_too_many_legs():
    legs = _two_legs()
    with pytest.raises(engine.ExecutionError):
        engine.build_mleg_order_request(legs * 3, qty=1, order_type="market")  # 6 legs


def test_build_mleg_order_request_rejects_zero_qty():
    with pytest.raises(engine.ExecutionError):
        engine.build_mleg_order_request(_two_legs(), qty=0, order_type="market")


def test_build_mleg_order_request_rejects_limit_without_price():
    with pytest.raises(engine.ExecutionError):
        engine.build_mleg_order_request(_two_legs(), qty=1, order_type="limit", limit_price=None)


# ---------------------------------------------------------------------------
# execution_engine: submit_and_confirm accepting a pre-built MLEG order_request
# ---------------------------------------------------------------------------


def _autospec_trading_client():
    return create_autospec(TradingClient, instance=True)


def test_submit_and_confirm_mleg_dry_run_never_submits_order():
    tc = _autospec_trading_client()
    mleg_request = engine.build_mleg_order_request(_two_legs(), qty=2, order_type="limit", limit_price=4.55)

    result = engine.submit_and_confirm(None, tc, order_request=mleg_request)  # mode defaults to DRY_RUN

    assert result["state"] == "DRY_RUN_ONLY"
    tc.submit_order.assert_not_called()


def test_submit_and_confirm_rejects_both_intent_and_order_request():
    tc = _autospec_trading_client()
    mleg_request = engine.build_mleg_order_request(_two_legs(), qty=1, order_type="market")
    with pytest.raises(engine.ExecutionError):
        engine.submit_and_confirm({"option_symbol": "X", "qty": 1, "limit_price": 1.0}, tc, order_request=mleg_request)


def test_submit_and_confirm_mleg_live_mode_normal_fill_flow():
    tc = _autospec_trading_client()
    tc.submit_order.return_value = {"id": "mleg-order-1"}
    poll_responses = iter([{"status": "pending_new"}, {"status": "filled", "filled_qty": "2", "filled_avg_price": "4.52"}])
    tc.get_order_by_id.side_effect = lambda order_id, filter=None: next(poll_responses)

    mleg_request = engine.build_mleg_order_request(_two_legs(), qty=2, order_type="limit", limit_price=4.55)
    result = engine.submit_and_confirm(None, tc, order_request=mleg_request, mode="LIVE", sleep_fn=lambda s: None)

    assert result["state"] == "FILLED"
    assert result["filled_qty"] == "2"
    tc.submit_order.assert_called_once_with(mleg_request)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
