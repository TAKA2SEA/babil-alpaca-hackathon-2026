"""
Alpaca Hackathon 2026 - Paper-only configuration.

Single source of truth for the Paper trading endpoint and market data
endpoint. This module defines constants only - it never reads an
environment variable, never opens a network connection, and never reads a
credential value.

Governance (see ARCHITECTURE.md for the full boundary and rationale):
- This workspace must never reference the Live endpoint or the Live
  credential files/env vars used by the separate BABIL trading system.
- test_static_security.py enforces this mechanically against every .py
  file in this workspace on every run.
"""

# Single source of truth for the Paper trading endpoint.
# No other file in this workspace should hardcode this string literal -
# everything must import it from here.
PAPER_BASE_URL = "https://paper-api.alpaca.markets"

# Independent literal copy, defined only for self-consistency verification
# (a "canary"). If PAPER_BASE_URL is ever edited without this also being
# edited to match, the assert below fires immediately on import - before
# any code that uses PAPER_BASE_URL can run a single request. Callers that
# want a pre-flight check of their own (e.g. before making a network call)
# can compare PAPER_BASE_URL == EXPECTED_PAPER_BASE_URL_CANARY without ever
# needing to hardcode the endpoint string themselves.
EXPECTED_PAPER_BASE_URL_CANARY = "https://paper-api.alpaca.markets"

assert PAPER_BASE_URL == EXPECTED_PAPER_BASE_URL_CANARY, (
    "PAPER_BASE_URL does not match its own expected value - refusing to "
    "load config (fail-closed)."
)

# Market data endpoint (Phase 2/3 quote lookups) - a separate host from the
# trading API. Same single-source-of-truth + canary pattern. This host is
# account-agnostic (same market data regardless of paper/live), reached
# here using Paper credentials, GET-only.
MARKET_DATA_BASE_URL = "https://data.alpaca.markets"
MARKET_DATA_BASE_URL_CANARY = "https://data.alpaca.markets"

assert MARKET_DATA_BASE_URL == MARKET_DATA_BASE_URL_CANARY, (
    "MARKET_DATA_BASE_URL does not match its own expected value - "
    "refusing to load config (fail-closed)."
)

# Names of the ONLY credential environment variables this workspace may
# read. The literal key/secret values themselves are never read, stored,
# or logged by any module in this workspace.
PAPER_KEY_ENV_VAR = "ALPACA_PAPER_KEY_ID"
PAPER_SECRET_ENV_VAR = "ALPACA_PAPER_SECRET_KEY"

# Phase 3 risk-gate thresholds (G1-G4). DRY-RUN parameters only - nothing
# in this workspace submits an order using these values. Verified against
# a real Paper API response (see ARCHITECTURE.md): option contracts return
# both "multiplier" and "size" as equal string values (observed "100" for
# a real SPY contract - never assumed), and the account response carries a
# real integer "options_trading_level" field (observed 3 for this Paper
# account).
DTE_MIN_DAYS = 7
DTE_MAX_DAYS = 45
SPREAD_MAX_PCT = 0.15               # G2: reject if (ask-bid)/mid > 15%
MAX_LOSS_PCT_OF_EQUITY = 0.02       # G3: max total premium risked = 2% of equity
REQUIRED_OPTIONS_TRADING_LEVEL = 3  # G4

# G5 (Phase 7): minimum acceptable max_profit/max_loss ratio for a
# multi-leg spread. Directly motivated by a real Phase 6 DRY-RUN finding
# (a naive lowest-strike SPY bull call spread priced out to
# max_profit=-$341 - an economically dominated, guaranteed-loss trade
# that nothing previously would have rejected).
MIN_RISK_REWARD_RATIO = 0.2
