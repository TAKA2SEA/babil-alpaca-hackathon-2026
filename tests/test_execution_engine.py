"""
Phase 4 unit tests for execution_engine.py. Mock-only - no network call
anywhere in this file. TradingClient is mocked with
unittest.mock.create_autospec() against the REAL installed SDK class, so
submit_order/get_order_by_id calls (or the absence of them) are asserted
against the actual method surface, not a hand-rolled double.

Per the Phase 4 instructions, mode="LIVE" is exercised here ONLY against
autospec'd mocks - this file never touches the real Paper API, and no test
here constructs a real TradingClient or reads .env.paper.
"""
from unittest.mock import create_autospec

import pytest

import execution_engine as engine
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide

SAMPLE_INTENT = {
    "option_symbol": "SPY260902C00650000",
    "qty": 2,
    "limit_price": 5.10,
}


def _autospec_trading_client():
    return create_autospec(TradingClient, instance=True)


# ---------------------------------------------------------------------------
# build_order_request
# ---------------------------------------------------------------------------


def test_build_order_request_limit():
    req = engine.build_order_request(SAMPLE_INTENT, order_type="limit")
    assert req.symbol == "SPY260902C00650000"
    assert req.qty == 2
    assert req.side == OrderSide.BUY
    assert req.order_class == OrderClass.SIMPLE
    assert req.limit_price == 5.10


def test_build_order_request_market():
    req = engine.build_order_request(SAMPLE_INTENT, order_type="market")
    assert req.symbol == "SPY260902C00650000"
    assert req.qty == 2


def test_build_order_request_fail_closed_on_zero_qty():
    intent = dict(SAMPLE_INTENT, qty=0)
    with pytest.raises(engine.ExecutionError):
        engine.build_order_request(intent)


def test_build_order_request_fail_closed_on_missing_limit_price():
    intent = {"option_symbol": "SPY260902C00650000", "qty": 1, "limit_price": None}
    with pytest.raises(engine.ExecutionError):
        engine.build_order_request(intent, order_type="limit")


# ---------------------------------------------------------------------------
# submit_and_confirm - DRY_RUN mode (the hard default)
# ---------------------------------------------------------------------------


def test_dry_run_is_the_default_mode_and_never_submits_order():
    tc = _autospec_trading_client()
    result = engine.submit_and_confirm(SAMPLE_INTENT, tc)  # mode not passed - must default to DRY_RUN

    assert result["state"] == "DRY_RUN_ONLY"
    tc.submit_order.assert_not_called()
    tc.get_order_by_id.assert_not_called()


def test_explicit_dry_run_mode_never_submits_order():
    tc = _autospec_trading_client()
    result = engine.submit_and_confirm(SAMPLE_INTENT, tc, mode="DRY_RUN")

    assert result["state"] == "DRY_RUN_ONLY"
    assert "order_request" in result
    tc.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# submit_and_confirm - LIVE mode against autospec'd mocks only
# ---------------------------------------------------------------------------


def test_live_mode_normal_fill_flow():
    tc = _autospec_trading_client()
    tc.submit_order.return_value = {"id": "order-123"}

    poll_responses = iter(
        [
            {"status": "pending_new"},
            {"status": "filled", "filled_qty": "2", "filled_avg_price": "5.05"},
        ]
    )
    tc.get_order_by_id.side_effect = lambda order_id, filter=None: next(poll_responses)

    result = engine.submit_and_confirm(SAMPLE_INTENT, tc, mode="LIVE", sleep_fn=lambda s: None)

    assert result["state"] == "FILLED"
    assert result["filled_qty"] == "2"
    assert result["filled_avg_price"] == "5.05"
    tc.submit_order.assert_called_once()
    assert tc.get_order_by_id.call_count == 2


def test_live_mode_rejected_fails_closed():
    tc = _autospec_trading_client()
    tc.submit_order.return_value = {"id": "order-456"}
    tc.get_order_by_id.return_value = {"status": "rejected"}

    result = engine.submit_and_confirm(SAMPLE_INTENT, tc, mode="LIVE", sleep_fn=lambda s: None)

    assert result["state"] == "FAILED"
    assert result["reason"] == "rejected"
    tc.submit_order.assert_called_once()
    tc.get_order_by_id.assert_called_once()


def test_live_mode_timeout_fails_closed_to_fill_unknown_not_filled():
    tc = _autospec_trading_client()
    tc.submit_order.return_value = {"id": "order-789"}
    tc.get_order_by_id.return_value = {"status": "pending_new"}  # never fills

    result = engine.submit_and_confirm(
        SAMPLE_INTENT, tc, mode="LIVE", poll_max_attempts=3, sleep_fn=lambda s: None
    )

    assert result["state"] == "FILL_UNKNOWN"
    assert result["reason"] == "poll_timeout"
    assert result["state"] != "FILLED"  # must never assume a fill on timeout
    assert tc.get_order_by_id.call_count == 3


def test_live_mode_polls_at_correct_interval():
    tc = _autospec_trading_client()
    tc.submit_order.return_value = {"id": "order-abc"}
    poll_responses = iter([{"status": "pending_new"}, {"status": "filled", "filled_qty": "1", "filled_avg_price": "5.00"}])
    tc.get_order_by_id.side_effect = lambda order_id, filter=None: next(poll_responses)

    sleep_calls = []
    engine.submit_and_confirm(
        SAMPLE_INTENT, tc, mode="LIVE", sleep_fn=lambda s: sleep_calls.append(s)
    )

    assert sleep_calls == [engine.POLL_INTERVAL_SECONDS]  # slept once, between the two polls


# ---------------------------------------------------------------------------
# cancel_and_confirm - DRY_RUN mode (the hard default)
# ---------------------------------------------------------------------------


def test_cancel_dry_run_is_the_default_mode_and_never_cancels():
    tc = _autospec_trading_client()
    result = engine.cancel_and_confirm("order-123", tc)  # mode not passed - must default to DRY_RUN

    assert result["state"] == "DRY_RUN_ONLY"
    tc.cancel_order_by_id.assert_not_called()
    tc.get_order_by_id.assert_not_called()


# ---------------------------------------------------------------------------
# cancel_and_confirm - LIVE mode against autospec'd mocks only
# ---------------------------------------------------------------------------


def test_cancel_live_mode_normal_cancel_flow():
    tc = _autospec_trading_client()
    tc.cancel_order_by_id.return_value = None

    poll_responses = iter([{"status": "pending_cancel"}, {"status": "canceled"}])
    tc.get_order_by_id.side_effect = lambda order_id, filter=None: next(poll_responses)

    result = engine.cancel_and_confirm("order-123", tc, mode="LIVE", sleep_fn=lambda s: None)

    assert result["state"] == "CANCELED"
    tc.cancel_order_by_id.assert_called_once_with("order-123")
    assert tc.get_order_by_id.call_count == 2


def test_cancel_live_mode_timeout_fails_closed_to_cancel_unknown():
    tc = _autospec_trading_client()
    tc.cancel_order_by_id.return_value = None
    tc.get_order_by_id.return_value = {"status": "pending_cancel"}  # never confirms canceled

    result = engine.cancel_and_confirm("order-123", tc, mode="LIVE", poll_max_attempts=3, sleep_fn=lambda s: None)

    assert result["state"] == "CANCEL_UNKNOWN"
    assert result["reason"] == "poll_timeout"
    assert result["state"] != "CANCELED"  # must never assume a cancel succeeded on timeout
    assert tc.get_order_by_id.call_count == 3


def test_cancel_live_mode_filled_before_cancel_reported_not_hidden():
    """Real race condition: the order fills before the cancel takes effect.
    Must be reported explicitly, never silently treated as canceled."""
    tc = _autospec_trading_client()
    tc.cancel_order_by_id.return_value = None
    tc.get_order_by_id.return_value = {"status": "filled", "filled_qty": "1"}

    result = engine.cancel_and_confirm("order-123", tc, mode="LIVE", sleep_fn=lambda s: None)

    assert result["state"] == "CANCEL_FAILED"
    assert result["reason"] == "filled_before_cancel"
    assert result["filled_qty"] == "1"


# ---------------------------------------------------------------------------
# Live endpoint / client-construction isolation
# ---------------------------------------------------------------------------


def test_execution_engine_never_constructs_its_own_trading_client():
    """
    execution_engine.py must only ever receive a TradingClient via
    dependency injection (paper=True enforcement lives centrally in
    contract_discovery.make_trading_client()) - it must never construct
    one itself, which would be a second, unaudited place paper=False
    could accidentally be introduced.
    """
    source = open(engine.__file__, encoding="utf-8").read()
    assert "TradingClient(" not in source
    assert "paper=False" not in source
    assert "paper=True" not in source  # this file should never even need to say it


def test_no_credential_reads_in_this_module():
    """execution_engine.py never reads credentials - it only receives an
    already-authenticated trading_client via dependency injection."""
    source = open(engine.__file__, encoding="utf-8").read()
    assert "ALPACA_PAPER_KEY_ID" not in source
    assert "ALPACA_PAPER_SECRET_KEY" not in source
    assert ".env.paper" not in source


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
