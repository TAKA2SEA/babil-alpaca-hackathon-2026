"""
Stage H - Execution Authorization Layer (BABIL adapter).

Converts a BABIL Proposal Bridge ALLOW/REJECT decision into an explicit,
immutable, time-bound AuthorizationRecord. The record is PURE DATA: it
never carries credentials, never represents an order, and is never
connected to any execution path.

Guiding invariants (ALLOW != EXECUTE, ALLOW != ORDER AUTHORIZATION):
- A GRANTED record is a pure authorization state, NOT permission to place
  an order and NOT an executable order.
- Fail-closed: only an ALLOW decision with mode="DRY_RUN" and a non-empty
  DRY-RUN intent can become GRANTED. A REJECT decision, an ALLOW without
  an intent, a non-dict decision, or any invalid input becomes DENIED.
  TTL expiry is reported as EXPIRED, explicit revocation produces a
  REVOKED record, and any invalid state resolves to DENIED.
- The record's `state` is fixed at creation; the effective state is a pure
  function of the record plus time (check_authorization). Revocation
  returns a NEW record - records themselves are immutable.

Security boundary:
- This module imports no execution_engine, no Alpaca SDK, no trading MCP,
  and no MCP transport. It performs no order API call and builds no LIVE
  execution path. No function here can derive an order from a record.

Standard library only. Pure - no network, no SDK, no credentials.
"""
import copy
import datetime as dt
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Literal

DEFAULT_AUTHORIZATION_TTL_SECONDS = 300


class AuthorizationState(Enum):
    DENIED = "DENIED"
    GRANTED = "GRANTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


@dataclass(frozen=True)
class AuthorizationRecord:
    """
    Immutable, time-bound authorization record.

    decision / proposal / gates / intent are deep snapshots taken at
    creation. The record carries no credentials, API keys, tokens, or
    account number, and `not_executable` is always True - this is pure
    authorization data, never an order.
    """

    auth_id: str
    state: AuthorizationState
    created_at: str
    expires_at: str
    revoked_at: str | None
    decision: dict
    proposal: dict
    gates: tuple
    intent: dict | None
    not_executable: Literal[True] = True

    def to_dict(self):
        return {
            "auth_id": self.auth_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "decision": copy.deepcopy(self.decision),
            "proposal": copy.deepcopy(self.proposal),
            "gates": copy.deepcopy(list(self.gates)),
            "intent": copy.deepcopy(self.intent),
            "not_executable": self.not_executable,
        }


def _now(now=None):
    if now is not None:
        if isinstance(now, str):
            return dt.datetime.fromisoformat(now)
        return now
    return dt.datetime.now(dt.timezone.utc)


def _snapshot(value):
    return copy.deepcopy(value)


def _denied_record(auth_id, created, decision_snap, proposal_snap):
    created_iso = created.isoformat()
    return AuthorizationRecord(
        auth_id=auth_id,
        state=AuthorizationState.DENIED,
        created_at=created_iso,
        expires_at=created_iso,
        revoked_at=None,
        decision=decision_snap,
        proposal=proposal_snap,
        gates=(),
        intent=None,
    )


def authorize_decision(
    decision,
    proposal=None,
    *,
    ttl_seconds=DEFAULT_AUTHORIZATION_TTL_SECONDS,
    now=None,
):
    """
    Turn a Bridge decision into an AuthorizationRecord (fail-closed).

    GRANTED only when: decision is a dict, decision["decision"] == "ALLOW",
    decision["mode"] == "DRY_RUN", and decision["intent"] is a non-None
    dict. Everything else becomes DENIED. proposal (optional) is the
    validated Proposal.to_dict() snapshot attached for auditability.
    """
    created = _now(now)
    auth_id = str(uuid.uuid4())
    decision_snap = _snapshot(decision) if isinstance(decision, dict) else {}
    proposal_snap = _snapshot(proposal) if isinstance(proposal, dict) else {}

    if not isinstance(decision, dict):
        return _denied_record(auth_id, created, {}, proposal_snap)
    if decision.get("decision") != "ALLOW":
        return _denied_record(auth_id, created, decision_snap, proposal_snap)
    if decision.get("mode") != "DRY_RUN":
        return _denied_record(auth_id, created, decision_snap, proposal_snap)
    intent_snap = _snapshot(decision.get("intent"))
    if not isinstance(intent_snap, dict):
        return _denied_record(auth_id, created, decision_snap, proposal_snap)

    gates_snap = tuple(_snapshot(g) for g in (decision.get("gates") or []))
    expires = created + dt.timedelta(seconds=int(ttl_seconds))

    return AuthorizationRecord(
        auth_id=auth_id,
        state=AuthorizationState.GRANTED,
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        revoked_at=None,
        decision=decision_snap,
        proposal=proposal_snap,
        gates=gates_snap,
        intent=intent_snap,
    )


def check_authorization(record, now=None):
    """
    Effective state of a record at `now` (fail-closed). Returns
    (AuthorizationState, reason). Never mutates the record.
    """
    if not isinstance(record, AuthorizationRecord):
        return AuthorizationState.DENIED, "invalid authorization record"
    if record.state is AuthorizationState.REVOKED:
        return AuthorizationState.REVOKED, "authorization revoked"
    if record.state is not AuthorizationState.GRANTED:
        return record.state, f"authorization state is {record.state.value}"
    try:
        expires = dt.datetime.fromisoformat(record.expires_at)
    except (TypeError, ValueError):
        return AuthorizationState.DENIED, "invalid expires_at (fail-closed)"
    if _now(now) > expires:
        return AuthorizationState.EXPIRED, "authorization TTL expired"
    return AuthorizationState.GRANTED, "authorization valid"


def revoke_authorization(record, now=None):
    """
    Return a NEW REVOKED record (immutable - the input is untouched).
    Revoking an already-revoked record is a no-op.
    """
    if not isinstance(record, AuthorizationRecord):
        raise TypeError("revoke_authorization requires an AuthorizationRecord")
    if record.state is AuthorizationState.REVOKED:
        return record
    return AuthorizationRecord(
        auth_id=record.auth_id,
        state=AuthorizationState.REVOKED,
        created_at=record.created_at,
        expires_at=record.expires_at,
        revoked_at=_now(now).isoformat(),
        decision=record.decision,
        proposal=record.proposal,
        gates=record.gates,
        intent=record.intent,
    )


def is_granted(record, now=None):
    """Convenience: True only when the record is currently effective."""
    return check_authorization(record, now=now)[0] is AuthorizationState.GRANTED
