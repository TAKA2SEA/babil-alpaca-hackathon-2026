#!/usr/bin/env python3
"""
Phase 6 - DRY-RUN demonstration only. No order is submitted.

Part 1: builds a real bull call vertical spread from live SPY ATM
contracts (GET-only discovery/quotes, same code paths already exercised
in Phase 3/4), computes its max loss/profit, builds the MLEG order
request, and runs it through submit_and_confirm() in the DEFAULT
DRY_RUN mode (mode="LIVE" is never passed - no submit_order call occurs).

Part 2: runs exit_evaluator.evaluate_exit() over a few synthetic
position scenarios (no real position currently open on the account -
the Phase 4.1 position was never filled and its order was canceled in
Phase 5) to demonstrate TP / SL / DTE-forced-exit / HOLD behavior.
"""
import datetime as dt

import config
import contract_discovery as discovery
import execution_engine as engine
import exit_evaluator as exitmod
import mleg_builder as mb
from alpaca.trading.enums import ContractType


def part1_mleg_dry_run():
    print("=" * 70)
    print("PART 1: MLEG BULL CALL SPREAD - DRY-RUN (SPY, real market data)")
    print(f"Endpoint: {config.PAPER_BASE_URL}")
    print("=" * 70)

    trading_client = discovery.make_trading_client()
    data_client = discovery.make_data_client()
    stock_data_client = discovery.make_stock_data_client()

    today = dt.date.today()
    exp_gte = today + dt.timedelta(days=config.DTE_MIN_DAYS)
    exp_lte = today + dt.timedelta(days=config.DTE_MAX_DAYS)

    contracts = discovery.fetch_active_contracts(
        "SPY",
        trading_client=trading_client,
        contract_type=ContractType.CALL,
        expiration_date_gte=exp_gte.isoformat(),
        expiration_date_lte=exp_lte.isoformat(),
    )
    contracts = discovery.filter_by_type(contracts, "call")
    dte_filtered = discovery.filter_by_dte(contracts, config.DTE_MIN_DAYS, config.DTE_MAX_DAYS, today=today)
    dte_ok_contracts = [c for c, _dte in dte_filtered]

    spot_price = discovery.fetch_underlying_spot_price("SPY", stock_data_client=stock_data_client)
    atm_candidates = discovery.filter_by_atm_proximity(dte_ok_contracts, spot_price, pct=0.05)
    if len(atm_candidates) < 2:
        print(f"ABORT: fewer than 2 ATM call candidates found near spot ${spot_price:.2f}.")
        return

    # Lowest-strike ATM candidate = long leg, next-highest strike = short leg (bull call spread).
    atm_candidates_sorted = sorted(atm_candidates, key=lambda c: float(discovery.field(c, "strike_price")))
    long_contract, short_contract = atm_candidates_sorted[0], atm_candidates_sorted[1]

    print(f"\n[SPOT] SPY = ${spot_price:.2f}")
    print(f"[LONG LEG]  {discovery.field(long_contract, 'symbol')} strike={discovery.field(long_contract, 'strike_price')}")
    print(f"[SHORT LEG] {discovery.field(short_contract, 'symbol')} strike={discovery.field(short_contract, 'strike_price')}")

    legs, summary = mb.build_vertical_call_spread(long_contract, short_contract)
    print(f"\n[SPREAD SUMMARY] {summary}")

    long_quote = discovery.fetch_latest_quote(discovery.field(long_contract, "symbol"), data_client=data_client)
    short_quote = discovery.fetch_latest_quote(discovery.field(short_contract, "symbol"), data_client=data_client)
    long_ask = float(discovery.field(long_quote, "ask_price"))
    short_bid = float(discovery.field(short_quote, "bid_price"))
    net_debit = round(long_ask - short_bid, 2)
    print(f"\n[QUOTES] long_ask={long_ask} short_bid={short_bid} -> net_debit={net_debit}")

    if net_debit <= 0:
        print("ABORT: computed net_debit <= 0 (would be a credit for this leg selection) - not a bull call debit spread as configured.")
        return

    risk = mb.compute_spread_risk(net_premium=net_debit, strike_width=summary["strike_width"], multiplier=summary["multiplier"], is_debit=True)
    print(f"[RISK] max_loss=${risk['max_loss']:.2f} max_profit=${risk['max_profit']:.2f} per spread (qty=1)")

    mleg_request = engine.build_mleg_order_request(legs, qty=1, order_type="limit", limit_price=net_debit)

    print("\n[EXECUTION] calling execution_engine.submit_and_confirm() - mode NOT passed, defaults to DRY_RUN")
    result = engine.submit_and_confirm(None, trading_client, order_request=mleg_request)

    print("\n[STATE MACHINE LOG]")
    for line in result["log"]:
        print(f"  {line}")
    print(f"\n[RESULT] state={result['state']} (no order_id - nothing was submitted)")


def part2_exit_evaluator_demo():
    print("\n" + "=" * 70)
    print("PART 2: EXIT EVALUATOR - synthetic position scenarios")
    print("=" * 70)

    scenarios = [
        {"label": "Take-profit candidate", "entry_price": 5.00, "current_price": 8.10, "dte": 20},
        {"label": "Stop-loss candidate", "entry_price": 5.00, "current_price": 3.20, "dte": 20},
        {"label": "DTE forced-exit (even though profitable)", "entry_price": 5.00, "current_price": 9.00, "dte": 1},
        {"label": "Hold (no condition met)", "entry_price": 5.00, "current_price": 5.30, "dte": 15},
    ]
    for s in scenarios:
        result = exitmod.evaluate_exit(s["entry_price"], s["current_price"], s["dte"])
        print(f"\n[{s['label']}]")
        print(f"  entry=${s['entry_price']:.2f} current=${s['current_price']:.2f} dte={s['dte']}")
        print(f"  -> action={result.action} | reason={result.reason}")


if __name__ == "__main__":
    part1_mleg_dry_run()
    part2_exit_evaluator_demo()
