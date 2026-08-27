"""
Phase 7 unit tests: gate_g5_spread_economics boundary cases (including a
direct reproduction of the real Phase 6 max_profit=-$341 finding) and
mock-only integration tests for babil_alpaca_orchestrator.py.
"""
import datetime as dt
from unittest.mock import create_autospec

import pytest

import babil_alpaca_orchestrator as orch
import mleg_builder as mb
import risk_evaluator as risk
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.trading.client import TradingClient

# ---------------------------------------------------------------------------
# gate_g5_spread_economics
# ---------------------------------------------------------------------------


def test_g5_rejects_negative_max_profit():
    # Direct reproduction of the real Phase 6 finding: $1-wide spread,
    # net debit $4.41 x 100 = $441 > width_value $100 -> max_profit=-$341.
    spread_risk = mb.compute_spread_risk(net_premium=4.41, strike_width=1.0, multiplier=100, is_debit=True)
    assert spread_risk["max_profit"] == -341.0

    result = risk.gate_g5_spread_economics(spread_risk)
    assert result.passed is False
    assert "max_profit=$-341.00" in result.reason


def test_g5_rejects_zero_max_profit():
    spread_risk = {"max_profit": 0.0, "max_loss": 100.0, "width_value": 100.0, "is_debit": True}
    assert risk.gate_g5_spread_economics(spread_risk).passed is False


def test_g5_passes_with_good_risk_reward():
    # net debit $4.50, width $10, multiplier 100 -> max_loss=$450, max_profit=$550, ratio=1.22
    spread_risk = mb.compute_spread_risk(net_premium=4.50, strike_width=10.0, multiplier=100, is_debit=True)
    result = risk.gate_g5_spread_economics(spread_risk, min_risk_reward_ratio=0.2)
    assert result.passed is True


def test_g5_rejects_poor_risk_reward_ratio():
    # net debit $9.00, width $10, multiplier 100 -> max_loss=$900, max_profit=$100, ratio=0.11 < 0.2
    spread_risk = mb.compute_spread_risk(net_premium=9.00, strike_width=10.0, multiplier=100, is_debit=True)
    result = risk.gate_g5_spread_economics(spread_risk, min_risk_reward_ratio=0.2)
    assert result.passed is False
    assert "risk/reward" in result.reason


def test_g5_rejects_implausible_negative_max_loss():
    spread_risk = {"max_profit": 100.0, "max_loss": -1.0, "width_value": 99.0, "is_debit": False}
    result = risk.gate_g5_spread_economics(spread_risk)
    assert result.passed is False
    assert "implausible" in result.reason


def test_g5_fail_closed_on_missing_fields():
    result = risk.gate_g5_spread_economics({"max_profit": 100.0})  # missing max_loss/width_value
    assert result.passed is False


def test_evaluate_all_gates_skips_g5_by_default():
    contract = {"symbol": "X", "status": "active", "tradable": True, "strike_price": "580", "multiplier": "100", "size": "100"}
    all_passed, results, _sizing = risk.evaluate_all_gates(contract, bid=5.00, ask=5.10, account_equity=100000, account_options_trading_level=3)
    assert all(r.name != "G5_spread_economics" for r in results)
    assert all_passed is True


def test_evaluate_all_gates_includes_and_enforces_g5_when_spread_risk_passed():
    contract = {"symbol": "X", "status": "active", "tradable": True, "strike_price": "580", "multiplier": "100", "size": "100"}
    bad_spread_risk = mb.compute_spread_risk(net_premium=4.41, strike_width=1.0, multiplier=100, is_debit=True)
    all_passed, results, _sizing = risk.evaluate_all_gates(
        contract, bid=5.00, ask=5.10, account_equity=100000, account_options_trading_level=3, spread_risk=bad_spread_risk
    )
    assert all_passed is False
    g5 = next(r for r in results if r.name == "G5_spread_economics")
    assert g5.passed is False


# ---------------------------------------------------------------------------
# mleg_builder: select_vertical_spread_pair spot-proximity tie-break
# ---------------------------------------------------------------------------


def test_select_pair_prefers_spot_proximity_tiebreak_over_deep_itm_call():
    # Two pairs both have width=5: a deep-ITM pair far from spot, and an
    # ATM pair right at spot. Width-diff is identical for both, so the
    # spot-proximity tie-break must decide.
    spot = 580.0
    exp = "2026-09-18"
    contracts = [
        make_contract("DEEP1", "500", exp),
        make_contract("DEEP2", "505", exp),
        make_contract("ATM1", "578", exp),
        make_contract("ATM2", "583", exp),
    ]
    long_c, short_c = mb.select_vertical_spread_pair(contracts, spot, target_width=5.0, option_type="call")
    assert long_c["symbol"] == "ATM1"
    assert short_c["symbol"] == "ATM2"


def test_select_pair_excludes_deep_itm_long_leg_for_calls():
    spot = 580.0
    exp = "2026-09-18"
    # Only a deep ITM pair exists: long strike 500 << spot*0.98=568.4
    contracts = [make_contract("DEEP1", "500", exp), make_contract("DEEP2", "505", exp)]
    with pytest.raises(mb.SpreadBuilderError):
        mb.select_vertical_spread_pair(contracts, spot, target_width=5.0, option_type="call")


def test_select_pair_excludes_deep_itm_long_leg_for_puts_symmetric():
    spot = 580.0
    exp = "2026-09-18"
    # Bear put: long leg = higher strike. 655/660 are both >> spot*1.02=591.6 -> deep ITM put, excluded.
    contracts = [make_contract("DEEPPUT1", "655", exp), make_contract("DEEPPUT2", "660", exp)]
    with pytest.raises(mb.SpreadBuilderError):
        mb.select_vertical_spread_pair(contracts, spot, target_width=5.0, option_type="put")


def test_select_pair_reproduces_and_fixes_the_real_phase7_dry_run_case():
    # SPY spot=$765.85 - the exact real Phase 7 candidate window that
    # previously produced 728/733 (deep ITM, negative max_profit). With
    # the tie-break, a near-spot pair must now be preferred if one is
    # present in the same window.
    spot = 765.85
    exp = "2026-09-02"
    contracts = [
        make_contract("SPY_DEEP1", "728", exp),
        make_contract("SPY_DEEP2", "733", exp),
        make_contract("SPY_ATM1", "763", exp),
        make_contract("SPY_ATM2", "768", exp),
    ]
    long_c, short_c = mb.select_vertical_spread_pair(contracts, spot, target_width=5.0, option_type="call")
    assert long_c["symbol"] == "SPY_ATM1"
    assert short_c["symbol"] == "SPY_ATM2"


# ---------------------------------------------------------------------------
# babil_alpaca_orchestrator: mock-only integration
# ---------------------------------------------------------------------------


def make_clock(is_open=True, next_open=None):
    return {"is_open": is_open, "timestamp": "2026-08-26T14:00:00-04:00", "next_open": next_open, "next_close": None}


def make_contract(symbol, strike, expiration, multiplier="100"):
    return {
        "symbol": symbol,
        "type": "call",
        "status": "active",
        "tradable": True,
        "strike_price": strike,
        "expiration_date": expiration,
        "underlying_symbol": "SPY",
        "multiplier": multiplier,
        "size": multiplier,
    }


def _autospec_trading_client(contracts_payload, account_payload, clock_payload, positions_payload=None):
    tc = create_autospec(TradingClient, instance=True)
    tc.get_option_contracts.return_value = {"option_contracts": contracts_payload}
    tc.get_account.return_value = account_payload
    tc.get_clock.return_value = clock_payload
    tc.get_all_positions.return_value = positions_payload or []
    return tc


def _autospec_data_client(quotes_by_symbol):
    dc = create_autospec(OptionHistoricalDataClient, instance=True)
    dc.get_option_latest_quote.side_effect = lambda req: {req.symbol_or_symbols: quotes_by_symbol[req.symbol_or_symbols]}
    return dc


def _autospec_stock_data_client(spot_price, symbol="SPY"):
    sdc = create_autospec(StockHistoricalDataClient, instance=True)
    sdc.get_stock_latest_trade.return_value = {symbol: {"price": spot_price}}
    return sdc


def test_orchestrator_run_for_underlying_with_good_spread():
    exp = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    contracts = [
        make_contract("SPY_C1", "578", exp),  # near spot 580, part of a sane $5-wide pair with SPY_C2
        make_contract("SPY_C2", "583", exp),
    ]
    tc = _autospec_trading_client(
        contracts_payload=contracts,
        account_payload={"equity": "100000", "options_trading_level": 3},
        clock_payload=make_clock(is_open=True),
    )
    quotes = {
        "SPY_C1": {"bid_price": 5.00, "ask_price": 5.10},
        "SPY_C2": {"bid_price": 2.00, "ask_price": 2.10},
    }
    dc = _autospec_data_client(quotes)
    sdc = _autospec_stock_data_client(spot_price=580.0)

    report = orch.run_for_underlying("SPY", tc, dc, sdc, account_equity=100000, account_options_trading_level=3, clock=tc.get_clock())

    assert report["g0_market_clock"]["passed"] is True
    assert report["single_leg"] is not None
    assert report["vertical_spread"] is not None
    assert "error" not in report["vertical_spread"]
    # net_debit = 5.10 - 2.00 = 3.10, width=5 -> max_loss=310, max_profit=190, ratio=0.61 > 0.2 -> G5 passes
    assert report["vertical_spread"]["all_passed"] is True
    tc.submit_order.assert_not_called()


def test_orchestrator_rejects_spread_via_g5_when_economically_dominated():
    exp = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    contracts = [
        make_contract("SPY_C1", "579", exp),
        make_contract("SPY_C2", "580", exp),  # $1-wide, mirrors the real Phase 6 finding
    ]
    tc = _autospec_trading_client(
        contracts_payload=contracts,
        account_payload={"equity": "100000", "options_trading_level": 3},
        clock_payload=make_clock(is_open=True),
    )
    quotes = {
        "SPY_C1": {"bid_price": 40.00, "ask_price": 40.29},
        "SPY_C2": {"bid_price": 35.88, "ask_price": 36.20},
    }
    dc = _autospec_data_client(quotes)
    sdc = _autospec_stock_data_client(spot_price=580.0)

    report = orch.run_for_underlying("SPY", tc, dc, sdc, account_equity=100000, account_options_trading_level=3, clock=tc.get_clock())

    vs = report["vertical_spread"]
    assert vs["max_profit"] < 0
    assert vs["all_passed"] is False
    g5 = next(g for g in vs["gates"] if g["name"] == "G5_spread_economics")
    assert g5["passed"] is False
    tc.submit_order.assert_not_called()


def test_orchestrator_rejects_via_g0_when_market_closed():
    exp = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    contracts = [make_contract("SPY_C1", "578", exp), make_contract("SPY_C2", "583", exp)]
    tc = _autospec_trading_client(
        contracts_payload=contracts,
        account_payload={"equity": "100000", "options_trading_level": 3},
        clock_payload=make_clock(is_open=False, next_open="2026-08-26T09:30:00-04:00"),
    )
    quotes = {"SPY_C1": {"bid_price": 5.00, "ask_price": 5.10}, "SPY_C2": {"bid_price": 2.00, "ask_price": 2.10}}
    dc = _autospec_data_client(quotes)
    sdc = _autospec_stock_data_client(spot_price=580.0)

    report = orch.run_for_underlying("SPY", tc, dc, sdc, account_equity=100000, account_options_trading_level=3, clock=tc.get_clock())

    assert report["g0_market_clock"]["passed"] is False
    assert report["single_leg"]["all_passed"] is False
    assert report["vertical_spread"]["all_passed"] is False


def test_orchestrator_exit_checks_with_mock_option_position():
    tc = create_autospec(TradingClient, instance=True)
    tc.get_all_positions.return_value = [
        {"symbol": "SPY260918C00580000", "asset_class": "us_option", "avg_entry_price": "5.00", "current_price": "8.10"}
    ]

    exit_reports = orch.run_exit_checks(tc)

    assert len(exit_reports) == 1
    assert exit_reports[0]["action"] == "CLOSE"
    assert "take profit" in exit_reports[0]["reason"]


def test_orchestrator_exit_checks_ignores_non_option_positions():
    tc = create_autospec(TradingClient, instance=True)
    tc.get_all_positions.return_value = [{"symbol": "AAPL", "asset_class": "us_equity", "avg_entry_price": "150.00", "current_price": "155.00"}]

    exit_reports = orch.run_exit_checks(tc)

    assert exit_reports == []


def test_orchestrator_terminal_table_and_json_never_submits_order():
    exp = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    contracts = [make_contract("SPY_C1", "578", exp), make_contract("SPY_C2", "583", exp)]
    tc = _autospec_trading_client(
        contracts_payload=contracts,
        account_payload={"equity": "100000", "options_trading_level": 3},
        clock_payload=make_clock(is_open=True),
    )
    quotes = {"SPY_C1": {"bid_price": 5.00, "ask_price": 5.10}, "SPY_C2": {"bid_price": 2.00, "ask_price": 2.10}}
    dc = _autospec_data_client(quotes)
    sdc = _autospec_stock_data_client(spot_price=580.0)

    reports = [orch.run_for_underlying("SPY", tc, dc, sdc, 100000, 3, tc.get_clock())]
    table = orch.format_terminal_table(reports, exit_reports=[])

    assert "DRY-RUN ONLY" in table
    assert "SPY" in table
    tc.submit_order.assert_not_called()
    tc.cancel_order_by_id.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
