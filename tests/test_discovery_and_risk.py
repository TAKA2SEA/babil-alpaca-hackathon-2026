"""
Phase 2/3 unit tests. Mock-only - no network call anywhere in this file.

TradingClient/OptionHistoricalDataClient are mocked with
unittest.mock.create_autospec() against the REAL installed SDK classes, so
these tests assert against the actual method surface (submit_order,
cancel_order_by_id, exercise_options_position, etc. really exist on the
mock and can be asserted not-called) rather than a hand-rolled double that
might not match reality.
"""
import datetime as dt
from unittest.mock import create_autospec

import pytest

import contract_discovery as discovery
import dry_run_engine as engine
import risk_evaluator as risk
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.trading.client import TradingClient

TODAY = dt.date(2026, 1, 1)


def make_contract(**overrides):
    base = {
        "symbol": "SPY260201C00580000",
        "type": "call",
        "status": "active",
        "tradable": True,
        "strike_price": "580",
        "expiration_date": "2026-02-01",
        "underlying_symbol": "SPY",
        "multiplier": "100",
        "size": "100",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# contract_discovery: filters
# ---------------------------------------------------------------------------


def test_filter_by_type_keeps_only_matching_type():
    contracts = [make_contract(type="call"), make_contract(type="put"), make_contract(type="call")]
    result = discovery.filter_by_type(contracts, "call")
    assert len(result) == 2
    assert all(c["type"] == "call" for c in result)


def test_filter_by_dte_boundary_inclusive():
    contracts = [
        make_contract(symbol="EXACT_MIN", expiration_date=(TODAY + dt.timedelta(days=7)).isoformat()),
        make_contract(symbol="EXACT_MAX", expiration_date=(TODAY + dt.timedelta(days=45)).isoformat()),
        make_contract(symbol="TOO_SOON", expiration_date=(TODAY + dt.timedelta(days=6)).isoformat()),
        make_contract(symbol="TOO_FAR", expiration_date=(TODAY + dt.timedelta(days=46)).isoformat()),
    ]
    result = discovery.filter_by_dte(contracts, min_days=7, max_days=45, today=TODAY)
    kept_symbols = {c["symbol"] for c, _dte in result}
    assert kept_symbols == {"EXACT_MIN", "EXACT_MAX"}


def test_filter_by_strike_range():
    contracts = [make_contract(strike_price="500"), make_contract(strike_price="580"), make_contract(strike_price="700")]
    result = discovery.filter_by_strike_range(contracts, min_strike=550, max_strike=650)
    assert len(result) == 1
    assert result[0]["strike_price"] == "580"


# ---------------------------------------------------------------------------
# risk_evaluator: G1 Contract Validity
# ---------------------------------------------------------------------------


def test_g1_passes_when_active_and_tradable():
    result = risk.gate_g1_contract_validity(make_contract(status="active", tradable=True))
    assert result.passed is True


def test_g1_rejects_when_not_active():
    result = risk.gate_g1_contract_validity(make_contract(status="inactive", tradable=True))
    assert result.passed is False


def test_g1_rejects_when_not_tradable():
    result = risk.gate_g1_contract_validity(make_contract(status="active", tradable=False))
    assert result.passed is False


# ---------------------------------------------------------------------------
# risk_evaluator: G2 Spread & Liquidity
# ---------------------------------------------------------------------------


def test_g2_passes_within_spread_threshold():
    # bid=10, ask=11 -> mid=10.5, spread=1/10.5=9.5% < 15%
    result = risk.gate_g2_spread_liquidity(bid=10, ask=11, max_spread_pct=0.15)
    assert result.passed is True


def test_g2_rejects_when_spread_too_wide():
    # bid=10, ask=14 -> mid=12, spread=4/12=33.3% > 15%
    result = risk.gate_g2_spread_liquidity(bid=10, ask=14, max_spread_pct=0.15)
    assert result.passed is False


def test_g2_fail_closed_on_invalid_quote():
    assert risk.gate_g2_spread_liquidity(bid=None, ask=11).passed is False
    assert risk.gate_g2_spread_liquidity(bid=0, ask=11).passed is False
    assert risk.gate_g2_spread_liquidity(bid=12, ask=11).passed is False  # ask < bid


# ---------------------------------------------------------------------------
# risk_evaluator: G3 Exposure & Sizing (the ×multiplier math)
# ---------------------------------------------------------------------------


def test_compute_max_qty_boundary_math():
    # ask=5.00, multiplier=100 -> per_contract_risk=500
    # equity=100000, max_loss_pct=0.02 -> budget=2000
    # max_qty = floor(2000 / 500) = 4
    max_qty, per_contract_risk, budget = risk.compute_max_qty(
        ask_price=5.00, contract_multiplier=100, account_equity=100000, max_loss_pct=0.02
    )
    assert per_contract_risk == 500.0
    assert budget == 2000.0
    assert max_qty == 4


def test_compute_max_qty_exact_division_boundary():
    # ask=4.00, multiplier=100 -> per_contract_risk=400; budget=2000 -> exactly 5.0
    max_qty, _, _ = risk.compute_max_qty(ask_price=4.00, contract_multiplier=100, account_equity=100000, max_loss_pct=0.02)
    assert max_qty == 5


def test_compute_max_qty_zero_when_single_contract_exceeds_budget():
    # ask=25.00, multiplier=100 -> per_contract_risk=2500 > budget=2000 -> 0
    max_qty, _, _ = risk.compute_max_qty(ask_price=25.00, contract_multiplier=100, account_equity=100000, max_loss_pct=0.02)
    assert max_qty == 0


def test_g3_converts_string_multiplier_and_computes_qty():
    # Real Paper API evidence: multiplier/size come back as the STRING "100".
    contract = make_contract(multiplier="100")
    result, max_qty, per_contract_risk, budget = risk.gate_g3_exposure_sizing(
        ask_price=5.00, contract=contract, account_equity=100000
    )
    assert result.passed is True
    assert max_qty == 4
    assert per_contract_risk == 500.0


def test_g3_fail_closed_when_multiplier_missing():
    contract = make_contract(multiplier=None, size=None)
    result, max_qty, _, _ = risk.gate_g3_exposure_sizing(ask_price=5.00, contract=contract, account_equity=100000)
    assert result.passed is False
    assert max_qty == 0


def test_g3_fail_closed_when_multiplier_not_numeric():
    contract = make_contract(multiplier="not-a-number", size=None)
    result, max_qty, _, _ = risk.gate_g3_exposure_sizing(ask_price=5.00, contract=contract, account_equity=100000)
    assert result.passed is False
    assert max_qty == 0


def test_g3_fail_closed_when_risk_exceeds_budget():
    contract = make_contract(multiplier="100")
    result, max_qty, _, _ = risk.gate_g3_exposure_sizing(ask_price=25.00, contract=contract, account_equity=100000)
    assert result.passed is False
    assert max_qty == 0


# ---------------------------------------------------------------------------
# risk_evaluator: G4 Options Level
# ---------------------------------------------------------------------------


def test_g4_passes_at_required_level():
    assert risk.gate_g4_options_level(3, required_level=3).passed is True


def test_g4_passes_above_required_level():
    assert risk.gate_g4_options_level(4, required_level=3).passed is True


def test_g4_rejects_below_required_level():
    assert risk.gate_g4_options_level(2, required_level=3).passed is False


def test_g4_converts_string_level():
    assert risk.gate_g4_options_level("3", required_level=3).passed is True


def test_g4_fail_closed_on_invalid_level():
    assert risk.gate_g4_options_level(None, required_level=3).passed is False


# ---------------------------------------------------------------------------
# risk_evaluator: G0 Market Clock
# ---------------------------------------------------------------------------


def make_clock(**overrides):
    base = {"is_open": True, "timestamp": "2026-08-26T14:00:00-04:00", "next_open": None, "next_close": None}
    base.update(overrides)
    return base


def test_g0_passes_when_market_open():
    assert risk.gate_g0_market_clock(make_clock(is_open=True)).passed is True


def test_g0_rejects_when_market_closed():
    clock = make_clock(is_open=False, next_open="2026-08-26T09:30:00-04:00")
    result = risk.gate_g0_market_clock(clock)
    assert result.passed is False
    assert "2026-08-26T09:30:00-04:00" in result.reason


def test_g0_fail_closed_when_is_open_missing():
    result = risk.gate_g0_market_clock({"timestamp": "2026-08-26T14:00:00-04:00"})
    assert result.passed is False


# ---------------------------------------------------------------------------
# risk_evaluator: evaluate_all_gates integration
# ---------------------------------------------------------------------------


def test_evaluate_all_gates_all_pass():
    contract = make_contract()
    all_passed, results, sizing = risk.evaluate_all_gates(
        contract, bid=5.00, ask=5.10, account_equity=100000, account_options_trading_level=3
    )
    assert all_passed is True
    assert len(results) == 4  # G0 skipped by default (clock=None) - existing callers unaffected
    assert sizing["max_qty"] > 0


def test_evaluate_all_gates_includes_g0_when_clock_passed_and_passes_when_open():
    contract = make_contract()
    all_passed, results, _sizing = risk.evaluate_all_gates(
        contract,
        bid=5.00,
        ask=5.10,
        account_equity=100000,
        account_options_trading_level=3,
        clock=make_clock(is_open=True),
    )
    assert all_passed is True
    assert len(results) == 5
    assert results[0].name == "G0_market_clock"
    assert results[0].passed is True


def test_evaluate_all_gates_rejects_when_market_closed():
    contract = make_contract()
    all_passed, results, _sizing = risk.evaluate_all_gates(
        contract,
        bid=5.00,
        ask=5.10,
        account_equity=100000,
        account_options_trading_level=3,
        clock=make_clock(is_open=False, next_open="2026-08-26T09:30:00-04:00"),
    )
    assert all_passed is False
    g0 = next(r for r in results if r.name == "G0_market_clock")
    assert g0.passed is False


def test_evaluate_all_gates_rejects_on_wide_spread():
    contract = make_contract()
    all_passed, results, sizing = risk.evaluate_all_gates(
        contract, bid=5.00, ask=8.00, account_equity=100000, account_options_trading_level=3
    )
    assert all_passed is False
    g2 = next(r for r in results if r.name == "G2_spread_liquidity")
    assert g2.passed is False


def test_evaluate_all_gates_rejects_on_insufficient_options_level():
    contract = make_contract()
    all_passed, results, _sizing = risk.evaluate_all_gates(
        contract, bid=5.00, ask=5.10, account_equity=100000, account_options_trading_level=1
    )
    assert all_passed is False
    g4 = next(r for r in results if r.name == "G4_options_level")
    assert g4.passed is False


# ---------------------------------------------------------------------------
# dry_run_engine: full pipeline with autospec'd SDK clients (no network)
# ---------------------------------------------------------------------------


def _autospec_trading_client(contracts_payload, account_payload, clock_payload=None):
    tc = create_autospec(TradingClient, instance=True)
    tc.get_option_contracts.return_value = {"option_contracts": contracts_payload}
    tc.get_account.return_value = account_payload
    # Explicit, not left to MagicMock's (truthy, non-deterministic) default -
    # run_dry_run_for_underlying always calls get_clock() now (G0).
    tc.get_clock.return_value = clock_payload if clock_payload is not None else make_clock(is_open=True)
    return tc


def _autospec_data_client(quotes_by_symbol):
    dc = create_autospec(OptionHistoricalDataClient, instance=True)
    dc.get_option_latest_quote.return_value = quotes_by_symbol
    return dc


def _autospec_stock_data_client(spot_price):
    """
    Mocks the underlying spot-price lookup added in Phase 4
    (fetch_underlying_spot_price -> filter_by_atm_proximity). Without this,
    run_dry_run_for_underlying() falls back to constructing a REAL
    StockHistoricalDataClient from .env.paper and making a real network
    call - exactly the bug this mock exists to prevent in these
    mock-only tests.
    """
    sdc = create_autospec(StockHistoricalDataClient, instance=True)
    sdc.get_stock_latest_trade.return_value = {"SPY": {"price": spot_price}}
    return sdc


def test_dry_run_pipeline_generates_intent_and_never_calls_mutating_methods():
    contract = make_contract(
        symbol="SPY260201C00580000",
        expiration_date=(dt.date.today() + dt.timedelta(days=30)).isoformat(),
    )
    tc = _autospec_trading_client(
        contracts_payload=[contract],
        account_payload={"equity": "100000", "options_trading_level": 3},
    )
    dc = _autospec_data_client({"SPY260201C00580000": {"bid_price": 5.00, "ask_price": 5.10}})
    sdc = _autospec_stock_data_client(spot_price=580.0)  # matches contract's strike -> within ATM window

    reports = engine.run_dry_run_for_underlying("SPY", trading_client=tc, data_client=dc, stock_data_client=sdc)

    assert len(reports) == 1
    assert "DRY-RUN / NO ORDER SUBMITTED" in reports[0]
    assert "ALL PASS" in reports[0]

    # The autospec mocks only expose real TradingClient/OptionHistoricalDataClient
    # methods - these assertions confirm the pipeline never touched any of them.
    tc.submit_order.assert_not_called()
    tc.cancel_order_by_id.assert_not_called()
    tc.cancel_orders.assert_not_called()
    tc.exercise_options_position.assert_not_called()
    tc.close_position.assert_not_called()
    tc.close_all_positions.assert_not_called()


def test_dry_run_pipeline_rejects_on_wide_spread_and_qty_zero():
    contract = make_contract(
        symbol="SPY260201C00580000",
        expiration_date=(dt.date.today() + dt.timedelta(days=30)).isoformat(),
    )
    tc = _autospec_trading_client(
        contracts_payload=[contract],
        account_payload={"equity": "100000", "options_trading_level": 3},
    )
    # bid=5.00, ask=8.00 -> spread 46% > 15% threshold -> G2 rejects
    dc = _autospec_data_client({"SPY260201C00580000": {"bid_price": 5.00, "ask_price": 8.00}})
    sdc = _autospec_stock_data_client(spot_price=580.0)

    reports = engine.run_dry_run_for_underlying("SPY", trading_client=tc, data_client=dc, stock_data_client=sdc)

    assert len(reports) == 1
    assert "REJECTED" in reports[0]
    assert "Target Qty     : 0" in reports[0]
    tc.submit_order.assert_not_called()


def test_dry_run_pipeline_rejects_when_market_closed_even_if_other_gates_pass():
    contract = make_contract(
        symbol="SPY260201C00580000",
        expiration_date=(dt.date.today() + dt.timedelta(days=30)).isoformat(),
    )
    tc = _autospec_trading_client(
        contracts_payload=[contract],
        account_payload={"equity": "100000", "options_trading_level": 3},
        clock_payload=make_clock(is_open=False, next_open="2026-08-26T09:30:00-04:00"),
    )
    dc = _autospec_data_client({"SPY260201C00580000": {"bid_price": 5.00, "ask_price": 5.10}})
    sdc = _autospec_stock_data_client(spot_price=580.0)

    reports = engine.run_dry_run_for_underlying("SPY", trading_client=tc, data_client=dc, stock_data_client=sdc)

    assert len(reports) == 1
    assert "REJECTED" in reports[0]
    assert "G0_market_clock" in reports[0]
    assert "Target Qty     : 0" in reports[0]
    tc.submit_order.assert_not_called()


def test_dry_run_pipeline_no_contracts_in_dte_window_returns_message_no_calls():
    tc = _autospec_trading_client(
        contracts_payload=[make_contract(expiration_date="2099-01-01")],  # far outside DTE window
        account_payload={"equity": "100000", "options_trading_level": 3},
    )
    dc = _autospec_data_client({})

    reports = engine.run_dry_run_for_underlying("SPY", trading_client=tc, data_client=dc)

    assert len(reports) == 1
    assert "No " in reports[0]
    dc.get_option_latest_quote.assert_not_called()
    tc.submit_order.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
