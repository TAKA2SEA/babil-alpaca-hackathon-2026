"""
Stage I unit tests - Authorization Consumption & Replay Protection.

Mechanically proves:
  GRANTED -> consume succeeds (once)
  DENIED / EXPIRED / REVOKED -> reject
  replay (2nd consume) -> ALREADY_CONSUMED reject
  Proposal binding (strategy / underlying / width change -> reject)
  Decision binding (decision change -> reject)
  missing proposal / decision -> reject
  malformed record / not_executable=False -> reject
  deep snapshot isolation (mutating originals never changes the record)
  frozen / credential-free / order-free / execution-free ConsumptionRecord

Pure - no network, no SDK, no order API.
"""
import copy
import dataclasses
import datetime as dt
import json

import pytest

import babil_authorization as auth
from babil_authorization import (
    AuthorizationState,
    authorize_decision,
    check_authorization,
    revoke_authorization,
)
from babil_authorization_consumer import (
    ConsumptionRecord,
    ConsumptionRejectedError,
    consume,
    fingerprint,
)

NOW = "2026-08-27T12:00:00+00:00"


def make_intent():
    return {
        "intent_id": "intent-1",
        "created_at": NOW,
        "mode": "DRY_RUN",
        "underlying": "SPY",
        "strategy": "bull_call_spread",
        "legs": [
            {"symbol": "SPY-C-L", "side": "buy", "ratio_qty": 1, "position_intent": "buy_to_open"},
            {"symbol": "SPY-C-S", "side": "sell", "ratio_qty": 1, "position_intent": "sell_to_open"},
        ],
        "strike_width": 5.0,
        "net_premium": 0.2,
        "max_loss": 20.0,
        "max_profit": 480.0,
        "simulation_status": "DRY-RUN / NO ORDER SUBMITTED",
    }


def make_decision():
    return {
        "decision": "ALLOW",
        "mode": "DRY_RUN",
        "proposal_action": "TRADE",
        "underlying": "SPY",
        "strategy": "bull_call_spread",
        "reason": "all G0-G5 gates passed (DRY-RUN)",
        "gates": [
            {"name": "G0_market_clock", "passed": True, "reason": "market is open"},
            {"name": "G1_contract_validity", "passed": True, "reason": "active/tradable"},
            {"name": "G2_spread_liquidity", "passed": True, "reason": "spread within max"},
            {"name": "G3_exposure_sizing", "passed": True, "reason": "max_qty=13"},
            {"name": "G4_options_level", "passed": True, "reason": "level 3"},
            {"name": "G5_spread_economics", "passed": True, "reason": "risk/reward ok"},
        ],
        "intent": make_intent(),
        "sizing": {"max_qty": 13, "per_contract_risk": 150.0, "max_total_risk_budget": 2000.0},
    }


def make_reject_decision():
    decision = make_decision()
    decision["decision"] = "REJECT"
    decision["intent"] = None
    return decision


def make_proposal(**overrides):
    base = {
        "action": "TRADE",
        "underlying": "SPY",
        "strategy": "bull_call_spread",
        "width": 5.0,
        "rationale": "stage i test",
        "generated_at": NOW,
    }
    base.update(overrides)
    return base


def make_granted(ttl=300):
    return authorize_decision(make_decision(), make_proposal(), ttl_seconds=ttl, now=NOW)


def _reject_code(excinfo):
    return excinfo.value.code


# ---------------------------------------------------------------------------
# grant / reject transitions
# ---------------------------------------------------------------------------


def test_consume_granted_succeeds_once():
    record = make_granted()
    proposal = make_proposal()
    decision = make_decision()
    consumed = consume(record, proposal, decision, now=NOW)

    assert consumed.consumed is True
    assert consumed.auth_id == record.auth_id
    assert consumed.consumption_id
    assert consumed.consumed_at == NOW
    assert consumed.authorization_snapshot["auth_id"] == record.auth_id
    assert consumed.authorization_snapshot["not_executable"] is True
    assert consumed.proposal_fingerprint == fingerprint(proposal)
    assert consumed.decision_fingerprint == fingerprint(decision)
    assert record.state is AuthorizationState.GRANTED  # input never mutated


def test_consume_denied_rejected():
    record = authorize_decision(make_reject_decision(), make_proposal(), now=NOW)
    with pytest.raises(ConsumptionRejectedError) as exc:
        consume(record, make_proposal(), make_decision(), now=NOW)
    assert _reject_code(exc) == "DENIED"


def test_consume_expired_rejected():
    record = make_granted(ttl=1)
    with pytest.raises(ConsumptionRejectedError) as exc:
        consume(record, make_proposal(), make_decision(), now="2026-08-27T12:00:02+00:00")
    assert _reject_code(exc) == "EXPIRED"


def test_consume_revoked_rejected():
    record = make_granted()
    revoked = revoke_authorization(record, now=NOW)
    with pytest.raises(ConsumptionRejectedError) as exc:
        consume(revoked, make_proposal(), make_decision(), now=NOW)
    assert _reject_code(exc) == "REVOKED"


def test_consume_malformed_record_rejected():
    with pytest.raises(ConsumptionRejectedError) as exc:
        consume("not-a-record", make_proposal(), make_decision(), now=NOW)
    assert _reject_code(exc) == "MALFORMED_RECORD"


def test_consume_not_executable_false_rejected():
    record = dataclasses.replace(make_granted(), not_executable=False)
    with pytest.raises(ConsumptionRejectedError) as exc:
        consume(record, make_proposal(), make_decision(), now=NOW)
    assert _reject_code(exc) == "NOT_SAFE"


def test_consume_missing_proposal_rejected():
    with pytest.raises(ConsumptionRejectedError) as exc:
        consume(make_granted(), None, make_decision(), now=NOW)
    assert _reject_code(exc) == "MISSING_PROPOSAL"


def test_consume_missing_decision_rejected():
    with pytest.raises(ConsumptionRejectedError) as exc:
        consume(make_granted(), make_proposal(), None, now=NOW)
    assert _reject_code(exc) == "MISSING_DECISION"


# ---------------------------------------------------------------------------
# replay protection
# ---------------------------------------------------------------------------


def test_replay_second_consume_rejected():
    record = make_granted()
    first = consume(record, make_proposal(), make_decision(), consumed_auth_ids=set(), now=NOW)
    assert first.consumed is True
    with pytest.raises(ConsumptionRejectedError) as exc:
        consume(record, make_proposal(), make_decision(), consumed_auth_ids={record.auth_id}, now=NOW)
    assert _reject_code(exc) == "ALREADY_CONSUMED"


def test_replay_rejected_even_if_other_consumed_present():
    record = make_granted()
    with pytest.raises(ConsumptionRejectedError) as exc:
        consume(record, make_proposal(), make_decision(), consumed_auth_ids={record.auth_id, "other"}, now=NOW)
    assert _reject_code(exc) == "ALREADY_CONSUMED"


# ---------------------------------------------------------------------------
# proposal binding (reuse of Authorization A for Proposal B is forbidden)
# ---------------------------------------------------------------------------


def test_proposal_width_change_rejected():
    record = make_granted()
    with pytest.raises(ConsumptionRejectedError) as exc:
        consume(record, make_proposal(width=99.0), make_decision(), now=NOW)
    assert _reject_code(exc) == "PROPOSAL_MISMATCH"


def test_proposal_strategy_change_rejected():
    record = make_granted()
    with pytest.raises(ConsumptionRejectedError) as exc:
        consume(record, make_proposal(strategy="bear_put_spread"), make_decision(), now=NOW)
    assert _reject_code(exc) == "PROPOSAL_MISMATCH"


def test_proposal_underlying_change_rejected():
    record = make_granted()
    with pytest.raises(ConsumptionRejectedError) as exc:
        consume(record, make_proposal(underlying="QQQ"), make_decision(), now=NOW)
    assert _reject_code(exc) == "PROPOSAL_MISMATCH"


def test_same_proposal_passes():
    record = make_granted()
    consumed = consume(record, make_proposal(), make_decision(), now=NOW)
    assert consumed.consumed is True


# ---------------------------------------------------------------------------
# decision binding
# ---------------------------------------------------------------------------


def test_decision_change_rejected():
    record = make_granted()
    changed = make_decision()
    changed["gates"][0]["passed"] = False
    with pytest.raises(ConsumptionRejectedError) as exc:
        consume(record, make_proposal(), changed, now=NOW)
    assert _reject_code(exc) == "DECISION_MISMATCH"


def test_same_decision_passes():
    record = make_granted()
    consumed = consume(record, make_proposal(), make_decision(), now=NOW)
    assert consumed.consumed is True


# ---------------------------------------------------------------------------
# snapshot isolation / immutability
# ---------------------------------------------------------------------------


def test_consumption_record_unchanged_when_originals_mutated():
    proposal = make_proposal()
    decision = make_decision()
    record = authorize_decision(decision, proposal, ttl_seconds=300, now=NOW)
    consumed = consume(record, proposal, decision, now=NOW)
    payload = consumed.to_dict()

    proposal["width"] = 9999.0
    proposal["strategy"] = "bear_put_spread"
    decision["intent"]["max_loss"] = 9999.0
    decision["gates"][0]["passed"] = False
    decision["decision"] = "REJECT"

    assert consumed.to_dict() == payload  # snapshot + fingerprints are stable
    assert payload["proposal_fingerprint"] == fingerprint(record.proposal)
    assert payload["proposal_fingerprint"] != fingerprint(proposal)
    assert payload["decision_fingerprint"] == fingerprint(record.decision)
    assert payload["decision_fingerprint"] != fingerprint(decision)


def test_consumption_record_is_frozen():
    record = make_granted()
    consumed = consume(record, make_proposal(), make_decision(), now=NOW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        consumed.consumed = False


def test_consume_never_mutates_authorization_record():
    record = make_granted()
    before = record.to_dict()
    consume(record, make_proposal(), make_decision(), now=NOW)
    assert record.to_dict() == before
    assert record.state is AuthorizationState.GRANTED


# ---------------------------------------------------------------------------
# credential-free / order-free / execution-free
# ---------------------------------------------------------------------------


def test_consumption_record_is_credential_and_order_free():
    record = make_granted()
    consumed = consume(record, make_proposal(), make_decision(), now=NOW)
    serialized = json.dumps(consumed.to_dict())
    for token in (
        "ALPACA_",
        "api_key",
        "secret",
        "token",
        "account_number",
        "order_id",
        "order_request",
        "broker_transaction_id",
        "qty",
        "price",
        "strike",
        "limit_price",
        "premium",
        "max_loss",
        "max_profit",
    ):
        assert token not in serialized, f"ConsumptionRecord must not carry {token!r}"


def test_consumption_record_fields_are_audit_only():
    names = {f.name for f in dataclasses.fields(ConsumptionRecord)}
    assert names == {
        "consumption_id",
        "auth_id",
        "consumed_at",
        "authorization_snapshot",
        "proposal_fingerprint",
        "decision_fingerprint",
        "consumed",
    }
    for token in ("order", "execute", "trade", "api_key", "secret", "token", "account_number", "qty", "price", "strike"):
        assert token not in names


def test_consumption_record_has_no_execution_engine_reference():
    consumed = consume(make_granted(), make_proposal(), make_decision(), now=NOW)
    serialized = json.dumps(consumed.to_dict())
    assert "execution_engine" not in serialized
    assert "execution_engine" not in consumed.to_dict()


def test_no_conversion_to_order_exists():
    import babil_authorization_consumer as consumer

    for name in (
        "submit_order",
        "place_order",
        "place_option_order",
        "cancel_order_by_id",
        "replace_order_by_id",
        "close_position",
        "close_all_positions",
        "build_order_request",
        "build_mleg_order_request",
        "execute",
        "to_order",
        "to_order_request",
    ):
        assert not hasattr(consumer, name), f"consumer must not define {name!r}"
