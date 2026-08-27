"""
Dynamic Proposal Discovery (Hackathon mode).

Finds vertical-spread candidates in the REAL options universe that pass the
EXISTING G0-G5 gates at the CURRENT quote snapshot. Unlike the single-pair
production selector (mleg_builder.select_vertical_spread_pair, which
optimizes width/spot closeness), this discovery layer:

  1. enumerates bounded near-ATM strike pairs (deep-ITM long legs excluded
     with the SAME max_itm_pct=2% rule the production selector uses),
  2. evaluates every candidate with the UNCHANGED risk gates
     (risk_evaluator.evaluate_all_gates + mleg_builder.compute_spread_risk),
  3. keeps only ALL-PASS candidates (fail-closed: no candidate -> verdict
     VALID_PROPOSAL_NOT_FOUND),
  4. ranks valid candidates with a fully documented, deterministic rule
     (no hidden heuristics):
       1. higher risk/reward  (max_profit / max_loss)
       2. lower net premium
       3. lower spread width
       4. earlier expiration
       5. lower long strike
       6. long symbol lexicographically

This module is pure and data-provider-agnostic: it receives a duck-typed
`analyst` (MarketAnalyst-like) and a `quote_provider` callable
`quote_provider(symbols) -> {symbol: {"bid_price":.., "ask_price":..}}`.
It performs no network access of its own, never imports alpaca /
execution_engine / any MCP transport, never submits an order, and changes
no G0-G5 rule or threshold.
"""
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

import config
import contract_discovery as discovery
import mleg_builder as mb
import risk_evaluator as risk

MAX_ITM_PCT_DEFAULT = 0.02          # mirrors production selector's deep-ITM exclusion
LONG_UPPER_PCT_DEFAULT = 0.10       # documented enumeration bound: long <= spot*(1+0.10)
WIDTH_MIN_DEFAULT = 1.0
WIDTH_MAX_DEFAULT = 12.0
MAX_CANDIDATES_DEFAULT = 200

RANKING_RULE = (
    "1. higher risk/reward (max_profit/max_loss); "
    "2. lower net premium; 3. lower spread width; "
    "4. earlier expiration; 5. lower long strike; 6. long symbol lexicographically."
)

VERDICT_FOUND = "VALID_PROPOSAL_FOUND"
VERDICT_NOT_FOUND = "VALID_PROPOSAL_NOT_FOUND"


class DiscoveryError(Exception):
    pass


@dataclass(frozen=True)
class CandidateProposal:
    """A single evaluated vertical-spread candidate (valid or not)."""

    underlying: str
    option_type: str
    strategy: str
    expiration_date: str
    long_symbol: str
    short_symbol: str
    long_strike: float
    short_strike: float
    width: float
    net_premium: float
    max_profit: float
    max_loss: float
    risk_reward: Optional[float]
    gates: Tuple[risk.GateResult, ...]
    all_passed: bool

    def to_dict(self):
        return {
            "strategy": self.strategy,
            "expiration_date": self.expiration_date,
            "long_symbol": self.long_symbol,
            "short_symbol": self.short_symbol,
            "long_strike": self.long_strike,
            "short_strike": self.short_strike,
            "width": self.width,
            "net_premium": self.net_premium,
            "max_profit": self.max_profit,
            "max_loss": self.max_loss,
            "risk_reward": self.risk_reward,
            "gates": [{"name": g.name, "passed": g.passed, "reason": g.reason} for g in self.gates],
            "all_passed": self.all_passed,
        }


@dataclass(frozen=True)
class DiscoveryResult:
    """Immutable result of a discovery run."""

    generated_at: str
    underlying: str
    option_type: str
    spot_price: float
    candidate_count: int
    valid_count: int
    candidates: Tuple[CandidateProposal, ...]
    best: Optional[CandidateProposal]
    verdict: str


def _rank_key(c: CandidateProposal):
    rr = c.risk_reward if c.risk_reward is not None else float("-inf")
    return (-rr, c.net_premium, c.width, c.expiration_date, c.long_strike, c.long_symbol)


def discover_valid_proposals(
    analyst,
    quote_provider: Callable[[list], dict],
    *,
    underlying: str = "SPY",
    option_type: str = "call",
    dte_min: int = config.DTE_MIN_DAYS,
    dte_max: int = config.DTE_MAX_DAYS,
    max_itm_pct: float = MAX_ITM_PCT_DEFAULT,
    long_upper_pct: float = LONG_UPPER_PCT_DEFAULT,
    width_min: float = WIDTH_MIN_DEFAULT,
    width_max: float = WIDTH_MAX_DEFAULT,
    max_candidates: int = MAX_CANDIDATES_DEFAULT,
    now=None,
):
    """
    Enumerate candidates, evaluate every one with the UNCHANGED G0-G5 gates,
    keep only ALL-PASS candidates, and rank them deterministically.
    Returns a DiscoveryResult; never submits anything.
    """
    if option_type not in ("call", "put"):
        raise DiscoveryError(f"unsupported option_type: {option_type!r}")

    clock = analyst.market_clock()
    account = analyst.account_summary()
    equity = float(account["equity"])
    options_level = int(account["options_trading_level"])
    spot = float(analyst.spot_price(underlying))

    today = dt.date.today()
    exp_gte = (today + dt.timedelta(days=dte_min)).isoformat()
    exp_lte = (today + dt.timedelta(days=dte_max)).isoformat()
    contracts = analyst.option_contracts(underlying, option_type, exp_gte, exp_lte)
    contracts = discovery.filter_by_type(contracts, option_type)
    contracts = [c for c, _d in discovery.filter_by_dte(contracts, dte_min, dte_max, today=today)]

    by_exp = defaultdict(list)
    for c in contracts:
        by_exp[str(c.get("expiration_date", ""))].append(c)
    for exp in by_exp:
        by_exp[exp].sort(key=lambda c: float(c.get("strike_price") or 0.0))

    pairs = []
    seen = set()
    for exp in sorted(by_exp):
        lst = by_exp[exp]
        for i, long_c in enumerate(lst):
            ls = float(long_c.get("strike_price") or 0.0)
            if ls <= 0:
                continue
            # deep-ITM exclusion, exactly mirroring the production selector.
            if option_type == "call" and ls < spot * (1 - max_itm_pct):
                continue
            if option_type == "put" and ls > spot * (1 + max_itm_pct):
                continue
            # documented enumeration bound (not a gate change).
            if option_type == "call" and ls > spot * (1 + long_upper_pct):
                continue
            if option_type == "put" and ls < spot * (1 - long_upper_pct):
                continue
            for j in range(i + 1, len(lst)):
                ss = float(lst[j].get("strike_price") or 0.0)
                width = (ss - ls) if option_type == "call" else (ls - ss)
                if width < width_min or width > width_max:
                    continue
                key = (long_c.get("symbol"), lst[j].get("symbol"))
                if key in seen:
                    continue
                seen.add(key)
                pairs.append((long_c, lst[j], width))
                if len(pairs) >= max_candidates:
                    break
            if len(pairs) >= max_candidates:
                break
        if len(pairs) >= max_candidates:
            break

    symbols = []
    for long_c, short_c, _w in pairs:
        symbols.append(long_c.get("symbol"))
        symbols.append(short_c.get("symbol"))
    quote_map = quote_provider(symbols) if symbols else {}

    def _q(sym):
        q = quote_map.get(sym) or {}
        try:
            return float(q.get("bid_price")), float(q.get("ask_price"))
        except (TypeError, ValueError):
            return None, None

    valid = []
    for long_c, short_c, width in pairs:
        long_bid, long_ask = _q(long_c.get("symbol"))
        short_bid, _short_ask = _q(short_c.get("symbol"))
        if long_ask is None or long_ask <= 0 or short_bid is None:
            continue  # missing/unusable quote -> skip (fail-closed, no fabrication)
        net_premium = max(long_ask - short_bid, 0.0)
        if net_premium <= 0:
            continue
        try:
            spread_risk = mb.compute_spread_risk(net_premium, width, long_c.get("multiplier"), is_debit=True)
            all_passed, gates, _sizing = risk.evaluate_all_gates(
                long_c, long_bid, long_ask, equity, options_level, clock=clock, spread_risk=spread_risk
            )
        except (mb.SpreadBuilderError, TypeError, ValueError):
            continue
        risk_reward = None
        if spread_risk["max_loss"] > 0:
            risk_reward = spread_risk["max_profit"] / spread_risk["max_loss"]
        candidate = CandidateProposal(
            underlying=underlying,
            option_type=option_type,
            strategy="bull_call_spread" if option_type == "call" else "bear_put_spread",
            expiration_date=str(long_c.get("expiration_date", "")),
            long_symbol=str(long_c.get("symbol", "")),
            short_symbol=str(short_c.get("symbol", "")),
            long_strike=float(long_c.get("strike_price")),
            short_strike=float(short_c.get("strike_price")),
            width=float(width),
            net_premium=net_premium,
            max_profit=spread_risk["max_profit"],
            max_loss=spread_risk["max_loss"],
            risk_reward=risk_reward,
            gates=tuple(gates),
            all_passed=all_passed,
        )
        if all_passed:
            valid.append(candidate)

    valid.sort(key=_rank_key)
    generated = now if now is not None else dt.datetime.now(dt.timezone.utc).isoformat()
    verdict = VERDICT_FOUND if valid else VERDICT_NOT_FOUND
    return DiscoveryResult(
        generated_at=generated,
        underlying=underlying,
        option_type=option_type,
        spot_price=spot,
        candidate_count=len(pairs),
        valid_count=len(valid),
        candidates=tuple(valid),
        best=valid[0] if valid else None,
        verdict=verdict,
    )
