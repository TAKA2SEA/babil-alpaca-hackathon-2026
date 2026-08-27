"""
Phase 3/4 - DRY-RUN engine.

Orchestrates: discovery -> ATM/DTE filter -> quote -> G1-G4 risk gate
evaluation -> order intent generation -> formatted terminal output.
submit_order (or any other order-mutating method) is never called
anywhere in this file.

build_order_intent() produces a lightweight, independently-implemented
DRY-RUN intent structure - conceptually inspired by the separate BABIL
system's Signed Intent pattern (intent_id, immutable snapshot, gate
evidence) but with no cryptographic signing and no dependency on that
system's code. This structure is only ever printed/inspected - it is
never sent to submit_order.
"""
import datetime as dt
import uuid

from alpaca.trading.enums import ContractType

import config
import contract_discovery as discovery
import risk_evaluator as risk

DEFAULT_MAX_CONTRACTS_PER_RUN = 5


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_str_value(value):
    """
    Real alpaca-py enum members (ContractType.CALL, ...) stringify via
    str() as "ContractType.CALL", not their value "call" - confirmed
    against the installed SDK. Prefer .value when present, so the intent
    dict stores a plain string (also keeps it JSON-serializable).
    """
    if value is None:
        return value
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def build_order_intent(contract, side, qty, limit_price, gate_results):
    return {
        "intent_id": str(uuid.uuid4()),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "underlying": _field(contract, "underlying_symbol"),
        "option_symbol": _field(contract, "symbol"),
        "type": _as_str_value(_field(contract, "type")),
        "strike": str(_field(contract, "strike_price")),
        "expiration": str(_field(contract, "expiration_date")),
        "side": side,
        "order_type": "limit",
        "limit_price": limit_price,
        "qty": qty,
        "gate_results": [{"name": g.name, "passed": g.passed, "reason": g.reason} for g in gate_results],
        "simulation_status": "DRY-RUN / NO ORDER SUBMITTED",
    }


def format_report(contract, bid, ask, intent, all_passed, sizing):
    spread_pct = None
    try:
        b, a = float(bid), float(ask)
        if b + a > 0:
            spread_pct = (a - b) / ((a + b) / 2.0)
    except (TypeError, ValueError):
        pass

    multiplier_raw = _field(contract, "multiplier", None) or _field(contract, "size", None)
    try:
        multiplier = int(multiplier_raw)
    except (TypeError, ValueError):
        multiplier = None

    qty = intent["qty"]
    total_premium = None
    if multiplier is not None and ask is not None:
        try:
            total_premium = float(ask) * qty * multiplier
        except (TypeError, ValueError):
            total_premium = None

    lines = []
    lines.append("=" * 70)
    lines.append(f"Underlying     : {intent['underlying']}")
    lines.append(f"Option Symbol  : {intent['option_symbol']}")
    lines.append(f"Type           : {str(intent['type']).upper()}")
    lines.append(f"Strike         : {intent['strike']}")
    lines.append(f"Expiration     : {intent['expiration']}")
    lines.append("-" * 70)
    quote_line = f"Market Quote   : Bid={bid} / Ask={ask} / Spread="
    quote_line += f"{spread_pct:.1%}" if spread_pct is not None else "N/A"
    lines.append(quote_line)
    lines.append(f"Target Qty     : {qty}")
    lines.append(f"Total Premium  : ${total_premium:,.2f}" if total_premium is not None else "Total Premium  : N/A")
    lines.append(f"Max Risk ($)   : ${sizing['per_contract_risk'] * qty:,.2f}")
    lines.append("-" * 70)
    lines.append("Gate Results:")
    for g in intent["gate_results"]:
        status = "PASS" if g["passed"] else "REJECTED"
        lines.append(f"  [{status}] {g['name']}: {g['reason']}")
    lines.append(f"Overall        : {'ALL PASS' if all_passed else 'REJECTED'}")
    lines.append("-" * 70)
    lines.append(f"Intent ID      : {intent['intent_id']}")
    lines.append(f"Simulation Status: {intent['simulation_status']}")
    lines.append("=" * 70)
    return "\n".join(lines)


def run_dry_run_for_underlying(
    underlying_symbol,
    option_type="call",
    trading_client=None,
    data_client=None,
    stock_data_client=None,
    account_equity=None,
    account_options_trading_level=None,
    max_contracts=DEFAULT_MAX_CONTRACTS_PER_RUN,
    atm_pct=0.05,
):
    """
    Full Phase 2+3+4 discovery/DRY-RUN pipeline for one underlying. GET-only
    throughout - contract discovery, spot price, and quote fetch are all
    read-only SDK calls. submit_order is never called anywhere in this file.

    Candidates are selected by proximity to the underlying spot price
    (filter_by_atm_proximity, default +/-5%) rather than by nearest
    strike/expiration alone - Phase 3 without this filter always landed on
    the cheapest available strike, which for SPY/AAPL was deep ITM and
    always failed G3 sizing. This does not change what a real trader would
    consider tradeable; it changes which candidates this DRY-RUN bothers
    to fetch quotes and evaluate gates for.
    """
    trading_client = trading_client or discovery.make_trading_client()
    data_client = data_client or discovery.make_data_client()
    stock_data_client = stock_data_client or discovery.make_stock_data_client()

    if account_equity is None or account_options_trading_level is None:
        account = trading_client.get_account()
        if account_equity is None:
            account_equity = float(_field(account, "equity"))
        if account_options_trading_level is None:
            account_options_trading_level = int(_field(account, "options_trading_level"))

    # G0: fetched here (not left to a poll timeout to discover) so a
    # closed-market run rejects explicitly at the gate stage - see the
    # Phase 4.1 probe, where the real cause of a poll-timeout was only
    # found by a manual follow-up get_clock() call.
    clock = trading_client.get_clock()

    today = dt.date.today()
    exp_gte = today + dt.timedelta(days=config.DTE_MIN_DAYS)
    exp_lte = today + dt.timedelta(days=config.DTE_MAX_DAYS)
    contracts = discovery.fetch_active_contracts(
        underlying_symbol,
        trading_client=trading_client,
        contract_type=ContractType(option_type.lower()),
        expiration_date_gte=exp_gte.isoformat(),
        expiration_date_lte=exp_lte.isoformat(),
    )
    contracts = discovery.filter_by_type(contracts, option_type)
    dte_filtered = discovery.filter_by_dte(contracts, config.DTE_MIN_DAYS, config.DTE_MAX_DAYS, today=today)
    dte_ok_contracts = [c for c, _dte in dte_filtered]

    if not dte_ok_contracts:
        return [
            f"No {option_type} contracts found for {underlying_symbol} within DTE "
            f"[{config.DTE_MIN_DAYS}, {config.DTE_MAX_DAYS}]."
        ]

    spot_price = discovery.fetch_underlying_spot_price(underlying_symbol, stock_data_client=stock_data_client)
    atm_candidates = discovery.filter_by_atm_proximity(dte_ok_contracts, spot_price, pct=atm_pct)[:max_contracts]

    if not atm_candidates:
        return [
            f"No {option_type} contracts found for {underlying_symbol} within +/-{atm_pct:.0%} "
            f"of spot price ${spot_price:.2f}."
        ]

    reports = []
    for contract in atm_candidates:
        symbol = _field(contract, "symbol")
        quote = discovery.fetch_latest_quote(symbol, data_client=data_client)
        bid = _field(quote, "bid_price")
        ask = _field(quote, "ask_price")

        all_passed, gate_results, sizing = risk.evaluate_all_gates(
            contract, bid, ask, account_equity, account_options_trading_level, clock=clock
        )
        qty = sizing["max_qty"] if all_passed else 0
        intent = build_order_intent(contract, "buy_to_open", qty, ask, gate_results)
        reports.append(format_report(contract, bid, ask, intent, all_passed, sizing))

    return reports


if __name__ == "__main__":
    import sys

    symbol = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    for report in run_dry_run_for_underlying(symbol):
        print(report)
        print()
