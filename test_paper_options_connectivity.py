#!/usr/bin/env python3
"""
Phase 1 - Paper connectivity + Options contract discovery (GET-only).

Strict GET-only: no submit_order, cancel_order, exercise_options_position,
or any other order-mutating call exists anywhere in this file. Uses only
config.PAPER_BASE_URL, with a fail-closed assertion that refuses to run
against anything other than the Paper endpoint - there is no Live
endpoint fallback anywhere in this file.
"""
import json
import os
import sys

import requests

from config import (
    EXPECTED_PAPER_BASE_URL_CANARY,
    PAPER_BASE_URL,
    PAPER_KEY_ENV_VAR,
    PAPER_SECRET_ENV_VAR,
)

# Fail-closed Paper identity check. config.py already asserts this
# internally at import time; this is an independent second check (no
# endpoint string hardcoded here at all) so this script refuses to run
# even if that internal assert were ever removed.
if PAPER_BASE_URL != EXPECTED_PAPER_BASE_URL_CANARY:
    print(f"BLOCKED: PAPER_BASE_URL={PAPER_BASE_URL!r} does not match the expected Paper "
          "endpoint canary. Refusing to connect (fail-closed).")
    sys.exit(1)

PAPER_KEY = os.environ.get(PAPER_KEY_ENV_VAR)
PAPER_SECRET = os.environ.get(PAPER_SECRET_ENV_VAR)

if not PAPER_KEY or not PAPER_SECRET:
    if os.path.exists(".env.paper"):
        with open(".env.paper") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    if k == PAPER_KEY_ENV_VAR:
                        PAPER_KEY = v.strip("'\"")
                    if k == PAPER_SECRET_ENV_VAR:
                        PAPER_SECRET = v.strip("'\"")

if not PAPER_KEY or not PAPER_SECRET:
    print(f"[SETUP REQUIRED] {PAPER_KEY_ENV_VAR} or {PAPER_SECRET_ENV_VAR} is missing.")
    print("Please set up /root/alpaca_hackathon_2026/.env.paper")
    sys.exit(1)

# Never printed, logged, or included in any report - used only to build the
# request headers for this process's own outgoing requests.
headers = {
    "APCA-API-KEY-ID": PAPER_KEY,
    "APCA-API-SECRET-KEY": PAPER_SECRET,
}

print(f"Connecting to: {PAPER_BASE_URL} (Paper endpoint, verified above)")
print()

print("=== 1. ALPACA HACKATHON PAPER ACCOUNT (GET /v2/account) ===")
resp = requests.get(f"{PAPER_BASE_URL}/v2/account", headers=headers)
print(f"HTTP status  : {resp.status_code}")
if resp.status_code != 200:
    print(f"Account GET failed: {resp.text}")
    sys.exit(1)

acc = resp.json()
print(f"Account ID   : {acc.get('id')}")
print(f"Status       : {acc.get('status')}")
print(f"Equity       : ${acc.get('equity')}")
print(f"Cash         : ${acc.get('cash')}")
print(f"Buying Power : ${acc.get('buying_power')}")

print()
print("=== 2. OPTIONS CONTRACT DISCOVERY (GET /v2/options/contracts, SPY) ===")
params = {"underlying_symbols": "SPY", "status": "active", "limit": 3}
opt_resp = requests.get(f"{PAPER_BASE_URL}/v2/options/contracts", headers=headers, params=params)
print(f"HTTP status  : {opt_resp.status_code}")
if opt_resp.status_code != 200:
    print(f"Options contracts GET failed: {opt_resp.text}")
    sys.exit(1)

contracts = opt_resp.json().get("option_contracts", [])
print(f"Contracts returned: {len(contracts)}")
for c in contracts:
    print(f"  {c.get('symbol')} | {str(c.get('type', '')).upper()} | strike={c.get('strike_price')} "
          f"| exp={c.get('expiration_date')} | tradable={c.get('tradable')} "
          f"| underlying={c.get('underlying_symbol')}")

print()
print("=== 3. RAW FIELD DUMP OF FIRST CONTRACT (multiplier/size evidence - no assumption made) ===")
if contracts:
    print(json.dumps(contracts[0], indent=2))
    if "size" in contracts[0]:
        print()
        print(f"NOTE: raw 'size' field value = {contracts[0]['size']!r}. "
              "This script does NOT assume this means multiplier=100 - "
              "report it as-observed only.")
    else:
        print()
        print("NOTE: no 'size' field present in the raw response for this contract.")
else:
    print("No contracts returned - cannot inspect multiplier/size field.")
