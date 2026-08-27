"""
Stage K unit tests - Pre-Execution Boundary / Execution Firewall.

Mechanically proves:
  APPROVED -> READY (paper_only=True, not_executed=True, execution_ready=True)
  PENDING / REJECTED / EXPIRED / REVOKED approval -> REJECT
  CONSUMED mismatch / Proposal mismatch / Decision mismatch ->
    APPROVAL fingerprint mismatch -> REJECT
  Authorization expired -> REJECT; Consumption stale -> REJECT
  malformed record / missing input / replay / tampered input -> REJECT
  paper_only=False / not_executed=False on the record -> REJECT
  schema is credential/order/execution free
  READY does not generate an order and stays not_executed=True

Pure - no network, no SDK, no order API.
"""
import dataclasses
import datetime as dt
import json

import pytest

import babil_authorization as auth
from babil_authorization import authorize_decision
from babil_authorization_consumer import consume
from babil_human_approval import ApprovalState as HumanApprovalState
from babil_human_approval import approve as approve_approval
from babil_human_approval import create_approval_request
from babil_pre_execution import (
    PreExecutionRecord,
    PreExecutionRejectedError,
    PreExecutionState,
    check_pre_execution,
    is_execution_ready,
    prepare_paper_execution,
    revoke_pre_execution,
    verify_bindings,
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
        "rationale": "stage k test",
        "generated_at": NOW,
    }
    base.update(overrides)
    return base


def make_approved_chain(proposal=None, decision=None, auth_ttl=300):
    """authorize -> consume -> approve, returning all four objects."""
    proposal = proposal if proposal is not None else make_proposal()
    decision = decision if decision is not None else make_decision()
    authorization = authorize_decision(decision, proposal, ttl_seconds=auth_ttl, now=NOW)
    consumption = consume(authorization, proposal, decision, now=NOW)
    pending = create_approval_request(consumption, ttl_seconds=auth_ttl, now=NOW)
    approval = approve_approval(pending, explicit_confirmation=True, approved_by="human-operator", now=NOW)
    return approval, consumption, authorization, proposal, decision


def _code(excinfo):
    return excinfo.value.code


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_approved_chain_becomes_ready():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    ready = prepare_paper_execution(
        approval, consumption, authorization, proposal, decision, ttl_seconds=300, now=NOW
    )
    assert ready.state is PreExecutionState.READY
    assert ready.paper_only is True
    assert ready.not_executed is True
    assert ready.execution_ready is True
    assert ready.authorization_id == authorization.auth_id
    assert ready.consumption_id == consumption.consumption_id
    assert ready.approval_id == approval.approval_id
    assert ready.proposal_fingerprint == consumption.proposal_fingerprint
    assert ready.decision_fingerprint == consumption.decision_fingerprint
    assert ready.expires_at == (dt.datetime.fromisoformat(NOW) + dt.timedelta(seconds=300)).isoformat()
    assert is_execution_ready(ready, now=NOW) is True
    assert check_pre_execution(ready, now=NOW)[0] is PreExecutionState.READY


# ---------------------------------------------------------------------------
# fail-closed: approval states / authorization / bindings
# ---------------------------------------------------------------------------


def test_pending_approval_rejected():
    proposal, decision = make_proposal(), make_decision()
    authorization = authorize_decision(decision, proposal, ttl_seconds=300, now=NOW)
    consumption = consume(authorization, proposal, decision, now=NOW)
    pending = create_approval_request(consumption, ttl_seconds=300, now=NOW)
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(pending, consumption, authorization, proposal, decision, now=NOW)
    assert _code(exc) == "PENDING"


def test_rejected_approval_rejected():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    rejected = dataclasses.replace(approval, state=HumanApprovalState.REJECTED)
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(rejected, consumption, authorization, proposal, decision, now=NOW)
    assert _code(exc) == "REJECTED"


def test_expired_approval_rejected():
    proposal, decision = make_proposal(), make_decision()
    authorization = authorize_decision(decision, proposal, ttl_seconds=1, now=NOW)
    consumption = consume(authorization, proposal, decision, now=NOW)
    pending = create_approval_request(consumption, ttl_seconds=1, now=NOW)
    approval = approve_approval(pending, explicit_confirmation=True, approved_by="human", now=NOW)
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(approval, consumption, authorization, proposal, decision, now="2026-08-27T12:00:02+00:00")
    assert _code(exc) == "EXPIRED"


def test_revoked_approval_rejected():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    from babil_human_approval import revoke_approval
    revoked = revoke_approval(approval, now=NOW)
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(revoked, consumption, authorization, proposal, decision, now=NOW)
    assert _code(exc) == "REVOKED"


def test_authorization_expired_rejected():
    approval, consumption, authorization, proposal, decision = make_approved_chain(auth_ttl=1)
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(approval, consumption, authorization, proposal, decision, now="2026-08-27T12:00:02+00:00")
    assert _code(exc) == "EXPIRED"


def test_consumption_stale_rejected():
    approval, consumption, authorization, proposal, decision = make_approved_chain(auth_ttl=1)
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(approval, consumption, authorization, proposal, decision, now="2026-08-27T12:00:02+00:00")
    assert _code(exc) in {"EXPIRED", "CONSUMPTION_STALE"}


def test_consumption_mismatch_rejected():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    other_proposal = make_proposal(rationale="different")
    other_authorization = authorize_decision(decision, other_proposal, ttl_seconds=300, now=NOW)
    other_consumption = consume(other_authorization, other_proposal, decision, now=NOW)
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(approval, other_consumption, authorization, proposal, decision, now=NOW)
    assert _code(exc) == "CONSUMPTION_MISMATCH"


def test_proposal_mismatch_rejected():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(approval, consumption, authorization, make_proposal(width=99.0), decision, now=NOW)
    assert _code(exc) == "PROPOSAL_MISMATCH"


def test_decision_mismatch_rejected():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    changed = make_decision()
    changed["gates"][0]["passed"] = False
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(approval, consumption, authorization, proposal, changed, now=NOW)
    assert _code(exc) == "DECISION_MISMATCH"


def test_approval_fingerprint_mismatch_rejected():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    tampered = dataclasses.replace(approval, proposal_fingerprint="deadbeef")
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(tampered, consumption, authorization, proposal, decision, now=NOW)
    assert _code(exc) == "APPROVAL_MISMATCH"


# ---------------------------------------------------------------------------
# fail-closed: malformed / missing / replay / tampered
# ---------------------------------------------------------------------------


def test_malformed_record_rejected():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    for bad, label in (
        (None, "approval"),
        ("nope", "approval"),
        (None, "consumption"),
        (None, "authorization"),
    ):
        args = {
            "approval": approval, "consumption": consumption, "authorization": authorization,
            "proposal": proposal, "decision": decision,
        }
        args[label] = bad
        with pytest.raises(PreExecutionRejectedError):
            prepare_paper_execution(**args, now=NOW)


def test_missing_or_empty_proposal_decision_rejected():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(approval, consumption, authorization, None, decision, now=NOW)
    assert _code(exc) == "MISSING_PROPOSAL"
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(approval, consumption, authorization, {}, decision, now=NOW)
    assert _code(exc) == "MISSING_PROPOSAL"
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(approval, consumption, authorization, proposal, None, now=NOW)
    assert _code(exc) == "MISSING_DECISION"


def test_replay_rejected():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    first = prepare_paper_execution(approval, consumption, authorization, proposal, decision, now=NOW)
    assert first.state is PreExecutionState.READY
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(
            approval, consumption, authorization, proposal, decision,
            consumed_pre_execution_ids={approval.approval_id}, now=NOW,
        )
    assert _code(exc) == "REPLAY"


def test_not_executable_false_rejected():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    tampered = dataclasses.replace(approval, not_executable=False)
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(tampered, consumption, authorization, proposal, decision, now=NOW)
    assert _code(exc) == "NOT_SAFE"


# ---------------------------------------------------------------------------
# tamper resistance / verify_bindings
# ---------------------------------------------------------------------------


def test_ready_record_unchanged_when_inputs_mutated():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    ready = prepare_paper_execution(approval, consumption, authorization, proposal, decision, now=NOW)
    payload = ready.to_dict()

    proposal["width"] = 9999.0
    decision["gates"][0]["passed"] = False
    consumption.authorization_snapshot["expires_at"] = "hacked"

    assert ready.to_dict() == payload
    assert ready.proposal_fingerprint != ""
    assert ready.decision_fingerprint != ""


def test_verify_bindings_detects_tampering():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    ready = prepare_paper_execution(approval, consumption, authorization, proposal, decision, now=NOW)

    ok, _code_, _reason = verify_bindings(ready, approval=approval, consumption=consumption, authorization=authorization, proposal=proposal, decision=decision)
    assert ok is True

    ok, code, _ = verify_bindings(ready, proposal=make_proposal(width=99.0))
    assert ok is False and code == "PROPOSAL_MISMATCH"

    changed = make_decision()
    changed["gates"][0]["passed"] = False
    ok, code, _ = verify_bindings(ready, decision=changed)
    assert ok is False and code == "DECISION_MISMATCH"

    tampered_consumption = dataclasses.replace(consumption, proposal_fingerprint="deadbeef")
    ok, code, _ = verify_bindings(ready, consumption=tampered_consumption)
    assert ok is False and code == "CONSUMPTION_MISMATCH"

    tampered_approval = dataclasses.replace(approval, proposal_fingerprint="deadbeef")
    ok, code, _ = verify_bindings(ready, approval=tampered_approval)
    assert ok is False and code == "APPROVAL_MISMATCH"

    tampered_auth = dataclasses.replace(authorization, auth_id="deadbeef")
    ok, code, _ = verify_bindings(ready, authorization=tampered_auth)
    assert ok is False and code == "AUTH_MISMATCH"


def test_ready_record_is_frozen():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    ready = prepare_paper_execution(approval, consumption, authorization, proposal, decision, now=NOW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ready.not_executed = False


def test_paper_only_false_rejected_by_check():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    ready = prepare_paper_execution(approval, consumption, authorization, proposal, decision, now=NOW)
    tampered = dataclasses.replace(ready, paper_only=False)
    state, _reason = check_pre_execution(tampered, now=NOW)
    assert state is PreExecutionState.REJECTED
    assert is_execution_ready(tampered, now=NOW) is False


def test_not_executed_false_rejected_by_check():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    ready = prepare_paper_execution(approval, consumption, authorization, proposal, decision, now=NOW)
    tampered = dataclasses.replace(ready, not_executed=False)
    state, _reason = check_pre_execution(tampered, now=NOW)
    assert state is PreExecutionState.REJECTED


def test_ttl_expiration_reports_expired():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    ready = prepare_paper_execution(approval, consumption, authorization, proposal, decision, ttl_seconds=1, now=NOW)
    state, _reason = check_pre_execution(ready, now="2026-08-27T12:00:02+00:00")
    assert state is PreExecutionState.EXPIRED
    assert is_execution_ready(ready, now="2026-08-27T12:00:02+00:00") is False


def test_revoke_pre_execution():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    ready = prepare_paper_execution(approval, consumption, authorization, proposal, decision, now=NOW)
    revoked = revoke_pre_execution(ready, now="2026-08-27T12:00:30+00:00")
    assert revoked.state is PreExecutionState.REVOKED
    assert revoked.execution_ready is False
    assert ready.state is PreExecutionState.READY  # input untouched
    assert is_execution_ready(revoked, now=NOW) is False


# ---------------------------------------------------------------------------
# credential / order / execution free + READY is not an order
# ---------------------------------------------------------------------------


def test_ready_record_is_credential_and_order_free():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    ready = prepare_paper_execution(approval, consumption, authorization, proposal, decision, now=NOW)
    serialized = json.dumps(ready.to_dict())
    for token in (
        "ALPACA_",
        "api_key",
        "secret",
        "token",
        "account_number",
        "account_id",
        "order_id",
        "client_order_id",
        "broker_transaction_id",
        "transaction_id",
        "qty",
        "price",
        "strike",
        "premium",
        "position",
        "execution_result",
    ):
        assert token not in serialized, f"PreExecutionRecord must not carry {token!r}"


def test_ready_fields_are_audit_only():
    names = {f.name for f in dataclasses.fields(PreExecutionRecord)}
    assert {"pre_execution_id", "created_at", "expires_at", "authorization_id", "consumption_id", "approval_id",
            "authorization_fingerprint", "consumption_fingerprint", "proposal_fingerprint", "decision_fingerprint",
            "approval_fingerprint", "state", "paper_only", "not_executed", "execution_ready"} <= names
    for token in ("order_id", "qty", "price", "strike", "premium", "api_key", "secret", "token", "account_number"):
        assert token not in names


def test_ready_does_not_generate_order():
    import babil_pre_execution as module

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
        assert not hasattr(module, name), f"pre-execution module must not define {name!r}"
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    ready = prepare_paper_execution(approval, consumption, authorization, proposal, decision, now=NOW)
    assert ready.not_executed is True
    assert "order" not in ready.to_dict()


def test_ready_has_no_execution_engine_reference():
    approval, consumption, authorization, proposal, decision = make_approved_chain()
    ready = prepare_paper_execution(approval, consumption, authorization, proposal, decision, now=NOW)
    assert "execution_engine" not in json.dumps(ready.to_dict())
