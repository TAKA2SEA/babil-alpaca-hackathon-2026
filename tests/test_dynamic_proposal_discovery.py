"""
Dynamic Proposal Discovery - unit tests.

Proves (all with fake data, no network):
  empty candidate universe -> VALID_PROPOSAL_NOT_FOUND
  all candidates rejected -> NOT_FOUND
  1 candidate PASS -> FOUND
  multiple PASS -> deterministic ranking, best = documented rule
  quote variation -> candidate set changes (no fabrication)
  G5 fail -> excluded
  deep-ITM exclusion (max_itm_pct=2%) maintained
  empty/missing quotes -> candidate skipped (fail-closed)
  deterministic ranking is stable across runs and ties are broken by the
  documented rule (lower net premium first)
"""
import datetime as dt

from dynamic_proposal_discovery import (
    RANKING_RULE,
    VERDICT_FOUND,
    VERDICT_NOT_FOUND,
    CandidateProposal,
    discover_valid_proposals,
)

NOW = "2026-08-27T12:00:00+00:00"


def _expiry(days=30):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def make_contract(strike, symbol, exp=None, type_="call", underlying="SPY", **overrides):
    base = {
        "symbol": symbol,
        "underlying_symbol": underlying,
        "type": type_,
        "strike_price": float(strike),
        "expiration_date": exp or _expiry(),
        "status": "active",
        "tradable": True,
        "multiplier": "100",
    }
    base.update(overrides)
    return base


class FakeAnalyst:
    def __init__(self, contracts, spot=100.0, clock=None, account=None):
        self._contracts = contracts
        self._spot = spot
        self._clock = clock or {"is_open": True, "next_open": "", "next_close": ""}
        self._account = account or {"equity": "100000", "options_trading_level": 3, "status": "ACTIVE", "currency": "USD"}

    def option_contracts(self, underlying, option_type, exp_gte, exp_lte):
        return [c for c in self._contracts if c.get("type") == option_type]

    def spot_price(self, underlying):
        return self._spot

    def market_clock(self):
        return dict(self._clock)

    def account_summary(self):
        return dict(self._account)


class FakeQuoteProvider:
    def __init__(self, quotes):
        self._quotes = quotes

    def __call__(self, symbols):
        return {s: dict(self._quotes[s]) for s in symbols if s in self._quotes}


def _good_pair(exp=None):
    """A pair that passes all gates: width 5, net 2.0 -> rr 1.5."""
    exp = exp or _expiry()
    long_c = make_contract(100.0, "L100", exp=exp)
    short_c = make_contract(105.0, "S105", exp=exp)
    quotes = {
        "L100": {"bid_price": 1.90, "ask_price": 2.10},
        "S105": {"bid_price": 0.10, "ask_price": 0.30},
    }
    return [long_c, short_c], quotes


# ---------------------------------------------------------------------------
# verdicts / fail-closed
# ---------------------------------------------------------------------------


def test_empty_universe_not_found():
    analyst = FakeAnalyst([])
    result = discover_valid_proposals(analyst, FakeQuoteProvider({}), now=NOW)
    assert result.verdict == VERDICT_NOT_FOUND
    assert result.valid_count == 0
    assert result.candidate_count == 0
    assert result.best is None


def test_all_candidates_rejected_not_found():
    # long_ask makes net premium >= width -> G5 rejects
    contracts, _ = _good_pair()
    long_c = contracts[0]
    short_c = contracts[1]
    quotes = {
        long_c["symbol"]: {"bid_price": 1.00, "ask_price": 5.00},
        short_c["symbol"]: {"bid_price": 0.10, "ask_price": 0.30},
    }
    result = discover_valid_proposals(FakeAnalyst(contracts, spot=100.0), FakeQuoteProvider(quotes), now=NOW)
    assert result.verdict == VERDICT_NOT_FOUND
    assert result.valid_count == 0


def test_one_valid_candidate_found():
    contracts, quotes = _good_pair()
    result = discover_valid_proposals(FakeAnalyst(contracts, spot=100.0), FakeQuoteProvider(quotes), now=NOW)
    assert result.verdict == VERDICT_FOUND
    assert result.valid_count == 1
    best = result.best
    assert best is not None
    assert best.long_strike == 100.0 and best.short_strike == 105.0
    assert best.all_passed is True
    assert all(g.passed for g in best.gates)
    assert best.risk_reward == 1.5


def test_g5_fail_candidate_excluded():
    contracts, quotes = _good_pair()
    # widen the long ask so net premium drives risk/reward below 0.20
    quotes["L100"] = {"bid_price": 4.60, "ask_price": 4.90}
    quotes["S105"] = {"bid_price": 0.20, "ask_price": 0.30}
    result = discover_valid_proposals(FakeAnalyst(contracts, spot=100.0), FakeQuoteProvider(quotes), now=NOW)
    assert result.valid_count == 0
    assert result.verdict == VERDICT_NOT_FOUND


def test_missing_quote_skips_candidate():
    contracts, _ = _good_pair()
    # only the long quote is provided; short is missing -> candidate skipped
    quotes = {"L100": {"bid_price": 1.90, "ask_price": 2.10}}
    result = discover_valid_proposals(FakeAnalyst(contracts, spot=100.0), FakeQuoteProvider(quotes), now=NOW)
    assert result.valid_count == 0
    assert result.verdict == VERDICT_NOT_FOUND


def test_quote_variation_changes_result():
    contracts, quotes = _good_pair()
    base = discover_valid_proposals(FakeAnalyst(contracts, spot=100.0), FakeQuoteProvider(quotes), now=NOW)
    assert base.verdict == VERDICT_FOUND

    quotes["L100"] = {"bid_price": 4.60, "ask_price": 4.90}  # quotes moved
    moved = discover_valid_proposals(FakeAnalyst(contracts, spot=100.0), FakeQuoteProvider(quotes), now=NOW)
    assert moved.verdict == VERDICT_NOT_FOUND


# ---------------------------------------------------------------------------
# deep-ITM exclusion
# ---------------------------------------------------------------------------


def test_deep_itm_long_leg_excluded():
    exp = _expiry()
    # long below spot*0.98 must never be enumerated as a candidate
    deep_itm = make_contract(97.0, "D97", exp=exp)
    near = make_contract(99.0, "N99", exp=exp)
    short = make_contract(104.0, "S104", exp=exp)
    contracts = [deep_itm, near, short]
    quotes = {
        "N99": {"bid_price": 1.90, "ask_price": 2.10},
        "S104": {"bid_price": 0.10, "ask_price": 0.30},
        "D97": {"bid_price": 3.00, "ask_price": 3.20},
    }
    result = discover_valid_proposals(FakeAnalyst(contracts, spot=100.0), FakeQuoteProvider(quotes), now=NOW)
    # the 97 strike long must not appear in any candidate
    for cand in result.candidates:
        assert cand.long_strike >= 99.0
    assert result.best.long_strike == 99.0


# ---------------------------------------------------------------------------
# deterministic ranking
# ---------------------------------------------------------------------------


def test_deterministic_ranking_highest_rr_first():
    exp_a = _expiry(30)
    exp_b = _expiry(45)
    # A on exp_a: width 5, net 2.0 -> rr 1.5 ; B on exp_b: width 8, net 3.0 -> rr 5/3 ~1.667
    a_long = make_contract(100.0, "A100", exp=exp_a)
    a_short = make_contract(105.0, "A105", exp=exp_a)
    b_long = make_contract(102.0, "B102", exp=exp_b)
    b_short = make_contract(110.0, "B110", exp=exp_b)
    contracts = [a_long, a_short, b_long, b_short]
    quotes = {
        "A100": {"bid_price": 1.90, "ask_price": 2.10},
        "A105": {"bid_price": 0.10, "ask_price": 0.30},
        "B102": {"bid_price": 2.90, "ask_price": 3.10},
        "B110": {"bid_price": 0.10, "ask_price": 0.30},
    }
    result = discover_valid_proposals(FakeAnalyst(contracts, spot=100.0), FakeQuoteProvider(quotes), now=NOW)
    assert result.valid_count == 2
    assert result.best.short_strike == 110.0  # higher risk/reward first
    assert result.best.risk_reward > result.candidates[1].risk_reward


def test_deterministic_ranking_tie_break_lower_net_premium():
    exp_a = _expiry(30)
    exp_b = _expiry(45)
    # both rr == 1.0 : A on exp_a width 4 net 2.0 ; B on exp_b width 6 net 3.0 -> A first (lower net)
    a_long = make_contract(100.0, "A100", exp=exp_a)
    a_short = make_contract(104.0, "A104", exp=exp_a)
    b_long = make_contract(102.0, "B102", exp=exp_b)
    b_short = make_contract(108.0, "B108", exp=exp_b)
    contracts = [a_long, a_short, b_long, b_short]
    quotes = {
        "A100": {"bid_price": 1.90, "ask_price": 2.10},
        "A104": {"bid_price": 0.10, "ask_price": 0.30},
        "B102": {"bid_price": 2.90, "ask_price": 3.10},
        "B108": {"bid_price": 0.10, "ask_price": 0.30},
    }
    result = discover_valid_proposals(FakeAnalyst(contracts, spot=100.0), FakeQuoteProvider(quotes), now=NOW)
    assert result.valid_count == 2
    assert result.best.short_strike == 104.0  # lower net premium wins the tie
    assert result.best.net_premium == 2.0


def test_ranking_stable_across_runs():
    contracts, quotes = _good_pair()
    r1 = discover_valid_proposals(FakeAnalyst(contracts, spot=100.0), FakeQuoteProvider(quotes), now=NOW)
    r2 = discover_valid_proposals(FakeAnalyst(contracts, spot=100.0), FakeQuoteProvider(quotes), now=NOW)
    assert r1.candidates == r2.candidates
    assert r1.best == r2.best


def test_ranking_rule_is_documented():
    assert "risk/reward" in RANKING_RULE
    assert "net premium" in RANKING_RULE
