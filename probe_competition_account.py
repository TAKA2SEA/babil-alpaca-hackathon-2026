"""
Stage F - READ-ONLY probe for the Competition Paper account.

GET-only: constructs a TradingClient with paper=True using ONLY the
isolated Competition credential namespace and calls TradingClient
get_account() (a GET). It never submits, cancels, replaces, closes, or
exercises anything.

If Competition credentials are not configured it exits with a clear
"BLOCKED" message and a non-zero code - it never fakes a connection.

Usage:
    py probe_competition_account.py
"""
import sys

import competition_account as ca


def main():
    if not ca.competition_credentials_configured():
        print("Stage F BLOCKED: Competition credentials not configured")
        print(f"set {ca.COMPETITION_KEY_ID_ENV} and {ca.COMPETITION_SECRET_KEY_ENV}")
        print("(values come from environment variables only; no Dev fallback)")
        return 2

    if ca._dev_namespace_active():
        print("note: Dev paper credential namespace is also present; it is NOT used here")

    try:
        info = ca.verify_competition_account()
    except ca.CompetitionAccountError as exc:
        print(f"Stage F BLOCKED: {exc}")
        return 2

    for key, value in info.items():
        print(f"{key}: {value}")

    print()
    print("READ-ONLY probe complete (get_account only - no mutating API called)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
