"""
Phase 6 - Multi-leg (MLEG) vertical spread builder.

Pure construction logic - builds OptionLegRequest lists and pre-trade
max-loss/max-profit numbers for a 2-leg vertical spread. No network call,
no order submission anywhere in this file. Actual MLEG order submission
(if ever exercised) goes through
execution_engine.build_mleg_order_request() + submit_and_confirm(), never
through this file - this file never imports TradingClient.

Only OrderClass.MLEG is used for options here, per the confirmed SDK fact
(see ARCHITECTURE.md / risk_evaluator design notes): BRACKET/OCO/OTO are
documented by the installed SDK as equity-only, not valid for options.
"""
from alpaca.trading.enums import OrderSide, PositionIntent
from alpaca.trading.requests import OptionLegRequest


class SpreadBuilderError(Exception):
    pass


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _contract_multiplier(contract):
    raw = _field(contract, "multiplier", None)
    if raw is None:
        raw = _field(contract, "size", None)
    if raw is None:
        raise SpreadBuilderError("no multiplier/size field on contract")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise SpreadBuilderError(f"multiplier/size not integer-convertible: {raw!r}")


def _validate_pair(long_contract, short_contract, strategy_name):
    long_symbol = _field(long_contract, "symbol")
    short_symbol = _field(short_contract, "symbol")
    if not long_symbol or not short_symbol:
        raise SpreadBuilderError(f"{strategy_name}: both legs must have a symbol")
    if long_symbol == short_symbol:
        raise SpreadBuilderError(f"{strategy_name}: long and short legs must have different contract symbols")

    long_exp = str(_field(long_contract, "expiration_date"))
    short_exp = str(_field(short_contract, "expiration_date"))
    if long_exp != short_exp:
        raise SpreadBuilderError(f"{strategy_name}: vertical spread requires matching expirations: {long_exp} != {short_exp}")

    long_mult = _contract_multiplier(long_contract)
    short_mult = _contract_multiplier(short_contract)
    if long_mult != short_mult:
        raise SpreadBuilderError(f"{strategy_name}: leg multiplier mismatch: {long_mult} != {short_mult}")

    return long_symbol, short_symbol, long_mult


def build_vertical_call_spread(long_call_contract, short_call_contract, ratio_qty=1):
    """
    Bull Call Spread: BUY the lower-strike call, SELL the higher-strike
    call (same underlying/expiration, matching multiplier). Returns
    (legs: list[OptionLegRequest], summary: dict). Never submits
    anything - legs are plain SDK request objects, unsubmitted.
    """
    long_strike = float(_field(long_call_contract, "strike_price"))
    short_strike = float(_field(short_call_contract, "strike_price"))
    if short_strike <= long_strike:
        raise SpreadBuilderError(
            f"bull_call_spread requires short strike ({short_strike}) > long strike ({long_strike})"
        )

    long_symbol, short_symbol, multiplier = _validate_pair(long_call_contract, short_call_contract, "bull_call_spread")

    legs = [
        OptionLegRequest(symbol=long_symbol, ratio_qty=ratio_qty, side=OrderSide.BUY, position_intent=PositionIntent.BUY_TO_OPEN),
        OptionLegRequest(symbol=short_symbol, ratio_qty=ratio_qty, side=OrderSide.SELL, position_intent=PositionIntent.SELL_TO_OPEN),
    ]

    strike_width = short_strike - long_strike
    summary = {
        "strategy": "bull_call_spread",
        "long_symbol": long_symbol,
        "short_symbol": short_symbol,
        "strike_width": strike_width,
        "multiplier": multiplier,
        "max_theoretical_width_value": strike_width * multiplier,
    }
    return legs, summary


def build_vertical_put_spread(long_put_contract, short_put_contract, ratio_qty=1):
    """
    Bear Put Spread: BUY the higher-strike put, SELL the lower-strike put.
    """
    long_strike = float(_field(long_put_contract, "strike_price"))
    short_strike = float(_field(short_put_contract, "strike_price"))
    if short_strike >= long_strike:
        raise SpreadBuilderError(
            f"bear_put_spread requires short strike ({short_strike}) < long strike ({long_strike})"
        )

    long_symbol, short_symbol, multiplier = _validate_pair(long_put_contract, short_put_contract, "bear_put_spread")

    legs = [
        OptionLegRequest(symbol=long_symbol, ratio_qty=ratio_qty, side=OrderSide.BUY, position_intent=PositionIntent.BUY_TO_OPEN),
        OptionLegRequest(symbol=short_symbol, ratio_qty=ratio_qty, side=OrderSide.SELL, position_intent=PositionIntent.SELL_TO_OPEN),
    ]

    strike_width = long_strike - short_strike
    summary = {
        "strategy": "bear_put_spread",
        "long_symbol": long_symbol,
        "short_symbol": short_symbol,
        "strike_width": strike_width,
        "multiplier": multiplier,
        "max_theoretical_width_value": strike_width * multiplier,
    }
    return legs, summary


def compute_spread_risk(net_premium, strike_width, multiplier, is_debit):
    """
    Precomputes max loss / max profit for a 2-leg vertical spread from a
    net premium (per spread - i.e. abs(long_leg_price - short_leg_price))
    and the strike width. multiplier must come from the contract's own
    field (int-converted), never hardcoded.

    Debit spread (net premium paid, e.g. a bull call bought for a debit):
      max_loss   = net_premium * multiplier
      max_profit = (strike_width - net_premium) * multiplier

    Credit spread (net premium received):
      max_profit = net_premium * multiplier
      max_loss   = (strike_width - net_premium) * multiplier
    """
    net = float(net_premium)
    multiplier = int(multiplier)
    width_value = float(strike_width) * multiplier

    if net < 0:
        raise SpreadBuilderError(f"net_premium must be non-negative (pass the absolute value), got {net}")

    if is_debit:
        max_loss = net * multiplier
        max_profit = width_value - max_loss
    else:
        max_profit = net * multiplier
        max_loss = width_value - max_profit

    return {
        "max_loss": max_loss,
        "max_profit": max_profit,
        "width_value": width_value,
        "is_debit": is_debit,
    }


def select_vertical_spread_pair(contracts, spot_price, target_width=5.0, option_type="call", max_itm_pct=0.02):
    """
    Selects a (long_contract, short_contract) pair for a vertical spread
    from a same-expiration contract list.

    Scoring (lexicographic - primary criterion decides first, secondary
    only breaks ties):
      1. abs(strike_width - target_width) minimized - prefer a spread
         close to the requested width (e.g. $5).
      2. abs(long_strike - spot_price) minimized (tie-break) - among
         pairs with equally-good width, prefer the one whose LONG leg
         sits closest to spot, i.e. a genuine ATM/near-OTM pair rather
         than a width-matched pair anywhere in the chain.

    Hard filter (not a scoring criterion - failing pairs are excluded
    entirely, never merely deprioritized): the long leg must not be
    "deep ITM" by more than max_itm_pct. For calls, long_strike must be
    >= spot_price * (1 - max_itm_pct); for puts (symmetric: a put's long
    leg is its HIGHER strike, so deep ITM means long_strike far ABOVE
    spot), long_strike must be <= spot_price * (1 + max_itm_pct).

    This exists because two real DRY-RUN runs (Phase 6, and Phase 7
    before this fix) both picked pairs with negative max_profit - width
    matching alone was not sufficient; both had to actually be run
    against live market data to discover this, not assumed. G5
    (gate_g5_spread_economics) still rejects a bad spread if one is ever
    produced regardless, but this selector exists to make a sane pair the
    common case.

    Does not fetch quotes or call the network - `contracts` must already
    be a filtered candidate list (e.g. from filter_by_atm_proximity()).
    """
    spot_price = float(spot_price)
    if option_type not in ("call", "put"):
        raise SpreadBuilderError(f"unsupported option_type: {option_type!r}")

    sorted_contracts = sorted(contracts, key=lambda c: float(_field(c, "strike_price")))
    strikes = [float(_field(c, "strike_price")) for c in sorted_contracts]

    scored_candidates = []
    for i in range(len(sorted_contracts)):
        for j in range(i + 1, len(sorted_contracts)):
            width = strikes[j] - strikes[i]
            if width <= 0:
                continue

            if option_type == "call":
                long_strike, long_idx, short_idx = strikes[i], i, j
            else:
                long_strike, long_idx, short_idx = strikes[j], j, i

            if option_type == "call" and long_strike < spot_price * (1 - max_itm_pct):
                continue
            if option_type == "put" and long_strike > spot_price * (1 + max_itm_pct):
                continue

            width_diff = abs(width - target_width)
            spot_diff = abs(long_strike - spot_price)
            scored_candidates.append((width_diff, spot_diff, long_idx, short_idx))

    if not scored_candidates:
        raise SpreadBuilderError(
            f"could not find any valid strike pair for target_width={target_width}, "
            f"option_type={option_type!r}, spot_price={spot_price} "
            f"(deep-ITM long legs beyond {max_itm_pct:.0%} excluded) among {len(contracts)} contract(s)"
        )

    scored_candidates.sort(key=lambda c: (c[0], c[1]))
    _width_diff, _spot_diff, best_long_idx, best_short_idx = scored_candidates[0]
    return sorted_contracts[best_long_idx], sorted_contracts[best_short_idx]
