"""
Stage O+ - Full Chain Integration Test (ALLOW-side, synthetic only).

Proves the entire Stage H->L chain end-to-end as ONE integrated test using
fully synthetic / mock / pure data. No market data, no API, no network.

    ALLOW -> Authorization(GRANTED) -> Consumption(CONSUMED)
        -> Human Approval(APPROVED) -> PreExecution(READY)
        -> PaperExecutionRequest -> STOP

submit_paper_execution() is NEVER called for the ALLOW chain; the FakePaperBroker
is never invoked (submit_calls == []). Real API / Paper POST / orders = 0.

Scenarios:
  1 normal ALLOW chain (all states verified, broker untouched)
  2 REJECT chain fails closed (Authorization DENIED -> Consumption impossible)
  3 binding mismatch (Proposal/Decision/Consumption/Approval) all fail closed
  4 replay protection (2nd consume -> ALREADY_CONSUMED)
  5 approval protection (False/None/AI strings/AI self-approval all rejected)
  6 pre-execution protection (PENDING/REJECTED/EXPIRED/REVOKED/tamper/paper/
    not_executed all fail closed)
  7 final execution validation (no AI price/qty/strike/premium/order-type
    injection; request built from validated intent only)
  + kill switch default OFF -> NOT_ATTEMPTED, broker.submit never called
"""
import dataclasses
import datetime as dt
import inspect

import pytest

from babil_authorization import (
    AuthorizationState,
    authorize_decision,
    check_authorization,
)
from babil_authorization_consumer import ConsumptionRejectedError, consume
from babil_human_approval import (
    ApprovalRejectedError,
    ApprovalState,
    approve,
    check_approval,
    create_approval_request,
    reject as reject_approval,
    revoke_approval,
)
from babil_paper_execution import (
    SUPPORTED_ORDER_CLASS,
    SUPPORTED_ORDER_TYPE,
    ExecutionRejectedError,
    ExecutionStatus,
    PaperExecutionRequest,
    build_paper_execution_request,
    submit_paper_execution,
)
from babil_pre_execution import (
    PreExecutionRejectedError,
    PreExecutionState,
    check_pre_execution,
    prepare_paper_execution,
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
    decision["reason"] = "gates rejected: [G5_spread_economics]"
    decision["intent"] = None
    decision["gates"] = [{"name": "G5_spread_economics", "passed": False, "reason": "risk/reward too low"}]
    return decision


def make_proposal(**overrides):
    base = {
        "action": "TRADE",
        "underlying": "SPY",
        "strategy": "bull_call_spread",
        "width": 5.0,
        "rationale": "stage o+ integration test",
        "generated_at": NOW,
    }
    base.update(overrides)
    return base


class FakePaperBroker:
    def __init__(self):
        self.submit_calls = []
        self.verify_calls = 0

    def verify_paper_account(self):
        self.verify_calls += 1
        return {"ok": True}

    def submit(self, request):
        self.submit_calls.append(request)
        return {"order_id": "fake-order"}


def run_allow_chain():
    """authorize -> consume -> approve -> prepare -> build (never submits)."""
    decision = make_decision()
    proposal = make_proposal()

    authorization = authorize_decision(decision, proposal, ttl_seconds=300, now=NOW)
    consumption = consume(authorization, proposal, decision, now=NOW)
    pending = create_approval_request(consumption, ttl_seconds=300, now=NOW)
    approval = approve(pending, explicit_confirmation=True, approved_by="human-operator", now=NOW)
    pre_execution = prepare_paper_execution(approval, consumption, authorization, proposal, decision, now=NOW)
    request = build_paper_execution_request(
        pre_execution, approval, consumption, authorization, proposal, decision, now=NOW
    )
    return authorization, consumption, approval, pre_execution, request, proposal, decision


# ---------------------------------------------------------------------------
# Scenario 1 - normal ALLOW chain
# ---------------------------------------------------------------------------


def test_scenario1_normal_allow_chain():
    broker = FakePaperBroker()
    authorization, consumption, approval, pre_execution, request, proposal, decision = run_allow_chain()

    assert decision["decision"] == "ALLOW"
    assert check_authorization(authorization, now=NOW)[0] is AuthorizationState.GRANTED
    assert consumption.consumed is True
    assert check_approval(approval, consumption=consumption, now=NOW)[0] is ApprovalState.APPROVED
    assert check_pre_execution(pre_execution, now=NOW)[0] is PreExecutionState.READY
    assert pre_execution.paper_only is True
    assert pre_execution.not_executed is True
    assert pre_execution.execution_ready is True

    assert isinstance(request, PaperExecutionRequest)
    assert request.paper_only is True
    assert request.not_executed is True
    assert request.validated is True
    assert request.executed is False
    assert request.order_type == SUPPORTED_ORDER_TYPE
    assert request.order_class == SUPPORTED_ORDER_CLASS

    # broker was never touched
    assert broker.submit_calls == []
    assert broker.verify_calls == 0


# ---------------------------------------------------------------------------
# Scenario 2 - REJECT chain fails closed
# ---------------------------------------------------------------------------


def test_scenario2_reject_chain_fails_closed():
    reject_decision = make_reject_decision()
    proposal = make_proposal()

    authorization = authorize_decision(reject_decision, proposal, now=NOW)
    assert authorization.state is AuthorizationState.DENIED

    with pytest.raises(ConsumptionRejectedError):
        consume(authorization, proposal, reject_decision, now=NOW)

    # approval / pre-execution / request are unreachable (no CONSUMED record)


# ---------------------------------------------------------------------------
# Scenario 3 - binding mismatch, all fail closed
# ---------------------------------------------------------------------------


def test_scenario3_binding_mismatch_all_fail_closed():
    authorization, consumption, approval, pre_execution, _request, proposal, decision = run_allow_chain()

    # Proposal mismatch
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(
            pre_execution, approval, consumption, authorization, make_proposal(width=99.0), decision, now=NOW
        )
    assert exc.value.code == "PROPOSAL_MISMATCH"

    # Decision mismatch
    changed = make_decision()
    changed["gates"][0]["passed"] = False
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(pre_execution, approval, consumption, authorization, proposal, changed, now=NOW)
    assert exc.value.code == "DECISION_MISMATCH"

    # Consumption mismatch
    other_auth = authorize_decision(decision, make_proposal(rationale="other"), ttl_seconds=300, now=NOW)
    other_consumption = consume(other_auth, make_proposal(rationale="other"), decision, now=NOW)
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(pre_execution, approval, other_consumption, authorization, proposal, decision, now=NOW)
    assert exc.value.code == "CONSUMPTION_MISMATCH"

    # Approval mismatch
    tampered_approval = dataclasses.replace(approval, proposal_fingerprint="deadbeef")
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(pre_execution, tampered_approval, consumption, authorization, proposal, decision, now=NOW)
    assert exc.value.code == "APPROVAL_MISMATCH"


# ---------------------------------------------------------------------------
# Scenario 4 - replay protection
# ---------------------------------------------------------------------------


def test_scenario4_replay_protection():
    decision = make_decision()
    proposal = make_proposal()
    authorization = authorize_decision(decision, proposal, ttl_seconds=300, now=NOW)

    first = consume(authorization, proposal, decision, now=NOW)
    assert first.consumed is True

    with pytest.raises(ConsumptionRejectedError) as exc:
        consume(authorization, proposal, decision, consumed_auth_ids={authorization.auth_id}, now=NOW)
    assert exc.value.code == "ALREADY_CONSUMED"


# ---------------------------------------------------------------------------
# Scenario 5 - approval protection (no AI self-approval)
# ---------------------------------------------------------------------------


def test_scenario5_approval_protection():
    decision = make_decision()
    proposal = make_proposal()
    authorization = authorize_decision(decision, proposal, ttl_seconds=300, now=NOW)
    consumption = consume(authorization, proposal, decision, now=NOW)
    pending = create_approval_request(consumption, ttl_seconds=300, now=NOW)

    for bad_confirmation in (False, None, "approve", "yes", "execute", "confirmed", "true", 1):
        with pytest.raises(ApprovalRejectedError) as exc:
            approve(pending, explicit_confirmation=bad_confirmation, now=NOW)
        assert exc.value.code == "EXPLICIT_CONFIRMATION_REQUIRED"

    for ai_identity in ("ai", "llm", "agent", "claude", "opencode"):
        with pytest.raises(ApprovalRejectedError) as exc:
            approve(pending, explicit_confirmation=True, approved_by=ai_identity, now=NOW)
        assert exc.value.code == "AI_CANNOT_SELF_APPROVE"

    assert check_approval(pending, now=NOW)[0] is ApprovalState.PENDING  # never became APPROVED


# ---------------------------------------------------------------------------
# Scenario 6 - pre-execution protection
# ---------------------------------------------------------------------------


def test_scenario6_pre_execution_protection():
    decision = make_decision()
    proposal = make_proposal()
    authorization = authorize_decision(decision, proposal, ttl_seconds=300, now=NOW)
    consumption = consume(authorization, proposal, decision, now=NOW)
    pending = create_approval_request(consumption, ttl_seconds=300, now=NOW)
    approved = approve(pending, explicit_confirmation=True, approved_by="human-operator", now=NOW)
    pre = prepare_paper_execution(approved, consumption, authorization, proposal, decision, now=NOW)

    # PENDING approval
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(pending, consumption, authorization, proposal, decision, now=NOW)
    assert exc.value.code == "PENDING"

    # REJECTED approval
    rejected = reject_approval(create_approval_request(consumption, ttl_seconds=300, now=NOW), now=NOW)
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(rejected, consumption, authorization, proposal, decision, now=NOW)
    assert exc.value.code == "REJECTED"

    # EXPIRED approval (short TTL, later now)
    authorization_e = authorize_decision(decision, proposal, ttl_seconds=1, now=NOW)
    consumption_e = consume(authorization_e, proposal, decision, now=NOW)
    pending_e = create_approval_request(consumption_e, ttl_seconds=1, now=NOW)
    approved_e = approve(pending_e, explicit_confirmation=True, approved_by="human", now=NOW)
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(approved_e, consumption_e, authorization_e, proposal, decision,
                                now="2026-08-27T12:00:02+00:00")
    assert exc.value.code == "EXPIRED"

    # REVOKED approval
    revoked = revoke_approval(approved, now=NOW)
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(revoked, consumption, authorization, proposal, decision, now=NOW)
    assert exc.value.code == "REVOKED"

    # tampered fingerprint
    tampered = dataclasses.replace(approved, proposal_fingerprint="deadbeef")
    with pytest.raises(PreExecutionRejectedError) as exc:
        prepare_paper_execution(tampered, consumption, authorization, proposal, decision, now=NOW)
    assert exc.value.code == "APPROVAL_MISMATCH"

    # paper_only=False on the READY record
    tampered_paper = dataclasses.replace(pre, paper_only=False)
    assert check_pre_execution(tampered_paper, now=NOW)[0] is PreExecutionState.REJECTED

    # not_executed=False on the READY record
    tampered_not_executed = dataclasses.replace(pre, not_executed=False)
    assert check_pre_execution(tampered_not_executed, now=NOW)[0] is PreExecutionState.REJECTED


# ---------------------------------------------------------------------------
# Scenario 7 - final execution validation (no order-value injection)
# ---------------------------------------------------------------------------


def test_scenario7_request_built_only_from_validated_intent():
    authorization, consumption, approval, pre_execution, request, proposal, decision = run_allow_chain()

    assert request.limit_price == 0.20  # from validated intent net_premium
    assert request.qty == 1             # from validated intent ratio_qty
    assert request.legs[0]["symbol"] == "SPY-C-L"
    for leg in request.legs:
        assert "price" not in leg and "strike" not in leg and "premium" not in leg

    # order type is fixed - not injectable
    sig = inspect.signature(build_paper_execution_request)
    assert "order_type" not in sig.parameters

    # AI price/qty/strike/premium injected into proposal -> binding mismatch
    ai_proposal = make_proposal(price=1.23, quantity=999, strike=105, premium=1.23)
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(
            pre_execution, approval, consumption, authorization, ai_proposal, decision, now=NOW
        )
    assert exc.value.code == "PROPOSAL_MISMATCH"

    # AI raw output as decision (contains price/qty) -> binding mismatch
    ai_decision = make_decision()
    ai_decision["intent"]["price"] = 1.23
    ai_decision["intent"]["quantity"] = 999
    ai_decision["intent"]["strike"] = 105
    ai_decision["intent"]["premium"] = 1.23
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(
            pre_execution, approval, consumption, authorization, proposal, ai_decision, now=NOW
        )
    assert exc.value.code == "DECISION_MISMATCH"


# ---------------------------------------------------------------------------
# Broker safety - kill switch default OFF, broker.submit never called
# ---------------------------------------------------------------------------


def test_kill_switch_default_off_broker_untouched():
    authorization, consumption, approval, pre_execution, request, proposal, decision = run_allow_chain()
    broker = FakePaperBroker()

    # submit_paper_execution called with default execution_enabled=False
    result = submit_paper_execution(request, broker, now=NOW)
    assert result.status is ExecutionStatus.NOT_ATTEMPTED
    assert broker.submit_calls == []
    assert broker.verify_calls == 0
