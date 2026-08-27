"""
Phase 3 - Risk Evaluator (G1-G4).

Pure evaluation logic. No network call, no Alpaca SDK import, no order
submission anywhere in this file. Every gate function takes plain data
(a contract, a quote, account figures) and returns a GateResult; nothing
here calls the Alpaca API.
"""
import math

from config import (
    DTE_MAX_DAYS,
    DTE_MIN_DAYS,
    MAX_LOSS_PCT_OF_EQUITY,
    MIN_RISK_REWARD_RATIO,
    REQUIRED_OPTIONS_TRADING_LEVEL,
    SPREAD_MAX_PCT,
)


class GateResult:
    def __init__(self, name, passed, reason):
        self.name = name
        self.passed = passed
        self.reason = reason

    def __repr__(self):
        return f"GateResult({self.name}, {'PASS' if self.passed else 'REJECTED'}, {self.reason!r})"

    def __eq__(self, other):
        if not isinstance(other, GateResult):
            return NotImplemented
        return (self.name, self.passed, self.reason) == (other.name, other.passed, other.reason)


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_str_value(value):
    """
    Real alpaca-py enum members (AssetStatus.ACTIVE, ...) stringify via
    str() as "AssetStatus.ACTIVE", not their value "active" - confirmed
    against the installed SDK. Prefer .value when present.
    """
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def gate_g0_market_clock(clock):
    """
    G0: refuses to proceed unless the market is currently open, per the
    real alpaca-py Clock model (timestamp, is_open, next_open, next_close -
    confirmed against the installed SDK). This is what should have gated
    the Phase 4.1 probe explicitly instead of discovering the closed
    market only via a 10s poll timeout.
    """
    is_open = _field(clock, "is_open", None)
    if is_open is None:
        return GateResult("G0_market_clock", False, "clock.is_open is missing/unknown")
    if not is_open:
        next_open = _field(clock, "next_open", "unknown")
        return GateResult("G0_market_clock", False, f"market is closed (next_open={next_open})")
    return GateResult("G0_market_clock", True, "market is open")


def gate_g1_contract_validity(contract):
    status = _as_str_value(_field(contract, "status", "")).lower()
    tradable = _field(contract, "tradable", False)
    if status != "active":
        return GateResult("G1_contract_validity", False, f"status={status!r} is not 'active'")
    if not tradable:
        return GateResult("G1_contract_validity", False, "tradable=False")
    return GateResult("G1_contract_validity", True, "status=active, tradable=True")


def gate_g2_spread_liquidity(bid, ask, max_spread_pct=SPREAD_MAX_PCT):
    try:
        bid_f = float(bid)
        ask_f = float(ask)
    except (TypeError, ValueError):
        return GateResult("G2_spread_liquidity", False, f"invalid bid/ask: bid={bid!r} ask={ask!r}")
    if bid_f <= 0 or ask_f <= 0:
        return GateResult("G2_spread_liquidity", False, f"non-positive bid/ask: bid={bid_f} ask={ask_f}")
    if ask_f < bid_f:
        return GateResult("G2_spread_liquidity", False, f"ask({ask_f}) < bid({bid_f}) - invalid quote")
    mid = (bid_f + ask_f) / 2.0
    spread_pct = (ask_f - bid_f) / mid
    if spread_pct > max_spread_pct:
        return GateResult(
            "G2_spread_liquidity",
            False,
            f"spread {spread_pct:.1%} exceeds max {max_spread_pct:.1%} (bid={bid_f}, ask={ask_f})",
        )
    return GateResult("G2_spread_liquidity", True, f"spread {spread_pct:.1%} within max {max_spread_pct:.1%}")


def compute_max_qty(ask_price, contract_multiplier, account_equity, max_loss_pct=MAX_LOSS_PCT_OF_EQUITY):
    """
    G3 sizing math.

    max_loss_pct of account_equity is the maximum total premium that may be
    risked on one long-option position. contract_multiplier must come from
    the contract's own 'multiplier' (or 'size') field - confirmed via a
    real Paper API response to be a numeric string (e.g. "100") that
    requires int() conversion; never hardcoded to 100 by this function.

    Returns (max_qty: int, per_contract_risk: float, max_total_risk_budget: float).
    """
    ask_price = float(ask_price)
    contract_multiplier = int(contract_multiplier)
    account_equity = float(account_equity)

    per_contract_risk = ask_price * contract_multiplier
    max_total_risk_budget = account_equity * max_loss_pct

    if per_contract_risk <= 0:
        return 0, per_contract_risk, max_total_risk_budget

    max_qty = math.floor(max_total_risk_budget / per_contract_risk)
    return max_qty, per_contract_risk, max_total_risk_budget


def gate_g3_exposure_sizing(ask_price, contract, account_equity, max_loss_pct=MAX_LOSS_PCT_OF_EQUITY):
    multiplier_raw = _field(contract, "multiplier", None)
    if multiplier_raw is None:
        multiplier_raw = _field(contract, "size", None)
    if multiplier_raw is None:
        return (
            GateResult("G3_exposure_sizing", False, "no multiplier/size field on contract"),
            0,
            0.0,
            0.0,
        )

    try:
        multiplier = int(multiplier_raw)
    except (TypeError, ValueError):
        return (
            GateResult(
                "G3_exposure_sizing", False, f"multiplier/size not integer-convertible: {multiplier_raw!r}"
            ),
            0,
            0.0,
            0.0,
        )

    max_qty, per_contract_risk, max_total_risk_budget = compute_max_qty(
        ask_price, multiplier, account_equity, max_loss_pct
    )
    if max_qty < 1:
        return (
            GateResult(
                "G3_exposure_sizing",
                False,
                f"max_qty=0: per_contract_risk=${per_contract_risk:.2f} exceeds risk budget "
                f"${max_total_risk_budget:.2f} ({max_loss_pct:.1%} of equity)",
            ),
            max_qty,
            per_contract_risk,
            max_total_risk_budget,
        )
    return (
        GateResult(
            "G3_exposure_sizing",
            True,
            f"max_qty={max_qty} contracts (per-contract risk=${per_contract_risk:.2f}, "
            f"budget=${max_total_risk_budget:.2f})",
        ),
        max_qty,
        per_contract_risk,
        max_total_risk_budget,
    )


def gate_g4_options_level(account_options_trading_level, required_level=REQUIRED_OPTIONS_TRADING_LEVEL):
    try:
        level = int(account_options_trading_level)
    except (TypeError, ValueError):
        return GateResult("G4_options_level", False, f"invalid options trading level: {account_options_trading_level!r}")
    if level < required_level:
        return GateResult(
            "G4_options_level", False, f"account options_trading_level={level} < required {required_level}"
        )
    return GateResult("G4_options_level", True, f"options_trading_level={level} >= required {required_level}")


def gate_g5_spread_economics(spread_risk, min_risk_reward_ratio=MIN_RISK_REWARD_RATIO):
    """
    G5 (Phase 7): rejects an economically dominated multi-leg spread.

    spread_risk is the dict returned by mleg_builder.compute_spread_risk()
    (keys: max_loss, max_profit, width_value, is_debit). Because that
    function computes max_profit = width_value - max_loss by construction,
    "max_profit <= 0" and "net_debit*multiplier >= strike_width*multiplier"
    (i.e. max_loss >= width_value) are the same condition given input from
    that function - both are checked explicitly anyway for clear,
    independent failure messages and as defense-in-depth against a
    differently-constructed spread_risk dict.

    Directly motivated by a real Phase 6 finding: a naive lowest-strike
    SPY bull call spread priced out to max_profit=-$341 (net debit
    exceeded the spread's own width) - nothing previously rejected that.
    """
    max_profit = _field(spread_risk, "max_profit", None)
    max_loss = _field(spread_risk, "max_loss", None)
    width_value = _field(spread_risk, "width_value", None)

    if max_profit is None or max_loss is None or width_value is None:
        return GateResult("G5_spread_economics", False, "spread_risk missing max_profit/max_loss/width_value")

    if max_profit <= 0:
        return GateResult(
            "G5_spread_economics", False,
            f"max_profit=${max_profit:.2f} <= 0 - economically dominated spread (net debit >= spread width)",
        )

    if max_loss <= 0:
        # Positive max_profit with non-positive max_loss would be a
        # risk-free arbitrage - not realistic, fail-closed rather than
        # trust an obviously wrong number.
        return GateResult(
            "G5_spread_economics", False,
            f"max_loss=${max_loss:.2f} <= 0 - implausible risk-free result, refusing (fail-closed)",
        )

    risk_reward_ratio = max_profit / max_loss
    if risk_reward_ratio < min_risk_reward_ratio:
        return GateResult(
            "G5_spread_economics", False,
            f"risk/reward={risk_reward_ratio:.2f} < min {min_risk_reward_ratio:.2f} "
            f"(max_profit=${max_profit:.2f}, max_loss=${max_loss:.2f})",
        )

    return GateResult(
        "G5_spread_economics", True,
        f"max_profit=${max_profit:.2f}, max_loss=${max_loss:.2f}, risk/reward={risk_reward_ratio:.2f}",
    )


def evaluate_all_gates(
    contract,
    bid,
    ask,
    account_equity,
    account_options_trading_level,
    max_spread_pct=SPREAD_MAX_PCT,
    max_loss_pct=MAX_LOSS_PCT_OF_EQUITY,
    required_level=REQUIRED_OPTIONS_TRADING_LEVEL,
    clock=None,
    spread_risk=None,
    min_risk_reward_ratio=MIN_RISK_REWARD_RATIO,
):
    """
    Runs G0(optional)-G4-G5(optional) in order. Returns (all_passed, gate_results, sizing_dict).

    clock=None (default) skips G0; spread_risk=None (default) skips G5.
    Both preserve the exact behavior existing callers/tests already
    depend on. Pass a real (or mocked) Clock object to enforce G0, and a
    mleg_builder.compute_spread_risk() dict to enforce G5 - callers
    evaluating a multi-leg spread (the orchestrator) always pass both;
    single-leg callers pass neither.
    """
    results = []
    if clock is not None:
        results.append(gate_g0_market_clock(clock))

    results.append(gate_g1_contract_validity(contract))
    results.append(gate_g2_spread_liquidity(bid, ask, max_spread_pct))

    g3, max_qty, per_contract_risk, max_total_risk_budget = gate_g3_exposure_sizing(
        ask, contract, account_equity, max_loss_pct
    )
    results.append(g3)
    results.append(gate_g4_options_level(account_options_trading_level, required_level))

    if spread_risk is not None:
        results.append(gate_g5_spread_economics(spread_risk, min_risk_reward_ratio))

    all_passed = all(r.passed for r in results)
    sizing = {
        "max_qty": max_qty,
        "per_contract_risk": per_contract_risk,
        "max_total_risk_budget": max_total_risk_budget,
    }
    return all_passed, results, sizing
