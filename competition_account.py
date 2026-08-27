"""
Stage F - Competition Paper Account validation (READ-ONLY).

Provides a strictly isolated credential namespace for the Hackathon
Competition paper account and a GET-only validation path.

Isolation rules (mechanically enforced by
tests/test_competition_account_isolation.py):
- Competition credentials come ONLY from the environment variables
  ALPACA_COMPETITION_KEY_ID / ALPACA_COMPETITION_SECRET_KEY.
- The Dev paper credential namespace (config.PAPER_KEY_ENV_VAR /
  config.PAPER_SECRET_ENV_VAR, i.e. ALPACA_PAPER_KEY_ID /
  ALPACA_PAPER_SECRET_KEY) is NEVER read here and is NEVER used as a
  fallback - make_competition_trading_client() fails closed instead.
- No .env.paper reuse, no live credential names, no credential VALUES in
  source, no credential values in .mcp.json, .env.competition is
  git-ignored.
- TradingClient is always constructed with paper=True.

This module is READ-ONLY: the only SDK call it makes is
TradingClient.get_account() (a GET). No mutating method (submit_order,
cancel, replace, close, exercise) exists or is reachable here, and there
is no path from this module to execution_engine.py or to any trading MCP
tool.
"""
import os
from datetime import datetime, timezone

from alpaca.trading.client import TradingClient

import config


class CompetitionAccountError(Exception):
    """Raised when the Competition account cannot be configured or validated."""


# The ONLY credential env var names this module reads. Isolated namespace -
# never the Dev paper names from config.
COMPETITION_KEY_ID_ENV = "ALPACA_COMPETITION_KEY_ID"
COMPETITION_SECRET_KEY_ENV = "ALPACA_COMPETITION_SECRET_KEY"


def competition_credentials_configured():
    """
    Presence-only check: True only when both Competition credential env
    vars are set. Never reads, stores, or returns credential values.
    """
    return bool(
        os.environ.get(COMPETITION_KEY_ID_ENV)
        and os.environ.get(COMPETITION_SECRET_KEY_ENV)
    )


def make_competition_trading_client():
    """
    Build a TradingClient for the Competition account using ONLY the
    Competition credential namespace, always paper=True.

    Fail-closed: raises CompetitionAccountError if the Competition
    credentials are not configured. NEVER falls back to Dev credentials
    and always uses paper mode.
    """
    if not competition_credentials_configured():
        raise CompetitionAccountError(
            f"{COMPETITION_KEY_ID_ENV}/{COMPETITION_SECRET_KEY_ENV} not configured - "
            "Competition credentials must be set explicitly (no Dev fallback)."
        )
    return TradingClient(
        os.environ[COMPETITION_KEY_ID_ENV],
        os.environ[COMPETITION_SECRET_KEY_ENV],
        paper=True,
    )


def _masked(value, keep_first=2, keep_last=4):
    """Mask a semi-identifier (account id/number) so it is never echoed in full."""
    s = str(value)
    if len(s) <= keep_first + keep_last:
        return "*" * len(s)
    return s[:keep_first] + "*" * (len(s) - keep_first - keep_last) + s[-keep_last:]


def verify_competition_account():
    """
    READ-ONLY validation of the Competition paper account.

    Calls ONLY TradingClient.get_account() (a GET). Returns a dict of
    non-secret fields needed to confirm account identity, paper
    environment, status, and trading permissions. Never includes API
    keys, secrets, or authorization tokens. Raises CompetitionAccountError
    if credentials are not configured (fail-closed).
    """
    client = make_competition_trading_client()
    account = client.get_account()

    return {
        "status": getattr(account, "status", None),
        "account_id_masked": _masked(getattr(account, "id", "")),
        "account_number_masked": _masked(getattr(account, "account_number", "")),
        "currency": getattr(account, "currency", None),
        "trading_blocked": getattr(account, "trading_blocked", None),
        "account_blocked": getattr(account, "account_blocked", None),
        "pattern_day_trader": getattr(account, "pattern_day_trader", None),
        "shorting_enabled": getattr(account, "shorting_enabled", None),
        "options_trading_level": getattr(account, "options_trading_level", None),
        "options_approved_level": getattr(account, "options_approved_level", None),
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


def _dev_namespace_active():
    """
    True when the Dev paper credential namespace is also set in the
    environment. Informational only: the Competition path never uses it.
    """
    return bool(
        os.environ.get(config.PAPER_KEY_ENV_VAR)
        and os.environ.get(config.PAPER_SECRET_ENV_VAR)
    )
