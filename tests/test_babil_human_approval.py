"""
Stage J unit tests - Human Execution Authorization Boundary.

Mechanically proves:
  Consumption -> PENDING approval request
  PENDING -> APPROVED only with explicit human confirmation (True);
    False / None / AI strings ("approve","yes","execute","confirmed") reject
  PENDING -> REJECTED; APPROVED -> REVOKED; expiry -> EXPIRED
  Consumption / Proposal / Decision / Scope binding mismatches reject
  replay (re-approve) rejects; REJECTED / EXPIRED / REVOKED never valid
  immutable (frozen) + deep snapshot / tamper resistance
  credential-free / order-free / execution-free / AI cannot self-approve

Pure - no network, no SDK, no order API.
"""
import dataclasses
import datetime as dt
import json

import pytest

import babil_authorization as auth
from babil_authorization import authorize_decision
from babil_authorization_consumer import consume
from babil_human_approval import (
    AI_IDENTITIES,
    ApprovalRejectedError,
    ApprovalState,
    HumanApprovalRecord,
    approve,
    check_approval,
    create_approval_request,
    is_approved,
    reject,
    revoke_approval,
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


def make_proposal(**overrides):
    base = {
        "action": "TRADE",
        "underlying": "SPY",
        "strategy": "bull_call_spread",
        "width": 5.0,
        "rationale": "stage j test",
        "generated_at": NOW,
    }
    base.update(overrides)
    return base


def make_consumption(proposal=None, decision=None, ttl=300):
    proposal = proposal if proposal is not None else make_proposal()
    decision = decision if decision is not None else make_decision()
    record = authorize_decision(decision, proposal, ttl_seconds=ttl, now=NOW)
    return consume(record, proposal, decision, now=NOW)


def make_approval(consumption=None, ttl=300):
    consumption = consumption or make_consumption()
    return create_approval_request(consumption, ttl_seconds=ttl, now=NOW)


def _code(excinfo):
    return excinfo.value.code


# ---------------------------------------------------------------------------
# state transitions
# ---------------------------------------------------------------------------


def test_create_approval_request_pending_and_bound():
    consumption = make_consumption()
    approval = create_approval_request(consumption, ttl_seconds=300, now=NOW)

    assert approval.state is ApprovalState.PENDING
    assert approval.approval_id
    assert approval.consumption_id == consumption.consumption_id
    assert approval.not_executable is True
    assert approval.approved_at is None and approval.revoked_at is None
    assert approval.consumption_fingerprint != ""
    assert approval.proposal_fingerprint == consumption.proposal_fingerprint
    assert approval.decision_fingerprint == consumption.decision_fingerprint
    assert approval.approval_scope == {
        "consumption_id": consumption.consumption_id,
        "proposal_fingerprint": consumption.proposal_fingerprint,
        "decision_fingerprint": consumption.decision_fingerprint,
    }
    assert approval.expires_at == (dt.datetime.fromisoformat(NOW) + dt.timedelta(seconds=300)).isoformat()


def test_pending_to_approved_with_explicit_confirmation():
    approval = make_approval()
    approved = approve(approval, explicit_confirmation=True, approved_by="human-operator", now=NOW)
    assert approved.state is ApprovalState.APPROVED
    assert approved.approved_at == NOW
    assert approved.revoked_at is None
    assert approved.not_executable is True
    assert approval.state is ApprovalState.PENDING  # input never mutated
    assert is_approved(approved, now=NOW) is True


def test_pending_to_rejected():
    approval = make_approval()
    rejected = reject(approval, now=NOW)
    assert rejected.state is ApprovalState.REJECTED
    assert is_approved(rejected, now=NOW) is False
    state, _reason = check_approval(rejected, now=NOW)
    assert state is ApprovalState.REJECTED


def test_approved_to_revoked():
    approval = make_approval()
    approved = approve(approval, explicit_confirmation=True, approved_by="human-operator", now=NOW)
    revoked = revoke_approval(approved, now="2026-08-27T12:00:30+00:00")
    assert revoked.state is ApprovalState.REVOKED
    assert revoked.revoked_at == "2026-08-27T12:00:30+00:00"
    assert approved.state is ApprovalState.APPROVED  # input untouched
    state, _reason = check_approval(revoked, now=NOW)
    assert state is ApprovalState.REVOKED
    assert is_approved(revoked, now=NOW) is False


def test_expiration_reported_for_pending():
    approval = make_approval(ttl=1)
    state, _reason = check_approval(approval, now="2026-08-27T12:00:02+00:00")
    assert state is ApprovalState.EXPIRED
    assert is_approved(approval, now="2026-08-27T12:00:02+00:00") is False


def test_expiration_reported_for_approved():
    approval = make_approval(ttl=1)
    approved = approve(approval, explicit_confirmation=True, approved_by="human-operator", now=NOW)
    state, _reason = check_approval(approved, now="2026-08-27T12:00:02+00:00")
    assert state is ApprovalState.EXPIRED
    assert is_approved(approved, now="2026-08-27T12:00:02+00:00") is False


def test_approve_past_ttl_rejected():
    approval = make_approval(ttl=1)
    with pytest.raises(ApprovalRejectedError) as exc:
        approve(approval, explicit_confirmation=True, approved_by="human-operator", now="2026-08-27T12:00:02+00:00")
    assert _code(exc) == "EXPIRED"


# ---------------------------------------------------------------------------
# explicit human confirmation (AI output can never self-approve)
# ---------------------------------------------------------------------------


def test_approve_false_rejected():
    with pytest.raises(ApprovalRejectedError) as exc:
        approve(make_approval(), explicit_confirmation=False, now=NOW)
    assert _code(exc) == "EXPLICIT_CONFIRMATION_REQUIRED"


def test_approve_none_rejected():
    with pytest.raises(ApprovalRejectedError) as exc:
        approve(make_approval(), explicit_confirmation=None, now=NOW)
    assert _code(exc) == "EXPLICIT_CONFIRMATION_REQUIRED"


@pytest.mark.parametrize("token", ["approve", "yes", "execute", "confirmed", "true", "1"])
def test_approve_ai_strings_are_never_human_approval(token):
    with pytest.raises(ApprovalRejectedError) as exc:
        approve(make_approval(), explicit_confirmation=token, now=NOW)
    assert _code(exc) == "EXPLICIT_CONFIRMATION_REQUIRED"


@pytest.mark.parametrize("identity", sorted(AI_IDENTITIES))
def test_ai_identity_cannot_be_the_approver(identity):
    with pytest.raises(ApprovalRejectedError) as exc:
        approve(make_approval(), explicit_confirmation=True, approved_by=identity, now=NOW)
    assert _code(exc) == "AI_CANNOT_SELF_APPROVE"


def test_approve_without_approved_by_is_allowed():
    approved = approve(make_approval(), explicit_confirmation=True, now=NOW)
    assert approved.state is ApprovalState.APPROVED


# ---------------------------------------------------------------------------
# binding (consumption / proposal / decision / scope)
# ---------------------------------------------------------------------------


def test_consumption_mismatch_rejected():
    approval_a = make_approval(make_consumption())
    consumption_b = make_consumption(proposal=make_proposal(rationale="different consumption"))

    with pytest.raises(ApprovalRejectedError) as exc:
        approve(approval_a, explicit_confirmation=True, approved_by="human", consumption=consumption_b, now=NOW)
    assert _code(exc) == "CONSUMPTION_MISMATCH"
    assert is_approved(approval_a, consumption=consumption_b, now=NOW) is False


def test_proposal_mismatch_rejected():
    consumption = make_consumption()
    approval = make_approval(consumption)
    # same consumption_id but a different proposal fingerprint -> proposal binding breaks
    tampered = dataclasses.replace(consumption, proposal_fingerprint="deadbeef")

    with pytest.raises(ApprovalRejectedError) as exc:
        approve(approval, explicit_confirmation=True, approved_by="human", consumption=tampered, now=NOW)
    assert _code(exc) == "PROPOSAL_MISMATCH"
    assert is_approved(approval, consumption=tampered, now=NOW) is False


def test_decision_mismatch_rejected():
    consumption = make_consumption()
    approval = make_approval(consumption)
    tampered = dataclasses.replace(consumption, decision_fingerprint="deadbeef")

    with pytest.raises(ApprovalRejectedError) as exc:
        approve(approval, explicit_confirmation=True, approved_by="human", consumption=tampered, now=NOW)
    assert _code(exc) == "DECISION_MISMATCH"


def test_scope_mismatch_makes_approval_invalid():
    approval = make_approval()
    tampered = dataclasses.replace(approval, approval_scope={"consumption_id": "hacked"})
    state, _reason = check_approval(tampered, now=NOW)
    assert state is ApprovalState.REJECTED
    with pytest.raises(ApprovalRejectedError) as exc:
        approve(tampered, explicit_confirmation=True, approved_by="human", now=NOW)
    assert _code(exc) == "SCOPE_MISMATCH"


def test_correct_consumption_passes_binding():
    consumption = make_consumption()
    approval = make_approval(consumption)
    approved = approve(approval, explicit_confirmation=True, approved_by="human", consumption=consumption, now=NOW)
    assert approved.state is ApprovalState.APPROVED


# ---------------------------------------------------------------------------
# replay / immutability / snapshot isolation
# ---------------------------------------------------------------------------


def test_reapprove_rejected():
    approval = make_approval()
    approved = approve(approval, explicit_confirmation=True, approved_by="human", now=NOW)
    with pytest.raises(ApprovalRejectedError) as exc:
        approve(approved, explicit_confirmation=True, approved_by="human", now=NOW)
    assert _code(exc) == "APPROVED"


def test_rejected_cannot_be_reapproved():
    rejected = reject(make_approval(), now=NOW)
    with pytest.raises(ApprovalRejectedError) as exc:
        approve(rejected, explicit_confirmation=True, approved_by="human", now=NOW)
    assert _code(exc) == "REJECTED"


def test_revoked_cannot_be_reapproved():
    approval = make_approval()
    approved = approve(approval, explicit_confirmation=True, approved_by="human", now=NOW)
    revoked = revoke_approval(approved, now=NOW)
    with pytest.raises(ApprovalRejectedError) as exc:
        approve(revoked, explicit_confirmation=True, approved_by="human", now=NOW)
    assert _code(exc) == "REVOKED"


def test_approval_record_is_frozen():
    approval = make_approval()
    with pytest.raises(dataclasses.FrozenInstanceError):
        approval.state = ApprovalState.APPROVED


def test_tampered_consumption_does_not_change_approval_but_invalidates_binding():
    consumption = make_consumption()
    approval = make_approval(consumption)
    payload = approval.to_dict()

    consumption.authorization_snapshot["expires_at"] = "hacked"  # mutate the consumption after approval

    assert approval.to_dict() == payload  # approval record itself is unchanged
    assert is_approved(approval, consumption=consumption, now=NOW) is False  # binding now invalid


def test_approval_to_dict_returns_deep_copy():
    approval = make_approval()
    payload = approval.to_dict()
    payload["approval_scope"]["consumption_id"] = "hacked"
    payload["proposal_fingerprint"] = "hacked"
    payload["consumption_fingerprint"] = "hacked"
    assert approval.to_dict()["approval_scope"]["consumption_id"] == approval.consumption_id
    assert approval.to_dict()["proposal_fingerprint"] == approval.proposal_fingerprint
    assert approval.to_dict()["consumption_fingerprint"] == approval.consumption_fingerprint


# ---------------------------------------------------------------------------
# credential-free / order-free / execution-free
# ---------------------------------------------------------------------------


def test_approval_record_is_credential_and_order_free():
    approval = make_approval()
    serialized = json.dumps(approval.to_dict())
    for token in (
        "ALPACA_",
        "api_key",
        "secret",
        "token",
        "account_number",
        "account_id",
        "order_id",
        "client_order_id",
        "broker_order_id",
        "transaction_id",
        "qty",
        "price",
        "limit_price",
        "stop_price",
        "strike",
        "premium",
    ):
        assert token not in serialized, f"HumanApprovalRecord must not carry {token!r}"


def test_approval_fields_are_audit_only():
    names = {f.name for f in dataclasses.fields(HumanApprovalRecord)}
    assert "not_executable" in names
    for token in ("order_id", "qty", "price", "strike", "premium", "api_key", "secret", "token", "account_number"):
        assert token not in names


def test_no_approval_to_order_conversion_exists():
    import babil_human_approval as approval_module

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
        "to_order",
        "to_order_request",
        "execute",
    ):
        assert not hasattr(approval_module, name), f"approval module must not define {name!r}"


def test_approval_record_has_no_execution_engine_reference():
    approval = make_approval()
    assert "execution_engine" not in json.dumps(approval.to_dict())
