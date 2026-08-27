"""
Stage L - Paper Execution Adapter (first controlled execution boundary).

The ONLY module that turns a Stage K READY PreExecutionRecord into a Paper
execution request. The pipeline endpoint is:

    Pre-Execution Firewall (READY)
        -> Final Execution Validation
        -> Paper Execution Adapter
        -> Paper Account (injected broker)

NOTHING here executes a real order unless EVERY condition is satisfied AND
the execution kill switch is explicitly enabled. This module makes no
direct API call of its own: the broker is an injected duck-typed object
(the single broker boundary). Stage L ships with execution_enabled=False
(default), so no submission can occur without an explicit, deliberate
enable + a broker injection. Real Alpaca Paper POSTs are 0 in Stage L.

Design rules:
- build_paper_execution_request(...) = Final Execution Validation. It
  re-validates the entire chain (PreExecution READY, Authorization
  GRANTED, Consumption CONSUMED, Approval APPROVED, all five fingerprints,
  all TTLs) and, only on success, returns an immutable PaperExecutionRequest
  built from the VALIDATED decision intent. Any failure raises
  ExecutionRejectedError and no broker is ever touched.
- submit_paper_execution(request, broker, *, execution_enabled=False, ...)
  is the ONLY path that may call a broker. It re-checks paper-only / not
  executed / replay / TTL / kill switch and performs a GET-only paper
  account verification (broker.verify_paper_account) before calling
  broker.submit. A rejected precondition means zero broker.submit calls.
- Paper-only is structural: there is no LIVE / live-account / mode option
  anywhere in this module's API, and the request carries paper_only=True.
- AI raw output can never reach this module: inputs are the validated
  records/dicts from Stages H/I/J/K, proposal and decision are bound by
  fingerprint, and the request's execution values (legs, qty, limit_price,
  order_type) are derived only from the validated decision intent - never
  from AI price/quantity/strike/premium fields.
- Credentials are never stored: no key/secret/token/account number appears
  in PaperExecutionRequest, ExecutionResult, logs, or error messages.
- order_type is FIXED to the validated vertical-spread MLEG limit structure
  (no free choice of market/limit/stop/trailing/bracket/OCO).

Imports: standard library + the pure validation modules from Stages H-J-K.
This module never imports execution_engine, alpaca, TradingClient, any MCP/
transport module, or any network library.
"""
import datetime as dt
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from babil_authorization import AuthorizationRecord, AuthorizationState, check_authorization
from babil_authorization_consumer import ConsumptionRecord, fingerprint
from babil_human_approval import HumanApprovalRecord, ApprovalState, check_approval
from babil_pre_execution import (
    PreExecutionRecord,
    PreExecutionState,
    check_pre_execution,
    verify_bindings,
)

# The only order structure this adapter can build: a validated MLEG
# vertical-spread limit (no user-selectable order types).
SUPPORTED_ORDER_TYPE = "mleg_limit"

# The only MLEG order class used for options (matches mleg_builder).
SUPPORTED_ORDER_CLASS = "MLEG"


class ExecutionStatus(Enum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    REJECTED = "REJECTED"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


class ExecutionRejectedError(Exception):
    """Raised when the final execution validation is rejected (fail-closed)."""

    def __init__(self, reason, code=None):
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class PaperExecutionRequest:
    """
    Immutable, validated paper execution request. Holds NO credentials,
    API keys, secrets, tokens, account numbers, broker order IDs, or
    client_order_id. Execution values (legs, qty, limit_price) are copied
    from the VALIDATED decision intent only. order_type is fixed.
    """

    execution_id: str
    pre_execution_id: str
    authorization_id: str
    consumption_id: str
    approval_id: str
    created_at: str
    valid_until: str
    underlying: str
    strategy: str
    order_type: str
    order_class: str
    legs: tuple
    qty: int
    limit_price: float | None
    paper_only: Literal[True] = True
    validated: Literal[True] = True
    not_executed: Literal[True] = True
    executed: Literal[False] = False

    def to_dict(self):
        return {
            "execution_id": self.execution_id,
            "pre_execution_id": self.pre_execution_id,
            "authorization_id": self.authorization_id,
            "consumption_id": self.consumption_id,
            "approval_id": self.approval_id,
            "created_at": self.created_at,
            "valid_until": self.valid_until,
            "underlying": self.underlying,
            "strategy": self.strategy,
            "order_type": self.order_type,
            "order_class": self.order_class,
            "legs": [dict(leg) for leg in self.legs],
            "qty": self.qty,
            "limit_price": self.limit_price,
            "paper_only": self.paper_only,
            "validated": self.validated,
            "not_executed": self.not_executed,
            "executed": self.executed,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """
    Immutable execution outcome. broker-generated order ids may appear ONLY
    here (never in any Record). No credentials ever.
    """

    execution_id: str
    pre_execution_id: str
    timestamp: str
    status: ExecutionStatus
    reason: str = ""
    broker_order_id: str | None = None


def _now(now=None):
    if now is not None:
        if isinstance(now, str):
            return dt.datetime.fromisoformat(now)
        return now
    return dt.datetime.now(dt.timezone.utc)


def _min_expiry(*iso_timestamps):
    values = []
    for stamp in iso_timestamps:
        if stamp:
            values.append(dt.datetime.fromisoformat(stamp))
    if not values:
        raise ExecutionRejectedError("no valid expiry available", code="INVALID_TTL")
    return min(values)


def _validated_legs(decision):
    intent = decision.get("intent")
    if not isinstance(intent, dict):
        raise ExecutionRejectedError("decision carries no validated intent", code="MISSING_INTENT")
    raw_legs = intent.get("legs")
    if not isinstance(raw_legs, list) or not raw_legs:
        raise ExecutionRejectedError("validated intent has no legs", code="MISSING_LEGS")
    legs = []
    for raw in raw_legs:
        if not isinstance(raw, dict) or not raw.get("symbol"):
            raise ExecutionRejectedError("malformed leg in validated intent", code="MALFORMED_LEG")
        legs.append(
            {
                "symbol": str(raw["symbol"]),
                "side": str(raw.get("side", "")),
                "ratio_qty": int(raw.get("ratio_qty", 1)),
                "position_intent": str(raw.get("position_intent", "")),
            }
        )
    return tuple(legs)


def build_paper_execution_request(
    pre_execution,
    approval,
    consumption,
    authorization,
    proposal,
    decision,
    *,
    executed_pre_execution_ids=None,
    now=None,
):
    """
    Final Execution Validation: re-validate the entire chain and, only on
    success, return an immutable PaperExecutionRequest. No broker is ever
    touched here. Raises ExecutionRejectedError on any failure.
    """
    if not isinstance(pre_execution, PreExecutionRecord):
        raise ExecutionRejectedError("malformed pre-execution record", code="MALFORMED_PRE_EXECUTION")
    if not isinstance(approval, HumanApprovalRecord):
        raise ExecutionRejectedError("malformed approval", code="MALFORMED_APPROVAL")
    if not isinstance(consumption, ConsumptionRecord):
        raise ExecutionRejectedError("malformed consumption", code="MALFORMED_CONSUMPTION")
    if not isinstance(authorization, AuthorizationRecord):
        raise ExecutionRejectedError("malformed authorization", code="MALFORMED_AUTHORIZATION")
    if not isinstance(proposal, dict) or not proposal:
        raise ExecutionRejectedError("missing/invalid proposal", code="MISSING_PROPOSAL")
    if not isinstance(decision, dict) or not decision:
        raise ExecutionRejectedError("missing/invalid decision", code="MISSING_DECISION")

    if pre_execution.pre_execution_id in set(executed_pre_execution_ids or ()):
        raise ExecutionRejectedError(
            "pre-execution already executed (replay)", code="ALREADY_EXECUTED"
        )

    # Full fingerprint re-binding first (specific mismatch codes).
    ok, code, reason = verify_bindings(
        pre_execution,
        approval=approval,
        consumption=consumption,
        authorization=authorization,
        proposal=proposal,
        decision=decision,
    )
    if not ok:
        raise ExecutionRejectedError(f"binding rejected: {reason}", code=code)

    # Chain state re-validation (covers all TTLs).
    pe_state, pe_reason = check_pre_execution(pre_execution, now=now)
    if pe_state is not PreExecutionState.READY:
        raise ExecutionRejectedError(
            f"pre-execution not ready: {pe_state.value} ({pe_reason})", code=pe_state.value
        )
    auth_state, auth_reason = check_authorization(authorization, now=now)
    if auth_state is not AuthorizationState.GRANTED:
        raise ExecutionRejectedError(
            f"authorization not granted: {auth_state.value} ({auth_reason})", code=auth_state.value
        )
    if consumption.consumed is not True:
        raise ExecutionRejectedError("consumption is not consumed", code="NOT_CONSUMED")
    appr_state, appr_reason = check_approval(approval, consumption=consumption, now=now)
    if appr_state is not ApprovalState.APPROVED:
        raise ExecutionRejectedError(
            f"approval not approved: {appr_state.value} ({appr_reason})", code=appr_state.value
        )

    # Build the request strictly from VALIDATED data.
    try:
        valid_until = _min_expiry(
            pre_execution.expires_at, approval.expires_at, authorization.expires_at
        ).isoformat()
    except (TypeError, ValueError):
        raise ExecutionRejectedError("invalid TTL in the chain", code="INVALID_TTL")

    legs = _validated_legs(decision)
    qty = int(legs[0]["ratio_qty"]) if legs else 1
    intent = decision["intent"]
    net_premium = intent.get("net_premium")
    limit_price = round(float(net_premium), 2) if net_premium is not None else None

    created = _now(now)
    return PaperExecutionRequest(
        execution_id=str(uuid.uuid4()),
        pre_execution_id=pre_execution.pre_execution_id,
        authorization_id=authorization.auth_id,
        consumption_id=consumption.consumption_id,
        approval_id=approval.approval_id,
        created_at=created.isoformat(),
        valid_until=valid_until,
        underlying=str(decision.get("underlying", "")),
        strategy=str(decision.get("strategy", "")),
        order_type=SUPPORTED_ORDER_TYPE,
        order_class=SUPPORTED_ORDER_CLASS,
        legs=legs,
        qty=qty,
        limit_price=limit_price,
    )


def submit_paper_execution(
    execution_request,
    broker,
    *,
    execution_enabled=False,
    executed_pre_execution_ids=None,
    now=None,
):
    """
    The ONLY path that may touch a broker. Returns an immutable
    ExecutionResult; never raises on a rejected outcome.

    Safety gates, in order (any failure -> REJECTED / NOT_ATTEMPTED with
    zero broker.submit calls):
      1. request is a validated PaperExecutionRequest (paper_only=True,
         validated=True, not_executed=True, executed=False)
      2. replay: pre_execution_id not already executed
      3. TTL: now <= valid_until
      4. execution_enabled must be explicitly True (kill switch, default OFF)
      5. GET-only paper account verification (broker.verify_paper_account)
      6. broker.submit(request)
    """
    if not isinstance(execution_request, PaperExecutionRequest):
        return ExecutionResult(
            execution_id="",
            pre_execution_id="",
            timestamp=_now(now).isoformat(),
            status=ExecutionStatus.REJECTED,
            reason="malformed execution request",
        )
    if execution_request.paper_only is not True:
        return _rejected(execution_request, now, "paper_only must be True")
    if execution_request.validated is not True:
        return _rejected(execution_request, now, "request was not validated")
    if execution_request.not_executed is not True:
        return _rejected(execution_request, now, "not_executed must be True")
    if execution_request.executed is not False:
        return _rejected(execution_request, now, "ALREADY_EXECUTED", code="ALREADY_EXECUTED")

    if execution_request.pre_execution_id in set(executed_pre_execution_ids or ()):
        return _rejected(execution_request, now, "ALREADY_EXECUTED (replay)", code="ALREADY_EXECUTED")

    try:
        if _now(now) > dt.datetime.fromisoformat(execution_request.valid_until):
            return _rejected(execution_request, now, "execution TTL expired", code="EXPIRED")
    except (TypeError, ValueError):
        return _rejected(execution_request, now, "invalid valid_until (fail-closed)", code="INVALID_TTL")

    if execution_enabled is not True:
        return ExecutionResult(
            execution_id=execution_request.execution_id,
            pre_execution_id=execution_request.pre_execution_id,
            timestamp=_now(now).isoformat(),
            status=ExecutionStatus.NOT_ATTEMPTED,
            reason="execution disabled (kill switch OFF)",
        )

    verify = getattr(broker, "verify_paper_account", None)
    if verify is not None:
        try:
            verification = verify()
        except Exception as exc:
            return _rejected(execution_request, now, f"paper account verification error: {exc}")
        if isinstance(verification, dict) and verification.get("ok") is not True:
            return _rejected(
                execution_request,
                now,
                f"paper account verification failed: {verification.get('reason', '')}",
                code="PAPER_ACCOUNT_INVALID",
            )

    try:
        broker_result = broker.submit(execution_request)
    except Exception as exc:
        return ExecutionResult(
            execution_id=execution_request.execution_id,
            pre_execution_id=execution_request.pre_execution_id,
            timestamp=_now(now).isoformat(),
            status=ExecutionStatus.FAILED,
            reason=f"broker submission failed: {exc}",
        )

    broker_order_id = None
    if isinstance(broker_result, dict):
        broker_order_id = broker_result.get("order_id")
    return ExecutionResult(
        execution_id=execution_request.execution_id,
        pre_execution_id=execution_request.pre_execution_id,
        timestamp=_now(now).isoformat(),
        status=ExecutionStatus.SUBMITTED,
        reason="submitted to the injected broker (paper)",
        broker_order_id=broker_order_id,
    )


def _rejected(execution_request, now, reason, code=None):
    suffix = f" ({code})" if code else ""
    return ExecutionResult(
        execution_id=execution_request.execution_id,
        pre_execution_id=execution_request.pre_execution_id,
        timestamp=_now(now).isoformat(),
        status=ExecutionStatus.REJECTED,
        reason=f"{reason}{suffix}",
    )
