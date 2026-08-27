"""
Stage J - Human Execution Authorization Boundary (pure layer).

Attaches an explicit HUMAN approval to a Stage I ConsumptionRecord, as an
immutable, time-bound, pure-data proof. The pipeline endpoint is:

    Authorization -> Consumption -> Human Approval -> APPROVED -> STOP

APPROVED != EXECUTED. Approval never places an order and never connects
to execution: this module performs no network access, no API call, no MCP
call, and imports nothing beyond the standard library plus the Stage I
consumption layer. It never imports execution_engine, the Alpaca SDK,
competition_account, the orchestrator, or any MCP/transport module, and
there is no function that converts an approval into an order.

Key properties:
- approval_scope is fixed at creation and binds consumption_id +
  proposal_fingerprint + decision_fingerprint, so "what this approval is
  for" can never be changed afterwards.
- Explicit human confirmation: approve() requires explicit_confirmation
  to be literally True. False, None, and any string (including AI tokens
  such as "approve" / "yes" / "execute" / "confirmed") are rejected - AI
  output is never an authority source for human approval, and an AI
  identity can never be recorded as the approver.
- Immutable records: every transition returns a NEW record
  (PENDING -> APPROVED / REJECTED / EXPIRED / REVOKED are immutable
  states; EXPIRED is the effective state reported past the TTL).
- Fail-closed: PENDING / REJECTED / EXPIRED / REVOKED are never valid
  approvals; malformed input, missing consumption, fingerprint mismatch,
  expired TTL, not_executable != True, or explicit_confirmation != True
  are all rejected.
- Replay-safe: an approval can only be approved once (approve() requires
  PENDING); a REJECTED / EXPIRED / REVOKED approval can never become
  valid again.
"""
import copy
import datetime as dt
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from babil_authorization_consumer import ConsumptionRecord, fingerprint

DEFAULT_APPROVAL_TTL_SECONDS = 300

# AI identities that can never be recorded as the human approver.
AI_IDENTITIES = frozenset(
    {
        "ai",
        "llm",
        "agent",
        "assistant",
        "gpt",
        "chatgpt",
        "claude",
        "gemini",
        "opencode",
        "codex",
        "deepseek",
        "copilot",
        "bot",
        "model",
        "anthropic",
        "openai",
    }
)


class ApprovalState(Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class ApprovalRejectedError(Exception):
    """Raised when an approval operation is rejected (fail-closed)."""

    def __init__(self, reason, code=None):
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class HumanApprovalRecord:
    """
    Immutable, time-bound record that a specific ConsumptionRecord was
    explicitly approved (or rejected / revoked) by a human.

    Holds no credentials, API keys, secrets, tokens, account numbers,
    order IDs, broker transaction IDs, or any quantity/price/strike order
    values. `not_executable` is always True - approval is pure data, never
    an order and never an execution instruction.
    """

    approval_id: str
    consumption_id: str
    state: ApprovalState
    created_at: str
    expires_at: str
    approved_at: str | None
    revoked_at: str | None
    consumption_fingerprint: str
    proposal_fingerprint: str
    decision_fingerprint: str
    approval_scope: dict
    not_executable: Literal[True] = True

    def to_dict(self):
        return {
            "approval_id": self.approval_id,
            "consumption_id": self.consumption_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "approved_at": self.approved_at,
            "revoked_at": self.revoked_at,
            "consumption_fingerprint": self.consumption_fingerprint,
            "proposal_fingerprint": self.proposal_fingerprint,
            "decision_fingerprint": self.decision_fingerprint,
            "approval_scope": copy.deepcopy(self.approval_scope),
            "not_executable": self.not_executable,
        }


def _now(now=None):
    if now is not None:
        if isinstance(now, str):
            return dt.datetime.fromisoformat(now)
        return now
    return dt.datetime.now(dt.timezone.utc)


def _require_record(record):
    if not isinstance(record, HumanApprovalRecord):
        raise ApprovalRejectedError("malformed approval record", code="MALFORMED_RECORD")
    if record.not_executable is not True:
        raise ApprovalRejectedError(
            "approval is marked executable - refusing", code="NOT_SAFE"
        )


def _verify_binding(record, consumption):
    """
    Returns (ok, code, reason) for the fixed binding: consumption identity,
    consumption fingerprint, proposal fingerprint, decision fingerprint,
    and approval_scope self-consistency.
    """
    if not isinstance(record, HumanApprovalRecord):
        return False, "MALFORMED_RECORD", "malformed approval record"
    if record.not_executable is not True:
        return False, "NOT_SAFE", "not_executable is not True"

    if consumption is not None:
        if not isinstance(consumption, ConsumptionRecord):
            return False, "MALFORMED_CONSUMPTION", "malformed consumption"
        if consumption.consumption_id != record.consumption_id:
            return False, "CONSUMPTION_MISMATCH", "approval is not bound to this consumption"
        if consumption.proposal_fingerprint != record.proposal_fingerprint:
            return False, "PROPOSAL_MISMATCH", "proposal fingerprint mismatch"
        if consumption.decision_fingerprint != record.decision_fingerprint:
            return False, "DECISION_MISMATCH", "decision fingerprint mismatch"
        if fingerprint(consumption.to_dict()) != record.consumption_fingerprint:
            return False, "CONSUMPTION_MISMATCH", "consumption fingerprint mismatch"

    scope = record.approval_scope or {}
    if (
        scope.get("consumption_id") != record.consumption_id
        or scope.get("proposal_fingerprint") != record.proposal_fingerprint
        or scope.get("decision_fingerprint") != record.decision_fingerprint
    ):
        return False, "SCOPE_MISMATCH", "approval scope is inconsistent with the record"

    return True, None, ""


def create_approval_request(consumption, *, ttl_seconds=DEFAULT_APPROVAL_TTL_SECONDS, now=None):
    """
    Create a PENDING approval bound to a specific ConsumptionRecord.
    The approval_scope (consumption_id + proposal + decision fingerprints)
    is fixed here and can never be changed afterwards.
    """
    if not isinstance(consumption, ConsumptionRecord):
        raise ApprovalRejectedError(
            "malformed consumption - expected a ConsumptionRecord", code="MALFORMED_CONSUMPTION"
        )
    if consumption.consumed is not True:
        raise ApprovalRejectedError(
            "consumption is not consumed - cannot be approved", code="NOT_CONSUMED"
        )
    if not consumption.proposal_fingerprint or not consumption.decision_fingerprint:
        raise ApprovalRejectedError(
            "consumption is missing binding fingerprints", code="MISSING_FINGERPRINT"
        )

    created = _now(now)
    expires = created + dt.timedelta(seconds=int(ttl_seconds))
    scope = {
        "consumption_id": consumption.consumption_id,
        "proposal_fingerprint": consumption.proposal_fingerprint,
        "decision_fingerprint": consumption.decision_fingerprint,
    }

    return HumanApprovalRecord(
        approval_id=str(uuid.uuid4()),
        consumption_id=consumption.consumption_id,
        state=ApprovalState.PENDING,
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        approved_at=None,
        revoked_at=None,
        consumption_fingerprint=fingerprint(consumption.to_dict()),
        proposal_fingerprint=consumption.proposal_fingerprint,
        decision_fingerprint=consumption.decision_fingerprint,
        approval_scope=scope,
    )


def approve(
    record,
    *,
    explicit_confirmation,
    approved_by=None,
    consumption=None,
    now=None,
):
    """
    PENDING -> APPROVED, but ONLY with an explicit human confirmation.

    explicit_confirmation must be literally True (a Python bool). False,
    None, and any string - including AI tokens "approve", "yes",
    "execute", "confirmed" - are rejected, so AI output can never drive
    this transition. approved_by (optional) must be a non-empty label that
    is not an AI identity. Returns a new APPROVED record; never mutates
    the input.
    """
    _require_record(record)

    if explicit_confirmation is not True:
        raise ApprovalRejectedError(
            "explicit human confirmation required (explicit_confirmation must be exactly True)",
            code="EXPLICIT_CONFIRMATION_REQUIRED",
        )
    if approved_by is not None:
        if not isinstance(approved_by, str) or not approved_by.strip():
            raise ApprovalRejectedError("approved_by must be a non-empty label", code="INVALID_APPROVER")
        if approved_by.strip().lower() in AI_IDENTITIES:
            raise ApprovalRejectedError(
                "an AI identity cannot be recorded as the human approver",
                code="AI_CANNOT_SELF_APPROVE",
            )

    ok, code, reason = _verify_binding(record, consumption)
    if not ok:
        raise ApprovalRejectedError(f"approval binding rejected: {reason}", code=code)

    state, reason = check_approval(record, consumption=consumption, now=now)
    if state is not ApprovalState.PENDING:
        raise ApprovalRejectedError(
            f"cannot approve: {state.value} ({reason})", code=state.value
        )

    approved_at = _now(now)
    return HumanApprovalRecord(
        approval_id=record.approval_id,
        consumption_id=record.consumption_id,
        state=ApprovalState.APPROVED,
        created_at=record.created_at,
        expires_at=record.expires_at,
        approved_at=approved_at.isoformat(),
        revoked_at=None,
        consumption_fingerprint=record.consumption_fingerprint,
        proposal_fingerprint=record.proposal_fingerprint,
        decision_fingerprint=record.decision_fingerprint,
        approval_scope=copy.deepcopy(record.approval_scope),
    )


def reject(record, *, now=None):
    """PENDING -> REJECTED. Returns a new record; rejects non-PENDING input."""
    _require_record(record)
    if record.state is not ApprovalState.PENDING:
        raise ApprovalRejectedError(
            f"only a PENDING approval can be rejected (state={record.state.value})",
            code=record.state.value,
        )
    return HumanApprovalRecord(
        approval_id=record.approval_id,
        consumption_id=record.consumption_id,
        state=ApprovalState.REJECTED,
        created_at=record.created_at,
        expires_at=record.expires_at,
        approved_at=None,
        revoked_at=None,
        consumption_fingerprint=record.consumption_fingerprint,
        proposal_fingerprint=record.proposal_fingerprint,
        decision_fingerprint=record.decision_fingerprint,
        approval_scope=copy.deepcopy(record.approval_scope),
    )


def revoke_approval(record, *, now=None):
    """APPROVED -> REVOKED. Returns a new record; REVOKED is never valid again."""
    _require_record(record)
    if record.state is not ApprovalState.APPROVED:
        raise ApprovalRejectedError(
            f"only an APPROVED approval can be revoked (state={record.state.value})",
            code=record.state.value,
        )
    return HumanApprovalRecord(
        approval_id=record.approval_id,
        consumption_id=record.consumption_id,
        state=ApprovalState.REVOKED,
        created_at=record.created_at,
        expires_at=record.expires_at,
        approved_at=record.approved_at,
        revoked_at=_now(now).isoformat(),
        consumption_fingerprint=record.consumption_fingerprint,
        proposal_fingerprint=record.proposal_fingerprint,
        decision_fingerprint=record.decision_fingerprint,
        approval_scope=copy.deepcopy(record.approval_scope),
    )


def check_approval(record, *, consumption=None, now=None):
    """
    Effective approval state at `now` (fail-closed query; never mutates).
    Returns (ApprovalState, reason). PENDING / REJECTED / EXPIRED /
    REVOKED are never a valid approval. Binding is re-verified against
    `consumption` when supplied (consumption / proposal / decision / scope
    mismatch -> REJECTED).
    """
    ok, code, reason = _verify_binding(record, consumption)
    if not ok:
        return ApprovalState.REJECTED, f"{reason} ({code})"

    if record.state is ApprovalState.REVOKED:
        return ApprovalState.REVOKED, "approval revoked"
    if record.state is ApprovalState.REJECTED:
        return ApprovalState.REJECTED, "approval rejected"

    try:
        expires = dt.datetime.fromisoformat(record.expires_at)
    except (TypeError, ValueError):
        return ApprovalState.REJECTED, "invalid expires_at (fail-closed)"

    if _now(now) > expires:
        return ApprovalState.EXPIRED, "approval TTL expired"

    return record.state, f"approval state is {record.state.value}"


def is_approved(record, *, consumption=None, now=None):
    """True only when the approval is currently effective (APPROVED, in TTL, bound)."""
    state, _reason = check_approval(record, consumption=consumption, now=now)
    return state is ApprovalState.APPROVED
