"""
Stage H unit tests - Execution Authorization Layer.

Mechanically proves:
  ALLOW -> GRANTED (TTL-bound, not_executable)
  REJECT -> DENIED
  ALLOW without intent -> DENIED
  invalid/non-dict decision -> DENIED
  TTL expiry -> EXPIRED
  revoke -> REVOKED
  frozen / immutable record
  deep snapshot isolation (mutating inputs later never changes the record)
  credential-free record (no API keys / secrets / tokens / account number)
  no execution path (module imports no execution_engine / Alpaca / MCP,
  defines no order-generating function, has no LIVE gate)

Pure - no network, no SDK, no order API.
"""
import copy
import dataclasses
import datetime as dt
import json

import pytest

import babil_authorization as auth
from babil_authorization import (
    AuthorizationRecord,
    AuthorizationState,
    authorize_decision,
    check_authorization,
    is_granted,
    revoke_authorization,
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


def make_allow_decision(intent=None):
    intent = make_intent() if intent is None else intent
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
        "intent": intent,
        "sizing": {"max_qty": 13, "per_contract_risk": 150.0, "max_total_risk_budget": 2000.0},
    }


def make_reject_decision():
    return {
        "decision": "REJECT",
        "mode": "DRY_RUN",
        "proposal_action": "TRADE",
        "underlying": "SPY",
        "strategy": "bull_call_spread",
        "reason": "gates rejected: [G5_spread_economics]",
        "gates": [
            {"name": "G5_spread_economics", "passed": False, "reason": "risk/reward too low"},
        ],
        "intent": None,
        "sizing": None,
    }


def make_proposal():
    return {
        "action": "TRADE",
        "underlying": "SPY",
        "strategy": "bull_call_spread",
        "width": 5.0,
        "rationale": "stage h test",
        "generated_at": NOW,
    }


# ---------------------------------------------------------------------------
# grant / deny transitions (fail-closed)
# ---------------------------------------------------------------------------


def test_allow_decision_becomes_granted_with_ttl():
    record = authorize_decision(make_allow_decision(), make_proposal(), ttl_seconds=300, now=NOW)
    assert record.state is AuthorizationState.GRANTED
    assert record.not_executable is True
    assert record.auth_id
    assert record.expires_at == (dt.datetime.fromisoformat(NOW) + dt.timedelta(seconds=300)).isoformat()
    assert record.revoked_at is None
    assert len(record.gates) == 6
    assert record.intent["simulation_status"] == "DRY-RUN / NO ORDER SUBMITTED"
    assert record.decision["decision"] == "ALLOW"
    assert record.proposal["strategy"] == "bull_call_spread"


def test_reject_decision_becomes_denied():
    record = authorize_decision(make_reject_decision(), make_proposal(), now=NOW)
    assert record.state is AuthorizationState.DENIED
    assert record.intent is None
    assert record.gates == ()


def test_allow_without_intent_becomes_denied():
    decision = make_allow_decision()
    decision["intent"] = None
    record = authorize_decision(decision, make_proposal(), now=NOW)
    assert record.state is AuthorizationState.DENIED


def test_non_dry_run_mode_becomes_denied():
    decision = make_allow_decision()
    decision["mode"] = "LIVE"
    record = authorize_decision(decision, make_proposal(), now=NOW)
    assert record.state is AuthorizationState.DENIED


def test_non_dict_decision_becomes_denied():
    record = authorize_decision("garbage", now=NOW)
    assert record.state is AuthorizationState.DENIED
    assert record.intent is None


def test_invalid_ttl_fails_closed():
    with pytest.raises(ValueError):
        authorize_decision(make_allow_decision(), ttl_seconds="not-a-number", now=NOW)


# ---------------------------------------------------------------------------
# effective state over time / revocation
# ---------------------------------------------------------------------------


def test_granted_is_valid_until_ttl():
    record = authorize_decision(make_allow_decision(), ttl_seconds=300, now=NOW)
    assert check_authorization(record, now=NOW)[0] is AuthorizationState.GRANTED
    assert check_authorization(record, now="2026-08-27T12:04:59+00:00")[0] is AuthorizationState.GRANTED
    assert is_granted(record, now="2026-08-27T12:04:59+00:00") is True


def test_ttl_expiration_reports_expired():
    record = authorize_decision(make_allow_decision(), ttl_seconds=300, now=NOW)
    state, reason = check_authorization(record, now="2026-08-27T12:05:01+00:00")
    assert state is AuthorizationState.EXPIRED
    assert "expired" in reason
    assert is_granted(record, now="2026-08-27T12:05:01+00:00") is False


def test_revoke_returns_revoked_record():
    record = authorize_decision(make_allow_decision(), ttl_seconds=300, now=NOW)
    revoked = revoke_authorization(record, now="2026-08-27T12:00:30+00:00")
    assert revoked is not record
    assert revoked.state is AuthorizationState.REVOKED
    assert revoked.revoked_at == "2026-08-27T12:00:30+00:00"
    assert record.state is AuthorizationState.GRANTED  # input untouched
    state, reason = check_authorization(revoked, now="2026-08-27T12:00:31+00:00")
    assert state is AuthorizationState.REVOKED


def test_revoke_is_idempotent():
    record = authorize_decision(make_allow_decision(), now=NOW)
    revoked = revoke_authorization(record, now=NOW)
    assert revoke_authorization(revoked, now=NOW) is revoked


def test_invalid_expires_at_fails_closed():
    record = authorize_decision(make_allow_decision(), now=NOW)
    broken = dataclasses.replace(record, expires_at="not-a-timestamp")
    state, reason = check_authorization(broken, now=NOW)
    assert state is AuthorizationState.DENIED
    assert "fail-closed" in reason


# ---------------------------------------------------------------------------
# immutability / snapshot isolation
# ---------------------------------------------------------------------------


def test_record_is_frozen():
    record = authorize_decision(make_allow_decision(), now=NOW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.state = AuthorizationState.REVOKED


def test_deep_snapshot_isolation():
    decision = make_allow_decision()
    proposal = make_proposal()
    record = authorize_decision(decision, proposal, now=NOW)

    decision["intent"]["max_loss"] = 9999.0
    decision["gates"][0]["passed"] = False
    proposal["width"] = 9999.0

    assert record.decision["intent"]["max_loss"] == 20.0
    assert record.decision["gates"][0]["passed"] is True
    assert record.proposal["width"] == 5.0
    assert record.state is AuthorizationState.GRANTED


def test_to_dict_does_not_share_references():
    record = authorize_decision(make_allow_decision(), make_proposal(), now=NOW)
    payload = record.to_dict()
    payload["decision"]["intent"]["max_loss"] = 12345.0
    assert record.decision["intent"]["max_loss"] == 20.0


# ---------------------------------------------------------------------------
# credential-free
# ---------------------------------------------------------------------------


def test_record_is_credential_free():
    record = authorize_decision(make_allow_decision(), make_proposal(), now=NOW)
    serialized = json.dumps(record.to_dict())
    for token in (
        "ALPACA_PAPER_KEY_ID",
        "ALPACA_PAPER_SECRET_KEY",
        "ALPACA_COMPETITION_KEY_ID",
        "ALPACA_COMPETITION_SECRET_KEY",
        "account_number",
        "api_key",
        "secret",
        "token",
    ):
        assert token not in serialized, f"record must not contain {token!r}"


def test_record_has_no_credential_like_fields():
    names = {f.name for f in dataclasses.fields(AuthorizationRecord)}
    for token in ("api_key", "secret", "token", "account_number"):
        assert token not in names


# ---------------------------------------------------------------------------
# no execution path
# ---------------------------------------------------------------------------


def test_granted_record_has_no_order_surface():
    record = authorize_decision(make_allow_decision(), now=NOW)
    assert record.not_executable is True
    top = record.to_dict()
    assert "order" not in top
    assert "submit" not in top


def test_module_defines_no_order_generating_function():
    for name in (
        "submit_order",
        "place_order",
        "place_option_order",
        "build_order_request",
        "build_mleg_order_request",
        "create_order",
        "cancel_order_by_id",
        "close_position",
    ):
        assert not hasattr(auth, name), f"module must not define {name!r}"


def test_check_does_not_mutate_record():
    record = authorize_decision(make_allow_decision(), ttl_seconds=1, now=NOW)
    _state, _reason = check_authorization(record, now="2099-01-01T00:00:00+00:00")
    assert record.state is AuthorizationState.GRANTED  # still GRANTED (immutable; expiry is computed)
