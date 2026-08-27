"""
Stage K - Pre-Execution Boundary / Execution Firewall (pure layer).

Final validation of the APPROVED chain into an immutable, time-bound
PreExecutionRecord. The pipeline endpoint is:

    Authorization (GRANTED) -> Consumption (CONSUMED) -> Human Approval
    (APPROVED) -> Pre-Execution Firewall -> PAPER EXECUTION READY -> STOP

READY means "all pre-execution conditions are validated" - it is NOT
permission to place an order. This module never places, cancels, replaces,
or closes an order and never connects to execution: it performs no network
access, no API call, no MCP call, and imports nothing beyond the standard
library plus the pure validation modules from Stages H/J. It never imports
execution_engine, alpaca, TradingClient, MCP/transport, requests/httpx/
socket/subprocess, and there is no function that converts a READY record
into an order.

Guarantees (fail-closed; any rejection raises PreExecutionRejectedError
with a machine-readable code):
- approval must be APPROVED and bound to the consumption (PENDING /
  REJECTED / EXPIRED / REVOKED, consumption/proposal/decision/approval
  fingerprint mismatches all reject)
- consumption must be CONSUMED and its authorization snapshot not stale
- authorization must be GRANTED (not expired / revoked / denied)
- proposal and decision fingerprints must match the consumption
- every input must be the correct immutable record type / dict (None,
  wrong type, empty dict all reject)
- replay is rejected via an explicitly-passed consumed_pre_execution_ids
  set (no global database)
- the PreExecutionRecord is frozen, paper_only=True and not_executed=True
  are fixed, and TTL expiry is reported as EXPIRED (never auto-renewed)
"""
import datetime as dt
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from babil_authorization import AuthorizationRecord, AuthorizationState, check_authorization
from babil_authorization_consumer import ConsumptionRecord, fingerprint
from babil_human_approval import HumanApprovalRecord, ApprovalState, check_approval

DEFAULT_PRE_EXECUTION_TTL_SECONDS = 300


class PreExecutionState(Enum):
    NOT_READY = "NOT_READY"
    READY = "READY"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    REJECTED = "REJECTED"


class PreExecutionRejectedError(Exception):
    """Raised when a pre-execution validation is rejected (fail-closed)."""

    def __init__(self, reason, code=None):
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class PreExecutionRecord:
    """
    Immutable proof that all pre-execution conditions were validated.

    Holds identifiers + binding fingerprints only. It carries no
    credentials, API keys, secrets, tokens, account numbers, order IDs,
    broker transaction IDs, price/quantity/strike/premium values,
    client_order_id, execution results, or positions. paper_only and
    not_executed are always True - this record is a validation verdict,
    never an order and never an execution instruction.
    """

    pre_execution_id: str
    created_at: str
    expires_at: str
    authorization_id: str
    consumption_id: str
    approval_id: str
    authorization_fingerprint: str
    consumption_fingerprint: str
    proposal_fingerprint: str
    decision_fingerprint: str
    approval_fingerprint: str
    state: PreExecutionState
    paper_only: Literal[True] = True
    not_executed: Literal[True] = True
    execution_ready: bool = True

    def to_dict(self):
        return {
            "pre_execution_id": self.pre_execution_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "authorization_id": self.authorization_id,
            "consumption_id": self.consumption_id,
            "approval_id": self.approval_id,
            "authorization_fingerprint": self.authorization_fingerprint,
            "consumption_fingerprint": self.consumption_fingerprint,
            "proposal_fingerprint": self.proposal_fingerprint,
            "decision_fingerprint": self.decision_fingerprint,
            "approval_fingerprint": self.approval_fingerprint,
            "state": self.state.value,
            "paper_only": self.paper_only,
            "not_executed": self.not_executed,
            "execution_ready": self.execution_ready,
        }


def _now(now=None):
    if now is not None:
        if isinstance(now, str):
            return dt.datetime.fromisoformat(now)
        return now
    return dt.datetime.now(dt.timezone.utc)


def prepare_paper_execution(
    approval,
    consumption,
    authorization,
    proposal,
    decision,
    *,
    consumed_pre_execution_ids=None,
    ttl_seconds=DEFAULT_PRE_EXECUTION_TTL_SECONDS,
    now=None,
):
    """
    Final validation of the approved chain. On success returns a frozen
    PreExecutionRecord with state READY (paper_only=True, not_executed=True,
    execution_ready=True). On any failure raises PreExecutionRejectedError.

    `consumed_pre_execution_ids` is the explicit set of approval_ids that
    already produced a READY record (replay protection). All bindings are
    re-validated from the current inputs; inputs are never mutated.
    """
    if not isinstance(approval, HumanApprovalRecord):
        raise PreExecutionRejectedError("malformed approval", code="MALFORMED_APPROVAL")
    if not isinstance(consumption, ConsumptionRecord):
        raise PreExecutionRejectedError("malformed consumption", code="MALFORMED_CONSUMPTION")
    if not isinstance(authorization, AuthorizationRecord):
        raise PreExecutionRejectedError("malformed authorization", code="MALFORMED_AUTHORIZATION")
    if not isinstance(proposal, dict) or not proposal:
        raise PreExecutionRejectedError("missing/invalid proposal", code="MISSING_PROPOSAL")
    if not isinstance(decision, dict) or not decision:
        raise PreExecutionRejectedError("missing/invalid decision", code="MISSING_DECISION")

    if approval.not_executable is not True:
        raise PreExecutionRejectedError("approval is marked executable", code="NOT_SAFE")
    if authorization.not_executable is not True:
        raise PreExecutionRejectedError("authorization is marked executable", code="NOT_SAFE")
    if consumption.authorization_snapshot.get("not_executable") is not True:
        raise PreExecutionRejectedError("consumption authorization is marked executable", code="NOT_SAFE")

    if approval.approval_id in set(consumed_pre_execution_ids or ()):
        raise PreExecutionRejectedError(
            "approval already prepared for execution (replay)", code="REPLAY"
        )

    # Authorization must be GRANTED (covers expired / revoked / denied).
    auth_state, auth_reason = check_authorization(authorization, now=now)
    if auth_state is not AuthorizationState.GRANTED:
        raise PreExecutionRejectedError(
            f"authorization not granted: {auth_state.value} ({auth_reason})",
            code=auth_state.value,
        )

    if consumption.consumed is not True:
        raise PreExecutionRejectedError("consumption is not consumed", code="NOT_CONSUMED")

    # Approval <-> consumption binding (specific mismatch codes first).
    if approval.consumption_id != consumption.consumption_id:
        raise PreExecutionRejectedError(
            "approval is not bound to this consumption", code="CONSUMPTION_MISMATCH"
        )
    if fingerprint(consumption.to_dict()) != approval.consumption_fingerprint:
        raise PreExecutionRejectedError(
            "consumption fingerprint mismatch", code="CONSUMPTION_MISMATCH"
        )
    if approval.proposal_fingerprint != consumption.proposal_fingerprint:
        raise PreExecutionRejectedError(
            "approval proposal binding mismatch", code="APPROVAL_MISMATCH"
        )
    if approval.decision_fingerprint != consumption.decision_fingerprint:
        raise PreExecutionRejectedError(
            "approval decision binding mismatch", code="APPROVAL_MISMATCH"
        )

    # Approval must be APPROVED (covers PENDING / REJECTED / EXPIRED / REVOKED).
    appr_state, appr_reason = check_approval(approval, consumption=consumption, now=now)
    if appr_state is not ApprovalState.APPROVED:
        raise PreExecutionRejectedError(
            f"approval not approved: {appr_state.value} ({appr_reason})",
            code=appr_state.value,
        )

    # Consumption must still be bound to the same authorization and not stale.
    if consumption.authorization_snapshot.get("auth_id") != authorization.auth_id:
        raise PreExecutionRejectedError(
            "consumption is not bound to this authorization", code="AUTH_MISMATCH"
        )
    try:
        snapshot_expires = dt.datetime.fromisoformat(
            consumption.authorization_snapshot.get("expires_at") or ""
        )
    except (TypeError, ValueError):
        raise PreExecutionRejectedError(
            "consumption authorization snapshot has invalid expiry", code="CONSUMPTION_STALE"
        )
    if _now(now) > snapshot_expires:
        raise PreExecutionRejectedError(
            "consumption authorization snapshot is stale (expired)", code="CONSUMPTION_STALE"
        )

    # Proposal / Decision binding to the consumption.
    if fingerprint(proposal) != consumption.proposal_fingerprint:
        raise PreExecutionRejectedError(
            "proposal fingerprint mismatch", code="PROPOSAL_MISMATCH"
        )
    if fingerprint(decision) != consumption.decision_fingerprint:
        raise PreExecutionRejectedError(
            "decision fingerprint mismatch", code="DECISION_MISMATCH"
        )

    created = _now(now)
    expires = created + dt.timedelta(seconds=int(ttl_seconds))

    return PreExecutionRecord(
        pre_execution_id=str(uuid.uuid4()),
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        authorization_id=authorization.auth_id,
        consumption_id=consumption.consumption_id,
        approval_id=approval.approval_id,
        authorization_fingerprint=fingerprint(authorization.to_dict()),
        consumption_fingerprint=fingerprint(consumption.to_dict()),
        proposal_fingerprint=consumption.proposal_fingerprint,
        decision_fingerprint=consumption.decision_fingerprint,
        approval_fingerprint=fingerprint(approval.to_dict()),
        state=PreExecutionState.READY,
    )


def verify_bindings(
    record,
    *,
    approval=None,
    consumption=None,
    authorization=None,
    proposal=None,
    decision=None,
):
    """
    Tamper-resistance query: recompute fingerprints of the current inputs and
    compare against the READY record. Returns (ok, code, reason). Never
    mutates anything.
    """
    if not isinstance(record, PreExecutionRecord):
        return False, "MALFORMED_RECORD", "malformed pre-execution record"

    if approval is not None:
        if not isinstance(approval, HumanApprovalRecord):
            return False, "MALFORMED_APPROVAL", "malformed approval"
        if approval.approval_id != record.approval_id:
            return False, "APPROVAL_MISMATCH", "approval id mismatch"
        if fingerprint(approval.to_dict()) != record.approval_fingerprint:
            return False, "APPROVAL_MISMATCH", "approval fingerprint mismatch"

    if consumption is not None:
        if not isinstance(consumption, ConsumptionRecord):
            return False, "MALFORMED_CONSUMPTION", "malformed consumption"
        if consumption.consumption_id != record.consumption_id:
            return False, "CONSUMPTION_MISMATCH", "consumption id mismatch"
        if fingerprint(consumption.to_dict()) != record.consumption_fingerprint:
            return False, "CONSUMPTION_MISMATCH", "consumption fingerprint mismatch"

    if authorization is not None:
        if not isinstance(authorization, AuthorizationRecord):
            return False, "MALFORMED_AUTHORIZATION", "malformed authorization"
        if authorization.auth_id != record.authorization_id:
            return False, "AUTH_MISMATCH", "authorization id mismatch"
        if fingerprint(authorization.to_dict()) != record.authorization_fingerprint:
            return False, "AUTH_MISMATCH", "authorization fingerprint mismatch"

    if proposal is not None:
        if fingerprint(proposal) != record.proposal_fingerprint:
            return False, "PROPOSAL_MISMATCH", "proposal fingerprint mismatch"

    if decision is not None:
        if fingerprint(decision) != record.decision_fingerprint:
            return False, "DECISION_MISMATCH", "decision fingerprint mismatch"

    return True, None, ""


def check_pre_execution(record, *, now=None):
    """
    Effective state of a READY record at `now` (fail-closed query).
    Returns (PreExecutionState, reason). Never mutates the record.
    """
    if not isinstance(record, PreExecutionRecord):
        return PreExecutionState.REJECTED, "malformed pre-execution record"
    if record.paper_only is not True:
        return PreExecutionState.REJECTED, "paper_only must be True (paper-only firewall)"
    if record.not_executed is not True:
        return PreExecutionState.REJECTED, "not_executed must be True"
    if record.state is PreExecutionState.REVOKED:
        return PreExecutionState.REVOKED, "pre-execution revoked"
    if record.state is PreExecutionState.REJECTED:
        return PreExecutionState.REJECTED, "pre-execution rejected"
    if record.state is not PreExecutionState.READY:
        return PreExecutionState.NOT_READY, "pre-execution is not ready"
    if record.execution_ready is not True:
        return PreExecutionState.REJECTED, "execution_ready must be True for READY"
    try:
        expires = dt.datetime.fromisoformat(record.expires_at)
    except (TypeError, ValueError):
        return PreExecutionState.REJECTED, "invalid expires_at (fail-closed)"
    if _now(now) > expires:
        return PreExecutionState.EXPIRED, "pre-execution TTL expired"
    return PreExecutionState.READY, "all pre-execution conditions validated (paper execution ready)"


def revoke_pre_execution(record, *, now=None):
    """READY -> REVOKED. Returns a new record; REVOKED is never ready again."""
    if not isinstance(record, PreExecutionRecord):
        raise PreExecutionRejectedError("malformed pre-execution record", code="MALFORMED_RECORD")
    if record.state is not PreExecutionState.READY:
        raise PreExecutionRejectedError(
            f"only a READY pre-execution can be revoked (state={record.state.value})",
            code=record.state.value,
        )
    return PreExecutionRecord(
        pre_execution_id=record.pre_execution_id,
        created_at=record.created_at,
        expires_at=record.expires_at,
        authorization_id=record.authorization_id,
        consumption_id=record.consumption_id,
        approval_id=record.approval_id,
        authorization_fingerprint=record.authorization_fingerprint,
        consumption_fingerprint=record.consumption_fingerprint,
        proposal_fingerprint=record.proposal_fingerprint,
        decision_fingerprint=record.decision_fingerprint,
        approval_fingerprint=record.approval_fingerprint,
        state=PreExecutionState.REVOKED,
        execution_ready=False,
    )


def is_execution_ready(record, *, now=None):
    """True only while the record reports READY and is inside its TTL."""
    state, _reason = check_pre_execution(record, now=now)
    return state is PreExecutionState.READY
