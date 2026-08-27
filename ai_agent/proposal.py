"""
Stage C - AI Options Trading Strategy Proposal (fixed, validated schema).

The AI (LLM) output is restricted to exactly five fields: action,
underlying, strategy, width, rationale. This module owns that fixed
schema and rejects anything else on input - most importantly the
decision inputs that must never be trusted from the AI (strike, bid/ask,
entry price, quantity, max loss/profit, expected return, order id,
account/position size, contract symbol, ...). Those are always
recomputed downstream from real market data by the existing risk gates,
the MLEG builder, and the execution engine.

"width" is deliberately present: it is an approximate spread *target*
used only to steer spread selection (mleg_builder.select_vertical_spread_pair
target_width), never a price, quantity, or P/L number, and never fed to
G0-G5 evaluation.

This module is pure: no network, no Alpaca SDK import, no order API, no
import of the execution engine. It depends only on the standard library.
"""
import datetime as _dt
import json as _json
import math
from dataclasses import dataclass
from enum import Enum


class ProposalAction(Enum):
    TRADE = "TRADE"
    NO_TRADE = "NO_TRADE"


class OptionsStrategy(Enum):
    BULL_CALL_SPREAD = "bull_call_spread"
    BEAR_PUT_SPREAD = "bear_put_spread"


# Strategy allowlist. Anything the LLM proposes outside this set is
# rejected at parse time. Extend only by adding a member to
# OptionsStrategy (and a matching mleg_builder mapping in
# ai_agent.options_strategy_mapper).
DEFAULT_ALLOWED_STRATEGIES = frozenset(s.value for s in OptionsStrategy)

# The ONLY keys an AI proposal may carry. Any other key - including every
# decision input an LLM should not be allowed to decide - is rejected.
ALLOWED_KEYS = frozenset({"action", "underlying", "strategy", "width", "rationale"})

# Decision inputs that must never be accepted from the AI. Enforced as
# "key must be in ALLOWED_KEYS" (any unexpected key is rejected); this
# list exists so the guarantee is explicit, testable, and searchable.
AI_DECISION_INPUT_KEYS = frozenset(
    {
        "contract",
        "contract_symbol",
        "option_symbol",
        "strike",
        "strike_price",
        "bid",
        "ask",
        "entry_price",
        "price",
        "limit_price",
        "quantity",
        "qty",
        "max_loss",
        "max_profit",
        "expected_return",
        "order_id",
        "client_order_id",
        "account_size",
        "position_size",
    }
)

# Schema sanity bounds. width is a spread-width *target* (e.g. ~$5),
# used only to steer selection, so the bounds are wide but finite.
MAX_UNDERLYING_LEN = 10
MIN_WIDTH_EXCLUSIVE = 0.0
MAX_WIDTH_INCLUSIVE = 100.0
MAX_RATIONALE_LEN = 2000


class ProposalValidationError(Exception):
    """Raised when raw LLM output cannot be parsed into a valid Proposal."""


@dataclass(frozen=True)
class Proposal:
    action: ProposalAction
    underlying: str
    strategy: OptionsStrategy | None
    width: float | None
    rationale: str
    generated_at: str

    def to_dict(self):
        return {
            "action": self.action.value,
            "underlying": self.underlying,
            "strategy": self.strategy.value if self.strategy is not None else None,
            "width": self.width,
            "rationale": self.rationale,
            "generated_at": self.generated_at,
        }


def _now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _coerce_raw(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            obj = _json.loads(raw)
        except (_json.JSONDecodeError, TypeError) as exc:
            raise ProposalValidationError(f"proposal is not valid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise ProposalValidationError("JSON proposal must be a JSON object")
        return obj
    raise ProposalValidationError(f"proposal must be a dict or JSON string, got {type(raw).__name__}")


def _normalise_action(raw):
    if not isinstance(raw, str):
        raise ProposalValidationError(f"action must be 'TRADE' or 'NO_TRADE', got {raw!r}")
    token = raw.strip().upper()
    if token not in ProposalAction.__members__:
        raise ProposalValidationError(f"action must be 'TRADE' or 'NO_TRADE', got {raw!r}")
    return ProposalAction[token]


def _normalise_underlying(raw):
    if not isinstance(raw, str):
        raise ProposalValidationError(f"underlying must be a string, got {raw!r}")
    value = raw.strip().upper()
    if not value:
        raise ProposalValidationError("underlying is empty")
    if len(value) > MAX_UNDERLYING_LEN:
        raise ProposalValidationError(f"underlying {value!r} exceeds {MAX_UNDERLYING_LEN} chars")
    if any(not (c.isalnum() or c in ".-") for c in value):
        raise ProposalValidationError(f"underlying {value!r} contains invalid characters")
    return value


def _normalise_strategy(raw, allowed):
    if not isinstance(raw, str):
        raise ProposalValidationError(f"strategy must be a string, got {raw!r}")
    value = raw.strip().lower()
    if value not in allowed:
        raise ProposalValidationError(f"strategy {value!r} is not allowed; allowed: {sorted(allowed)}")
    return value


def _normalise_width(raw):
    if isinstance(raw, bool):
        raise ProposalValidationError(f"width must be a number, got bool {raw!r}")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ProposalValidationError(f"width must be a finite number, got {raw!r}")
    if not math.isfinite(value):
        raise ProposalValidationError(f"width must be finite, got {value!r}")
    if value <= MIN_WIDTH_EXCLUSIVE:
        raise ProposalValidationError(f"width must be > {MIN_WIDTH_EXCLUSIVE}, got {value}")
    if value > MAX_WIDTH_INCLUSIVE:
        raise ProposalValidationError(f"width must be <= {MAX_WIDTH_INCLUSIVE}, got {value}")
    return value


def _normalise_rationale(raw):
    if not isinstance(raw, str):
        raise ProposalValidationError(f"rationale must be a string, got {raw!r}")
    value = raw.strip()
    if not value:
        raise ProposalValidationError("rationale is empty")
    if len(value) > MAX_RATIONALE_LEN:
        raise ProposalValidationError(f"rationale exceeds {MAX_RATIONALE_LEN} chars")
    return value


def parse_proposal(raw, *, now=None, allowed_strategies=None):
    """
    Parse and validate raw LLM output into a frozen Proposal.

    Raises ProposalValidationError on any invalid input (malformed
    payload, unknown action/strategy, forbidden or unexpected fields,
    unusable width/rationale/underlying). Returns a Proposal on success.
    """
    data = _coerce_raw(raw)

    unknown = sorted(set(data) - ALLOWED_KEYS)
    if unknown:
        raise ProposalValidationError(
            f"unexpected field(s) not permitted in an AI proposal: {unknown}. "
            "Decision inputs such as strike, bid/ask, price, quantity, max loss/profit, "
            "expected return, order id, account/position size and contract must never be "
            "accepted from the AI - they are recomputed from real market data downstream."
        )

    action = _normalise_action(data.get("action"))
    underlying = _normalise_underlying(data.get("underlying"))
    allowed = DEFAULT_ALLOWED_STRATEGIES if allowed_strategies is None else frozenset(allowed_strategies)

    if action is ProposalAction.NO_TRADE:
        if data.get("strategy") is not None:
            raise ProposalValidationError("a NO_TRADE proposal must not carry a strategy")
        if data.get("width") is not None:
            raise ProposalValidationError("a NO_TRADE proposal must not carry a width")
        strategy = None
        width = None
    else:
        strategy = OptionsStrategy(_normalise_strategy(data.get("strategy"), allowed))
        width = _normalise_width(data.get("width"))

    rationale = _normalise_rationale(data.get("rationale"))
    generated_at = now if now is not None else _now()

    return Proposal(
        action=action,
        underlying=underlying,
        strategy=strategy,
        width=width,
        rationale=rationale,
        generated_at=generated_at,
    )


def parse_proposal_safe(raw, *, now=None, allowed_strategies=None):
    """
    Non-raising variant of parse_proposal. Returns (proposal, None) on
    success or (None, reason) on a ProposalValidationError. Callers that
    must not crash on bad LLM output (fail closed -> treat as no-trade)
    should use this.
    """
    try:
        return parse_proposal(raw, now=now, allowed_strategies=allowed_strategies), None
    except ProposalValidationError as exc:
        return None, str(exc)
