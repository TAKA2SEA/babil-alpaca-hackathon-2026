"""
Phase 4/5 - Paper single-leg execution engine: submit + fill-confirmation
polling, and cancel + cancellation-confirmation polling.

This is the ONE file in this workspace allowed to call
TradingClient.submit_order() / cancel_order_by_id() - test_static_security.py
bans those calls everywhere else in the workspace and additionally
verifies (in this file specifically) that each call site is textually
gated behind an `if mode != "LIVE"` early return.

mode="DRY_RUN" is the hard default on submit_and_confirm(). In that mode
this file builds the exact order request object that would be submitted
and logs it, but never calls submit_order or get_order_by_id - nothing is
sent to the network. mode="LIVE" is the only path that submits a real
order; per the Phase 4 instructions, mode="LIVE" is not invoked against
the real Paper API in this turn - only mock unit tests
(tests/test_execution_engine.py) and DRY-RUN are exercised this session.

State machine (mirrors the DRY-RUN report's own tri-state fail-closed
pattern, independently implemented here - not imported from any other
system): [SUBMITTED] -> [POLLING] -> [FILLED] / [FAILED] / [FILL_UNKNOWN].
A poll timeout returns FILL_UNKNOWN, never FILLED - the caller must never
assume a fill happened just because polling stopped.
"""
import time

from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

POLL_INTERVAL_SECONDS = 0.5
POLL_MAX_ATTEMPTS = 20  # 20 * 0.5s = 10s ceiling, mirrors the Moomoo-side BUY poll design intent

CANCEL_POLL_INTERVAL_SECONDS = 0.5
CANCEL_POLL_MAX_ATTEMPTS = 10  # 10 * 0.5s = 5s ceiling

TERMINAL_FAILURE_STATUSES = {"rejected", "canceled", "expired"}


class ExecutionError(Exception):
    pass


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_str_value(value):
    if value is None:
        return value
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def build_order_request(intent, order_type="limit"):
    """
    Builds (but does not submit) the SDK order-request object for a
    single-leg BUY from a DRY-RUN intent dict (see dry_run_engine.py).
    order_class is always OrderClass.SIMPLE. For multi-leg spreads, use
    build_mleg_order_request() instead (BRACKET/OCO/OTO remain out of
    scope - documented by the installed SDK as equity-only, not valid for
    options - see ARCHITECTURE.md).
    """
    symbol = intent.get("option_symbol") if isinstance(intent, dict) else _field(intent, "option_symbol")
    qty = intent.get("qty") if isinstance(intent, dict) else _field(intent, "qty")
    if not qty or qty < 1:
        raise ExecutionError(f"refusing to build an order request for qty={qty!r}")

    common_kwargs = dict(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.SIMPLE,
    )
    if order_type == "limit":
        limit_price = intent.get("limit_price") if isinstance(intent, dict) else _field(intent, "limit_price")
        if limit_price is None:
            raise ExecutionError("limit order requested but intent has no limit_price")
        return LimitOrderRequest(limit_price=round(float(limit_price), 2), **common_kwargs)
    if order_type == "market":
        return MarketOrderRequest(**common_kwargs)
    raise ExecutionError(f"unsupported order_type: {order_type!r}")


def build_mleg_order_request(legs, qty, order_type="limit", limit_price=None, time_in_force=None):
    """
    Builds (but does not submit) a multi-leg (MLEG) order request from a
    list of OptionLegRequest legs (see mleg_builder.py) and a spread
    quantity.

    order_class is always OrderClass.MLEG. symbol/side are deliberately
    NOT set at the top level: the installed SDK's own OrderRequest
    validator (confirmed by reading its source, not assumed) requires
    symbol/side to be present for every OTHER order class but does not
    require or use them when order_class == MLEG - only qty and 2-4
    unique-symbol legs are required there.
    """
    if not legs or len(legs) < 2:
        raise ExecutionError(f"MLEG order requires at least 2 legs, got {len(legs) if legs else 0}")
    if len(legs) > 4:
        raise ExecutionError(f"MLEG order allows at most 4 legs, got {len(legs)}")
    if not qty or qty < 1:
        raise ExecutionError(f"refusing to build an MLEG order request for qty={qty!r}")

    common_kwargs = dict(
        qty=qty,
        time_in_force=time_in_force or TimeInForce.DAY,
        order_class=OrderClass.MLEG,
        legs=legs,
    )
    if order_type == "limit":
        if limit_price is None:
            raise ExecutionError("limit MLEG order requested but no limit_price given")
        return LimitOrderRequest(limit_price=round(float(limit_price), 2), **common_kwargs)
    if order_type == "market":
        return MarketOrderRequest(**common_kwargs)
    raise ExecutionError(f"unsupported order_type: {order_type!r}")


def submit_and_confirm(
    intent,
    trading_client,
    order_type="limit",
    mode="DRY_RUN",
    poll_interval=POLL_INTERVAL_SECONDS,
    poll_max_attempts=POLL_MAX_ATTEMPTS,
    sleep_fn=time.sleep,
    order_request=None,
):
    """
    State machine: [SUBMITTED] -> [POLLING] -> [FILLED] / [FAILED] / [FILL_UNKNOWN].

    Pass either `intent` (the existing single-leg dict path, built via
    build_order_request) or a pre-built `order_request` (e.g. from
    build_mleg_order_request()) via the order_request= keyword - not
    both. This lets MLEG orders reuse the exact same submit/poll state
    machine and the exact same mode="LIVE" gate as single-leg orders,
    without adding a second guarded submit_order call site.

    Returns a result dict: {"state": ..., "log": [...], ...}. `state` is
    one of "DRY_RUN_ONLY", "FILLED", "FAILED", "FILL_UNKNOWN".
    """
    log = []

    if order_request is not None:
        if intent is not None:
            raise ExecutionError("pass either intent or order_request, not both")
    else:
        order_request = build_order_request(intent, order_type=order_type)

    symbol = _field(order_request, "symbol") or "MLEG"
    qty = _field(order_request, "qty")
    order_class = _as_str_value(_field(order_request, "order_class"))
    log.append(f"[BUILT] {order_type} order request ({order_class}) for {symbol} qty={qty}")

    if mode != "LIVE":
        log.append("[DRY-RUN] submit_order was NOT called (mode != 'LIVE').")
        return {"state": "DRY_RUN_ONLY", "log": log, "order_request": order_request}

    # --- The only submit_order call site in this workspace. ---
    order = trading_client.submit_order(order_request)
    order_id = _field(order, "id")
    log.append(f"[SUBMITTED] order_id={order_id}")

    for attempt in range(1, poll_max_attempts + 1):
        log.append(f"[POLLING] attempt {attempt}/{poll_max_attempts}")
        current = trading_client.get_order_by_id(order_id)
        status = _as_str_value(_field(current, "status", "")).lower()

        if status == "filled":
            filled_qty = _field(current, "filled_qty")
            filled_avg_price = _field(current, "filled_avg_price")
            log.append(f"[FILLED] filled_qty={filled_qty} filled_avg_price={filled_avg_price}")
            return {
                "state": "FILLED",
                "log": log,
                "order_id": order_id,
                "filled_qty": filled_qty,
                "filled_avg_price": filled_avg_price,
            }

        if status in TERMINAL_FAILURE_STATUSES:
            log.append(f"[FAILED] status={status!r} - fail-closed, no further action taken")
            return {"state": "FAILED", "log": log, "order_id": order_id, "reason": status}

        if attempt < poll_max_attempts:
            sleep_fn(poll_interval)

    log.append(
        f"[FAILED] timeout after {poll_max_attempts} attempts ({poll_max_attempts * poll_interval:.1f}s) - "
        "fail-closed, fill status unknown, not assumed filled"
    )
    return {"state": "FILL_UNKNOWN", "log": log, "order_id": order_id, "reason": "poll_timeout"}


def cancel_and_confirm(
    order_id,
    trading_client,
    mode="DRY_RUN",
    poll_interval=CANCEL_POLL_INTERVAL_SECONDS,
    poll_max_attempts=CANCEL_POLL_MAX_ATTEMPTS,
    sleep_fn=time.sleep,
):
    """
    State machine: [CANCEL_SUBMITTED] -> [POLLING] -> [CANCELED] / [CANCEL_FAILED] / [CANCEL_UNKNOWN].

    mode="DRY_RUN" (default): logs the cancel request that would be made
    but never calls cancel_order_by_id or get_order_by_id.

    mode="LIVE": the only path in this file that submits a real cancel
    request via the trading client. Then polls get_order_by_id() to
    confirm status == 'canceled'. If the order turns out to have filled
    before the cancel took effect (a real race condition, not a
    hypothetical one), this is reported explicitly as CANCEL_FAILED with
    reason="filled_before_cancel" - never silently treated as canceled. A
    poll timeout returns CANCEL_UNKNOWN, never CANCELED - the caller must
    never assume a cancel succeeded just because polling stopped.
    """
    log = []
    log.append(f"[BUILT] cancel request for order_id={order_id}")

    if mode != "LIVE":
        log.append("[DRY-RUN] cancel_order_by_id was NOT called (mode != 'LIVE').")
        return {"state": "DRY_RUN_ONLY", "log": log, "order_id": order_id}

    # --- The only cancel_order_by_id call site in this workspace. ---
    trading_client.cancel_order_by_id(order_id)
    log.append(f"[CANCEL_SUBMITTED] order_id={order_id}")

    for attempt in range(1, poll_max_attempts + 1):
        log.append(f"[POLLING] attempt {attempt}/{poll_max_attempts}")
        current = trading_client.get_order_by_id(order_id)
        status = _as_str_value(_field(current, "status", "")).lower()

        if status == "canceled":
            log.append(f"[CANCELED] order_id={order_id} confirmed canceled")
            return {"state": "CANCELED", "log": log, "order_id": order_id}

        if status == "filled":
            filled_qty = _field(current, "filled_qty")
            log.append(
                f"[CANCEL_FAILED] order filled before cancel took effect (filled_qty={filled_qty}) - "
                "fail-closed, reporting as-is, not treated as canceled"
            )
            return {
                "state": "CANCEL_FAILED",
                "log": log,
                "order_id": order_id,
                "reason": "filled_before_cancel",
                "filled_qty": filled_qty,
            }

        if attempt < poll_max_attempts:
            sleep_fn(poll_interval)

    log.append(
        f"[CANCEL_FAILED] timeout after {poll_max_attempts} attempts ({poll_max_attempts * poll_interval:.1f}s) - "
        "fail-closed, cancel status unknown, not assumed canceled"
    )
    return {"state": "CANCEL_UNKNOWN", "log": log, "order_id": order_id, "reason": "poll_timeout"}
