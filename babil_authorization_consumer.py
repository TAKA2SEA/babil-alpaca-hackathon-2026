"""
Stage I - Authorization Consumption & Replay Protection (pure layer).

Consumes a Stage H GRANTED AuthorizationRecord exactly once and returns a
new, immutable ConsumptionRecord. This is the END of the pipeline:

    Authorization (GRANTED)
        -> consume()
        -> ConsumptionRecord (consumed=True)
        -> STOP

No order is ever produced or referenced. This module performs no network
access, no API call, no MCP call, and imports nothing beyond the standard
library and babil_authorization (Stage H). It never imports
execution_engine, the Alpaca SDK, competition_account, the orchestrator,
or any MCP/transport module, and it defines no order-mutating or
execution function. There is no function that converts a ConsumptionRecord
into an order.

Consumption guarantees (fail-closed; any failure raises
ConsumptionRejectedError with a machine-readable code):
- record must be a real AuthorizationRecord (malformed -> reject)
- not_executable must be True
- current proposal and current decision must be present
- check_authorization() must report GRANTED (covers DENIED / EXPIRED /
  REVOKED and invalid state -> reject)
- auth_id must not already be in the explicitly-passed consumed set
  (replay -> reject)
- proposal_fingerprint(record.proposal) must equal
  proposal_fingerprint(current_proposal) (Proposal B cannot consume
  Authorization A)
- decision_fingerprint(record.decision) must equal
  decision_fingerprint(current_decision)

The ConsumptionRecord is pure data: it carries the authorization's
identity + state snapshot and the two binding fingerprints, and it is
frozen. It holds no credentials, API keys, secrets, account numbers,
order IDs, broker transaction IDs, quantity/price/strike order values, or
any execution-engine reference.
"""
import copy
import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Literal

from babil_authorization import AuthorizationRecord, AuthorizationState, check_authorization


class ConsumptionRejectedError(Exception):
    """Raised when a consume attempt is rejected (fail-closed)."""

    def __init__(self, reason, code=None):
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class ConsumptionRecord:
    """Immutable receipt that a GRANTED authorization was consumed once."""

    consumption_id: str
    auth_id: str
    consumed_at: str
    authorization_snapshot: dict
    proposal_fingerprint: str
    decision_fingerprint: str
    consumed: Literal[True] = True

    def to_dict(self):
        return {
            "consumption_id": self.consumption_id,
            "auth_id": self.auth_id,
            "consumed_at": self.consumed_at,
            "authorization_snapshot": copy.deepcopy(self.authorization_snapshot),
            "proposal_fingerprint": self.proposal_fingerprint,
            "decision_fingerprint": self.decision_fingerprint,
            "consumed": self.consumed,
        }


def fingerprint(value):
    """
    Canonical deterministic fingerprint (sha256 hex) of a JSON-safe value.
    sort_keys=True makes the digest independent of dict insertion order.
    """
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now(now=None):
    if now is not None:
        if isinstance(now, str):
            return dt.datetime.fromisoformat(now)
        return now
    return dt.datetime.now(dt.timezone.utc)


def _authorization_snapshot(record):
    """
    Redacted audit snapshot of the authorization being consumed: identity +
    state + timestamps + the not_executable marker. Deliberately does NOT
    carry the DRY-RUN decision/intent plan (no quantity/price/strike/order
    values) - binding is captured by the two fingerprints instead.
    """
    return {
        "auth_id": record.auth_id,
        "state": record.state.value,
        "created_at": record.created_at,
        "expires_at": record.expires_at,
        "revoked_at": record.revoked_at,
        "not_executable": record.not_executable,
    }


def consume(
    record,
    current_proposal,
    current_decision,
    *,
    consumed_auth_ids=None,
    now=None,
):
    """
    Consume a GRANTED authorization exactly once (fail-closed).

    `consumed_auth_ids` is the explicit set of auth_ids already consumed
    (replay protection - no global database required). `current_proposal`
    and `current_decision` must match the proposal/decision the
    authorization was bound to (Proposal/Decision binding). Returns a new
    immutable ConsumptionRecord; raises ConsumptionRejectedError on any
    rejection. Never mutates the input record and never creates an order.
    """
    if not isinstance(record, AuthorizationRecord):
        raise ConsumptionRejectedError(
            "malformed authorization record", code="MALFORMED_RECORD"
        )
    if record.not_executable is not True:
        raise ConsumptionRejectedError(
            "authorization is marked executable - refusing", code="NOT_SAFE"
        )
    if not isinstance(current_proposal, dict):
        raise ConsumptionRejectedError(
            "missing current proposal", code="MISSING_PROPOSAL"
        )
    if not isinstance(current_decision, dict):
        raise ConsumptionRejectedError(
            "missing current decision", code="MISSING_DECISION"
        )

    state, reason = check_authorization(record, now=now)
    if state is not AuthorizationState.GRANTED:
        raise ConsumptionRejectedError(
            f"authorization not consumable: {state.value} ({reason})",
            code=state.value,
        )

    consumed = set(consumed_auth_ids or ())
    if record.auth_id in consumed:
        raise ConsumptionRejectedError(
            "authorization already consumed (replay)", code="ALREADY_CONSUMED"
        )

    if fingerprint(record.proposal) != fingerprint(current_proposal):
        raise ConsumptionRejectedError(
            "proposal fingerprint mismatch - authorization is not bound to this proposal",
            code="PROPOSAL_MISMATCH",
        )
    if fingerprint(record.decision) != fingerprint(current_decision):
        raise ConsumptionRejectedError(
            "decision fingerprint mismatch - authorization is not bound to this decision",
            code="DECISION_MISMATCH",
        )

    consumed_at = _now(now)
    return ConsumptionRecord(
        consumption_id=str(uuid.uuid4()),
        auth_id=record.auth_id,
        consumed_at=consumed_at.isoformat(),
        authorization_snapshot=_authorization_snapshot(record),
        proposal_fingerprint=fingerprint(current_proposal),
        decision_fingerprint=fingerprint(current_decision),
    )
