#!/usr/bin/env python3
"""
Phase 4.1 - single-contract real Paper order probe.

One-shot standalone script: Contract Discovery -> ATM filter -> Risk Gates
(qty forced to 1) -> Intent -> execution_engine.submit_and_confirm(mode="LIVE")
-> position confirmation via get_all_positions().

This script does not add a new submit_order call site anywhere - it calls
execution_engine.submit_and_confirm(), which is the one place in the
workspace that call exists (see test_static_security.py tests 9-10).

Runs exactly once and stops - no loop, no retry beyond the single
fill-confirmation poll already built into submit_and_confirm() for this
one order. Qty is hard-forced to 1 regardless of what G3 sizing would
otherwise allow (G3's calculated max_qty is always >= 1 when it passes,
so forcing down to 1 is always the more conservative choice, never a
gate violation). Only the Paper endpoint (config.PAPER_BASE_URL) is ever
used - trading_client is always constructed via
contract_discovery.make_trading_client(), which is always paper=True.
"""
import datetime as dt

import config
import contract_discovery as discovery
import dry_run_engine as dryrun
import execution_engine as engine
import risk_evaluator as risk
from alpaca.trading.enums import ContractType

FORCED_QTY = 1


def main():
    trading_client = discovery.make_trading_client()
    data_client = discovery.make_data_client()
    stock_data_client = discovery.make_stock_data_client()

    account = trading_client.get_account()
    account_equity = float(discovery.field(account, "equity"))
    account_options_trading_level = int(discovery.field(account, "options_trading_level"))

    print("=" * 70)
    print("PHASE 4.1 - SINGLE CONTRACT PAPER ORDER PROBE")
    print(f"Endpoint: {config.PAPER_BASE_URL}")
    print(f"Account equity: ${account_equity:,.2f} | options_trading_level: {account_options_trading_level}")
    print("=" * 70)

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
    if not dte_ok_contracts:
        print("\nABORT: no SPY call contracts found in DTE window. No order submitted.")
        return

    spot_price = discovery.fetch_underlying_spot_price("SPY", stock_data_client=stock_data_client)
    atm_candidates = discovery.filter_by_atm_proximity(dte_ok_contracts, spot_price, pct=0.05)
    if not atm_candidates:
        print(f"\nABORT: no SPY call contracts within ATM window of spot ${spot_price:.2f}. No order submitted.")
        return

    contract = atm_candidates[0]  # single closest-to-spot candidate
    symbol = discovery.field(contract, "symbol")
    strike = discovery.field(contract, "strike_price")
    expiration = discovery.field(contract, "expiration_date")
    print(f"\n[SELECTED] {symbol} | strike={strike} | expiration={expiration} | spot=${spot_price:.2f}")

    quote = discovery.fetch_latest_quote(symbol, data_client=data_client)
    bid = discovery.field(quote, "bid_price")
    ask = discovery.field(quote, "ask_price")
    print(f"[QUOTE] bid={bid} ask={ask}")

    all_passed, gate_results, sizing = risk.evaluate_all_gates(
        contract, bid, ask, account_equity, account_options_trading_level
    )
    print("\n[GATE RESULTS] (qty will be forced to 1 regardless of G3's calculated max_qty)")
    for g in gate_results:
        print(f"  [{'PASS' if g.passed else 'REJECTED'}] {g.name}: {g.reason}")

    if not all_passed:
        print("\nABORT: not all gates passed. No order will be submitted.")
        return

    limit_price = round(float(ask) * 1.01, 2)  # small slippage cap above ask, maximizes fill probability
    intent = dryrun.build_order_intent(contract, "buy_to_open", FORCED_QTY, limit_price, gate_results)
    intent["qty"] = FORCED_QTY  # explicit, redundant override for clarity/audit trail
    print(f"\n[INTENT] {intent['intent_id']} | qty(forced)={intent['qty']} | limit_price={limit_price}")

    print("\n[EXECUTION] calling execution_engine.submit_and_confirm(mode='LIVE') - ONE order, no retry loop")
    t0 = dt.datetime.now(dt.timezone.utc)
    result = engine.submit_and_confirm(intent, trading_client, order_type="limit", mode="LIVE")
    elapsed = (dt.datetime.now(dt.timezone.utc) - t0).total_seconds()

    print("\n[STATE MACHINE LOG]")
    for line in result["log"]:
        print(f"  {line}")

    print(f"\n[RESULT] state={result['state']} | elapsed={elapsed:.2f}s")
    if result["state"] == "FILLED":
        print(f"  order_id={result['order_id']}")
        print(f"  filled_qty={result['filled_qty']}")
        print(f"  filled_avg_price={result['filled_avg_price']}")
    else:
        print(f"  order_id={result.get('order_id')}")
        print(f"  reason={result.get('reason')}")

    print("\n[POSITION CONFIRMATION] get_all_positions()")
    positions = trading_client.get_all_positions()
    match = None
    for p in positions:
        if discovery.field(p, "symbol") == symbol:
            match = p
            break
    if match:
        print(
            f"  FOUND: symbol={discovery.field(match, 'symbol')} qty={discovery.field(match, 'qty')} "
            f"avg_entry_price={discovery.field(match, 'avg_entry_price')} "
            f"market_value={discovery.field(match, 'market_value')}"
        )
    else:
        print(f"  NOT FOUND: no position for {symbol} in get_all_positions() (total positions: {len(positions)})")

    print("\n" + "=" * 70)
    print("PROBE COMPLETE - stopping (no further orders will be submitted).")
    print("=" * 70)


if __name__ == "__main__":
    main()
