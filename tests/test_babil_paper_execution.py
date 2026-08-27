"""
Stage L unit tests - Paper Execution Adapter (first controlled execution
boundary).

Proves the adapter is a validated, paper-only, kill-switched gate that
builds a request from VALIDATED data only, and that the injected broker is
never called on any failure. All tests use a FakePaperBroker - the real
Alpaca Paper API is never contacted (Paper API POST = 0).

  READY + APPROVED -> request accepted
  NOT_READY / EXPIRED / REVOKED / DENIED -> reject
  consumption / proposal / decision / approval mismatch -> reject
  replay (2nd build / already executed) -> reject
  paper-only invariant; live option absent; account-verification fail -> reject
  credentials absent from schemas/logs/errors
  AI raw output / price / quantity / strike / premium cannot reach the adapter
  rejected precondition / expired approval / fingerprint mismatch / replay
    -> zero broker.submit calls
"""
import dataclasses
import datetime as dt
import json

import pytest

import babil_authorization as auth
from babil_authorization import authorize_decision
from babil_authorization_consumer import consume
from babil_human_approval import approve as approve_approval
from babil_human_approval import create_approval_request
from babil_paper_execution import (
    SUPPORTED_ORDER_CLASS,
    SUPPORTED_ORDER_TYPE,
    ExecutionRejectedError,
    ExecutionResult,
    ExecutionStatus,
    PaperExecutionRequest,
    build_paper_execution_request,
    submit_paper_execution,
)
from babil_pre_execution import PreExecutionRecord, PreExecutionState, prepare_paper_execution

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
        "rationale": "stage l test",
        "generated_at": NOW,
    }
    base.update(overrides)
    return base


class FakePaperBroker:
    def __init__(self, submit_ok=True, account_ok=True, order_id="fake-order-1"):
        self.submit_calls = []
        self.verify_calls = 0
        self.submit_ok = submit_ok
        self.account_ok = account_ok
        self.order_id = order_id

    def verify_paper_account(self):
        self.verify_calls += 1
        if self.account_ok:
            return {"ok": True, "reason": "", "paper": True, "status": "ACTIVE"}
        return {"ok": False, "reason": "paper account not active or blocked"}

    def submit(self, request):
        self.submit_calls.append(request)
        if not self.submit_ok:
            raise RuntimeError("broker rejected submission")
        return {"order_id": self.order_id, "status": "submitted"}


def make_ready_chain(ttl=300):
    authorization = authorize_decision(make_decision(), make_proposal(), ttl_seconds=ttl, now=NOW)
    consumption = consume(authorization, make_proposal(), make_decision(), now=NOW)
    pending = create_approval_request(consumption, ttl_seconds=ttl, now=NOW)
    approval = approve_approval(pending, explicit_confirmation=True, approved_by="human-operator", now=NOW)
    pre_execution = prepare_paper_execution(
        approval, consumption, authorization, make_proposal(), make_decision(), now=NOW
    )
    return pre_execution, approval, consumption, authorization, make_proposal(), make_decision()


def _code(excinfo):
    return excinfo.value.code


# ---------------------------------------------------------------------------
# build: final execution validation
# ---------------------------------------------------------------------------


def test_ready_approved_builds_request_from_validated_intent():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    request = build_paper_execution_request(
        pre, approval, consumption, authorization, proposal, decision, now=NOW
    )
    assert isinstance(request, PaperExecutionRequest)
    assert request.execution_id
    assert request.pre_execution_id == pre.pre_execution_id
    assert request.authorization_id == authorization.auth_id
    assert request.consumption_id == consumption.consumption_id
    assert request.approval_id == approval.approval_id
    assert request.paper_only is True
    assert request.validated is True
    assert request.not_executed is True
    assert request.executed is False
    assert request.order_type == SUPPORTED_ORDER_TYPE
    assert request.order_class == SUPPORTED_ORDER_CLASS
    assert request.underlying == "SPY"
    assert request.strategy == "bull_call_spread"
    assert request.qty == 1
    assert request.limit_price == 0.20  # derived from validated net_premium, not AI
    assert len(request.legs) == 2
    assert request.legs[0]["symbol"] == "SPY-C-L"
    assert request.valid_until


def test_not_ready_rejected():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    tampered = dataclasses.replace(pre, state=PreExecutionState.NOT_READY)
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(tampered, approval, consumption, authorization, proposal, decision, now=NOW)
    assert _code(exc) == "NOT_READY"


def test_expired_pre_execution_rejected():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain(ttl=1)
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(
            pre, approval, consumption, authorization, proposal, decision,
            now="2026-08-27T12:00:02+00:00",
        )
    assert _code(exc) == "EXPIRED"


def test_revoked_pre_execution_rejected():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    revoked = dataclasses.replace(pre, state=PreExecutionState.REVOKED)
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(revoked, approval, consumption, authorization, proposal, decision, now=NOW)
    assert _code(exc) == "REVOKED"


def test_denied_authorization_rejected():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    denied = dataclasses.replace(authorization, state=auth.AuthorizationState.DENIED)
    with pytest.raises(ExecutionRejectedError):
        build_paper_execution_request(pre, approval, consumption, denied, proposal, decision, now=NOW)


def test_consumption_mismatch_rejected():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    other_authorization = authorize_decision(decision, make_proposal(rationale="other"), ttl_seconds=300, now=NOW)
    other_consumption = consume(other_authorization, make_proposal(rationale="other"), decision, now=NOW)
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(pre, approval, other_consumption, authorization, proposal, decision, now=NOW)
    assert _code(exc) == "CONSUMPTION_MISMATCH"


def test_proposal_mismatch_rejected():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(pre, approval, consumption, authorization, make_proposal(width=99.0), decision, now=NOW)
    assert _code(exc) == "PROPOSAL_MISMATCH"


def test_decision_mismatch_rejected():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    changed = make_decision()
    changed["gates"][0]["passed"] = False
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(pre, approval, consumption, authorization, proposal, changed, now=NOW)
    assert _code(exc) == "DECISION_MISMATCH"


def test_approval_mismatch_rejected():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    tampered = dataclasses.replace(approval, proposal_fingerprint="deadbeef")
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(pre, tampered, consumption, authorization, proposal, decision, now=NOW)
    assert _code(exc) == "APPROVAL_MISMATCH"


def test_malformed_or_missing_input_rejected():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    with pytest.raises(ExecutionRejectedError):
        build_paper_execution_request(None, approval, consumption, authorization, proposal, decision, now=NOW)
    with pytest.raises(ExecutionRejectedError):
        build_paper_execution_request(pre, None, consumption, authorization, proposal, decision, now=NOW)
    with pytest.raises(ExecutionRejectedError):
        build_paper_execution_request(pre, approval, consumption, authorization, None, decision, now=NOW)
    with pytest.raises(ExecutionRejectedError):
        build_paper_execution_request(pre, approval, consumption, authorization, proposal, None, now=NOW)


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def test_replay_second_build_rejected():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    first = build_paper_execution_request(pre, approval, consumption, authorization, proposal, decision, now=NOW)
    assert first.execution_id
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(
            pre, approval, consumption, authorization, proposal, decision,
            executed_pre_execution_ids={pre.pre_execution_id}, now=NOW,
        )
    assert _code(exc) == "ALREADY_EXECUTED"


def test_duplicate_execution_id_rejected_at_submit():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    request = build_paper_execution_request(pre, approval, consumption, authorization, proposal, decision, now=NOW)
    already_executed = dataclasses.replace(request, executed=True)
    broker = FakePaperBroker()
    result = submit_paper_execution(already_executed, broker, execution_enabled=True, now=NOW)
    assert result.status is ExecutionStatus.REJECTED
    assert broker.submit_calls == []
    assert broker.verify_calls == 0


# ---------------------------------------------------------------------------
# paper-only / kill switch / account verification
# ---------------------------------------------------------------------------


def test_paper_only_invariant_and_no_live_option():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    request = build_paper_execution_request(pre, approval, consumption, authorization, proposal, decision, now=NOW)
    assert request.paper_only is True
    names = {f.name for f in dataclasses.fields(PaperExecutionRequest)}
    assert "mode" not in names  # no execution-mode selection
    assert "paper" not in names  # paper is not selectable - it is fixed True
    import babil_paper_execution as module
    for name in ("live", "set_live", "execute_live"):
        assert not hasattr(module, name)


def test_execution_enabled_defaults_to_off():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    request = build_paper_execution_request(pre, approval, consumption, authorization, proposal, decision, now=NOW)
    broker = FakePaperBroker()
    result = submit_paper_execution(request, broker, now=NOW)  # execution_enabled not passed
    assert result.status is ExecutionStatus.NOT_ATTEMPTED
    assert broker.submit_calls == []
    assert broker.verify_calls == 0


def test_enabled_submits_to_injected_broker_once():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    request = build_paper_execution_request(pre, approval, consumption, authorization, proposal, decision, now=NOW)
    broker = FakePaperBroker()
    result = submit_paper_execution(request, broker, execution_enabled=True, now=NOW)
    assert result.status is ExecutionStatus.SUBMITTED
    assert result.pre_execution_id == pre.pre_execution_id
    assert result.broker_order_id == "fake-order-1"
    assert broker.submit_calls == [request]
    assert broker.verify_calls == 1


def test_paper_account_verification_failure_rejects_without_submit():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    request = build_paper_execution_request(pre, approval, consumption, authorization, proposal, decision, now=NOW)
    broker = FakePaperBroker(account_ok=False)
    result = submit_paper_execution(request, broker, execution_enabled=True, now=NOW)
    assert result.status is ExecutionStatus.REJECTED
    assert "PAPER_ACCOUNT_INVALID" in result.reason
    assert broker.submit_calls == []


def test_submit_expired_ttl_rejected():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    request = build_paper_execution_request(pre, approval, consumption, authorization, proposal, decision, now=NOW)
    expired = dataclasses.replace(request, valid_until="2026-08-27T11:00:00+00:00")
    broker = FakePaperBroker()
    result = submit_paper_execution(expired, broker, execution_enabled=True, now=NOW)
    assert result.status is ExecutionStatus.REJECTED
    assert broker.submit_calls == []


def test_broker_failure_reports_failed():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    request = build_paper_execution_request(pre, approval, consumption, authorization, proposal, decision, now=NOW)
    broker = FakePaperBroker(submit_ok=False)
    result = submit_paper_execution(request, broker, execution_enabled=True, now=NOW)
    assert result.status is ExecutionStatus.FAILED
    assert broker.submit_calls == [request]


# ---------------------------------------------------------------------------
# credentials / AI raw output
# ---------------------------------------------------------------------------


def test_request_and_result_are_credential_free():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    request = build_paper_execution_request(pre, approval, consumption, authorization, proposal, decision, now=NOW)
    broker = FakePaperBroker()
    result = submit_paper_execution(request, broker, execution_enabled=True, now=NOW)
    for obj in (request.to_dict(), dataclasses.asdict(result)):
        serialized = json.dumps(obj, default=str)
        for token in ("ALPACA_", "api_key", "secret", "token", "account_number", "account_id"):
            assert token not in serialized, f"must not carry {token!r}"


def test_request_has_no_broker_order_id_field():
    names = {f.name for f in dataclasses.fields(PaperExecutionRequest)}
    assert "order_id" not in names and "client_order_id" not in names and "broker_order_id" not in names


def test_raw_llm_output_cannot_reach_adapter():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    # a raw AI dict is not a valid proposal binding -> rejected before any request
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(
            pre, approval, consumption, authorization,
            {"action": "TRADE", "underlying": "SPY", "strategy": "bull_call_spread", "width": 5.0,
             "rationale": "raw", "price": 1.23, "quantity": 999, "strike": 105, "premium": 1.23},
            decision, now=NOW,
        )
    assert _code(exc) in {"PROPOSAL_MISMATCH"}


def test_ai_price_quantity_strike_premium_ignored():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    request = build_paper_execution_request(pre, approval, consumption, authorization, proposal, decision, now=NOW)
    # request values come from the VALIDATED intent only (net_premium=0.2, ratio_qty=1)
    assert request.limit_price == 0.20
    assert request.qty == 1
    for leg in request.legs:
        assert "price" not in leg and "strike" not in leg and "premium" not in leg


def test_errors_never_contain_credentials():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    tampered = dataclasses.replace(approval, proposal_fingerprint="deadbeef")
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(pre, tampered, consumption, authorization, proposal, decision, now=NOW)
    text = str(exc.value)
    for token in ("ALPACA_", "api_key", "secret", "token", "account_number"):
        assert token not in text


# ---------------------------------------------------------------------------
# security: zero broker calls on every rejection
# ---------------------------------------------------------------------------


def test_rejected_precondition_zero_broker_calls():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    broker = FakePaperBroker()

    tampered = dataclasses.replace(pre, state=PreExecutionState.NOT_READY)
    with pytest.raises(ExecutionRejectedError):
        build_paper_execution_request(tampered, approval, consumption, authorization, proposal, decision, now=NOW)

    with pytest.raises(ExecutionRejectedError):
        build_paper_execution_request(pre, approval, consumption, authorization, make_proposal(width=99.0), decision, now=NOW)

    assert broker.submit_calls == []
    assert broker.verify_calls == 0


def test_expired_approval_zero_broker_calls():
    authorization = authorize_decision(make_decision(), make_proposal(), ttl_seconds=1, now=NOW)
    consumption = consume(authorization, make_proposal(), make_decision(), now=NOW)
    pending = create_approval_request(consumption, ttl_seconds=1, now=NOW)
    approval = approve_approval(pending, explicit_confirmation=True, approved_by="human", now=NOW)
    broker = FakePaperBroker()
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(
            prepare_paper_execution(approval, consumption, authorization, make_proposal(), make_decision(), now=NOW),
            approval, consumption, authorization, make_proposal(), make_decision(),
            now="2026-08-27T12:00:02+00:00",
        )
    assert _code(exc) == "EXPIRED"
    assert broker.submit_calls == []


def test_fingerprint_mismatch_zero_broker_calls():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    broker = FakePaperBroker()
    changed = make_decision()
    changed["gates"][0]["passed"] = False
    with pytest.raises(ExecutionRejectedError) as exc:
        build_paper_execution_request(pre, approval, consumption, authorization, proposal, changed, now=NOW)
    assert _code(exc) == "DECISION_MISMATCH"
    assert broker.submit_calls == []


def test_replay_zero_broker_calls():
    pre, approval, consumption, authorization, proposal, decision = make_ready_chain()
    request = build_paper_execution_request(pre, approval, consumption, authorization, proposal, decision, now=NOW)
    broker = FakePaperBroker()
    result = submit_paper_execution(request, broker, execution_enabled=True, executed_pre_execution_ids={pre.pre_execution_id}, now=NOW)
    assert result.status is ExecutionStatus.REJECTED
    assert broker.submit_calls == []


def test_non_paper_path_impossible():
    import babil_paper_execution as module
    import inspect

    sig = inspect.signature(module.build_paper_execution_request)
    assert "mode" not in sig.parameters
    sig2 = inspect.signature(module.submit_paper_execution)
    assert "mode" not in sig2.parameters
    assert "live" not in sig2.parameters
