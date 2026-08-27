#!/usr/bin/env python3
"""
Phase 7 - Integrated DRY-RUN orchestrator.

One-stop runner per underlying: G0 market clock -> ATM single-leg call
discovery + vertical spread discovery -> G0-G5 gate evaluation -> exit
evaluation of any existing account option positions -> a formatted
telemetry report (terminal table + JSON).

DRY-RUN only. This file never calls submit_order, cancel_order_by_id, or
any other order-mutating method - it only reads (contract discovery,
quotes, account, clock, positions) and evaluates gates. If a caller
wanted to actually act on what this orchestrator reports, they would
take its output and pass it to execution_engine.submit_and_confirm(...,
mode="LIVE") themselves, outside of this file.
"""
import datetime as dt
import json

from alpaca.trading.enums import ContractType

import config
import contract_discovery as discovery
import exit_evaluator as exitmod
import mleg_builder as mb
import risk_evaluator as risk

DEFAULT_UNDERLYINGS = ("SPY", "QQQ")
DEFAULT_SPREAD_TARGET_WIDTH = 5.0


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _fetch_call_candidates(underlying_symbol, trading_client, today):
    exp_gte = today + dt.timedelta(days=config.DTE_MIN_DAYS)
    exp_lte = today + dt.timedelta(days=config.DTE_MAX_DAYS)
    contracts = discovery.fetch_active_contracts(
        underlying_symbol,
        trading_client=trading_client,
        contract_type=ContractType.CALL,
        expiration_date_gte=exp_gte.isoformat(),
        expiration_date_lte=exp_lte.isoformat(),
    )
    contracts = discovery.filter_by_type(contracts, "call")
    dte_filtered = discovery.filter_by_dte(contracts, config.DTE_MIN_DAYS, config.DTE_MAX_DAYS, today=today)
    return [c for c, _dte in dte_filtered]


def _evaluate_single_leg(atm_calls, data_client, account_equity, account_options_trading_level, clock):
    if not atm_calls:
        return None
    contract = atm_calls[0]
    symbol = _field(contract, "symbol")
    quote = discovery.fetch_latest_quote(symbol, data_client=data_client)
    bid = _field(quote, "bid_price")
    ask = _field(quote, "ask_price")

    all_passed, gate_results, sizing = risk.evaluate_all_gates(
        contract, bid, ask, account_equity, account_options_trading_level, clock=clock
    )
    return {
        "symbol": symbol,
        "strike": _field(contract, "strike_price"),
        "bid": bid,
        "ask": ask,
        "all_passed": all_passed,
        "gates": [{"name": g.name, "passed": g.passed, "reason": g.reason} for g in gate_results],
        "max_qty": sizing["max_qty"],
    }


def _evaluate_vertical_spread(
    atm_calls, spot_price, data_client, account_equity, account_options_trading_level, clock, target_width
):
    if len(atm_calls) < 2:
        return None
    try:
        long_c, short_c = mb.select_vertical_spread_pair(atm_calls, spot_price, target_width=target_width, option_type="call")
        legs, summary = mb.build_vertical_call_spread(long_c, short_c)

        long_quote = discovery.fetch_latest_quote(_field(long_c, "symbol"), data_client=data_client)
        short_quote = discovery.fetch_latest_quote(_field(short_c, "symbol"), data_client=data_client)
        long_bid = float(_field(long_quote, "bid_price"))
        long_ask = float(_field(long_quote, "ask_price"))
        short_bid = float(_field(short_quote, "bid_price"))
        net_debit = round(long_ask - short_bid, 2)

        spread_risk = mb.compute_spread_risk(
            net_premium=max(net_debit, 0.0), strike_width=summary["strike_width"], multiplier=summary["multiplier"], is_debit=True
        )

        # G1/G2/G3 evaluated against the long leg as a representative
        # single contract (a known simplification - the short leg is not
        # independently gated here); G0/G4/G5 are the gates that actually
        # matter for spread-level economics and are evaluated properly.
        all_passed, gate_results, _sizing = risk.evaluate_all_gates(
            long_c,
            long_bid,
            long_ask,
            account_equity,
            account_options_trading_level,
            clock=clock,
            spread_risk=spread_risk,
        )
        return {
            "strategy": summary["strategy"],
            "long_symbol": summary["long_symbol"],
            "short_symbol": summary["short_symbol"],
            "strike_width": summary["strike_width"],
            "net_debit": net_debit,
            "max_loss": spread_risk["max_loss"],
            "max_profit": spread_risk["max_profit"],
            "all_passed": all_passed,
            "gates": [{"name": g.name, "passed": g.passed, "reason": g.reason} for g in gate_results],
        }
    except mb.SpreadBuilderError as e:
        return {"error": str(e)}


def run_for_underlying(
    underlying_symbol,
    trading_client,
    data_client,
    stock_data_client,
    account_equity,
    account_options_trading_level,
    clock,
    atm_pct=0.05,
    spread_target_width=DEFAULT_SPREAD_TARGET_WIDTH,
):
    """Returns a telemetry dict for one underlying. GET-only - never submits an order."""
    report = {"underlying": underlying_symbol, "timestamp": dt.datetime.now(dt.timezone.utc).isoformat()}

    g0 = risk.gate_g0_market_clock(clock)
    report["g0_market_clock"] = {"passed": g0.passed, "reason": g0.reason}

    today = dt.date.today()
    dte_ok_contracts = _fetch_call_candidates(underlying_symbol, trading_client, today)

    spot_price = discovery.fetch_underlying_spot_price(underlying_symbol, stock_data_client=stock_data_client)
    report["spot_price"] = spot_price

    atm_calls = discovery.filter_by_atm_proximity(dte_ok_contracts, spot_price, pct=atm_pct)

    report["single_leg"] = _evaluate_single_leg(atm_calls, data_client, account_equity, account_options_trading_level, clock)
    report["vertical_spread"] = _evaluate_vertical_spread(
        atm_calls, spot_price, data_client, account_equity, account_options_trading_level, clock, spread_target_width
    )

    return report


def run_exit_checks(trading_client):
    """
    Runs exit_evaluator over every open option position. GET-only
    (get_all_positions). Known limitation: Position has no direct DTE
    field, and this workspace has never held a filled option position to
    validate a real OCC-symbol-based DTE parser against - dte is passed
    as a large placeholder (effectively disabling the DTE-forced-exit
    branch) rather than guessing an expiration-parsing implementation.
    """
    positions = trading_client.get_all_positions()
    exit_reports = []
    for p in positions:
        asset_class = str(_field(p, "asset_class", ""))
        if "option" not in asset_class.lower():
            continue
        symbol = _field(p, "symbol")
        entry_price = _field(p, "avg_entry_price")
        current_price = _field(p, "current_price")
        try:
            result = exitmod.evaluate_exit(entry_price, current_price, dte=9999)
        except (TypeError, ValueError) as e:
            result = exitmod.ExitIntent("HOLD", f"could not evaluate: {e}")
        exit_reports.append(
            {"symbol": symbol, "entry_price": entry_price, "current_price": current_price, "action": result.action, "reason": result.reason}
        )
    return exit_reports


def format_terminal_table(reports, exit_reports):
    lines = []
    lines.append("=" * 78)
    lines.append("BABIL ALPACA HACKATHON ORCHESTRATOR - DRY-RUN TELEMETRY REPORT")
    lines.append("=" * 78)

    for r in reports:
        spot = r.get("spot_price")
        spot_str = f"${spot:.2f}" if spot is not None else "N/A"
        lines.append(f"\n[{r['underlying']}] spot={spot_str}")
        lines.append(f"  G0 Market Clock: {'PASS' if r['g0_market_clock']['passed'] else 'REJECTED'} - {r['g0_market_clock']['reason']}")

        sl = r.get("single_leg")
        if sl:
            lines.append(
                f"  Single-leg ATM Call: {sl['symbol']} strike={sl['strike']} -> "
                f"{'ALL PASS' if sl['all_passed'] else 'REJECTED'} (max_qty={sl['max_qty']})"
            )
            for g in sl["gates"]:
                lines.append(f"    [{'PASS' if g['passed'] else 'REJECTED'}] {g['name']}: {g['reason']}")
        else:
            lines.append("  Single-leg ATM Call: no candidates found")

        vs = r.get("vertical_spread")
        if vs and "error" not in vs:
            lines.append(
                f"  Vertical Spread ({vs['strategy']}): {vs['long_symbol']}/{vs['short_symbol']} "
                f"width=${vs['strike_width']} net_debit=${vs['net_debit']} "
                f"max_loss=${vs['max_loss']:.2f} max_profit=${vs['max_profit']:.2f} -> "
                f"{'ALL PASS' if vs['all_passed'] else 'REJECTED'}"
            )
            for g in vs["gates"]:
                lines.append(f"    [{'PASS' if g['passed'] else 'REJECTED'}] {g['name']}: {g['reason']}")
        elif vs:
            lines.append(f"  Vertical Spread: ERROR - {vs['error']}")
        else:
            lines.append("  Vertical Spread: fewer than 2 ATM candidates, skipped")

    lines.append("\n" + "-" * 78)
    lines.append("EXIT EVALUATION (current account option positions)")
    if exit_reports:
        for e in exit_reports:
            lines.append(f"  {e['symbol']}: {e['action']} - {e['reason']}")
    else:
        lines.append("  No open option positions on this account.")

    lines.append("\n" + "=" * 78)
    lines.append("MODE: DRY-RUN ONLY - no order was submitted by this orchestrator run.")
    lines.append("=" * 78)
    return "\n".join(lines)


def main(underlyings=DEFAULT_UNDERLYINGS):
    trading_client = discovery.make_trading_client()
    data_client = discovery.make_data_client()
    stock_data_client = discovery.make_stock_data_client()

    account = trading_client.get_account()
    account_equity = float(_field(account, "equity"))
    account_options_trading_level = int(_field(account, "options_trading_level"))
    clock = trading_client.get_clock()

    reports = [
        run_for_underlying(symbol, trading_client, data_client, stock_data_client, account_equity, account_options_trading_level, clock)
        for symbol in underlyings
    ]
    exit_reports = run_exit_checks(trading_client)

    print(format_terminal_table(reports, exit_reports))

    json_report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "account_equity": account_equity,
        "account_options_trading_level": account_options_trading_level,
        "market_open": _field(clock, "is_open"),
        "underlyings": reports,
        "exit_checks": exit_reports,
        "mode": "DRY_RUN",
    }
    print("\nJSON TELEMETRY:")
    print(json.dumps(json_report, indent=2, default=str))
    return json_report


if __name__ == "__main__":
    import sys

    syms = tuple(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_UNDERLYINGS
    main(syms)
