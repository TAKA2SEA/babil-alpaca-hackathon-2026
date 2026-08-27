import os, json, requests

from config import PAPER_BASE_URL, MARKET_DATA_BASE_URL

env_path = ".env.paper"
if not os.path.exists(env_path):
    print(f"❌ {env_path} not found.")
    exit(1)

env_vars = {}
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env_vars[k.strip()] = v.strip().strip('"').strip("'")

key = env_vars.get('ALPACA_PAPER_KEY_ID')
secret = env_vars.get('ALPACA_PAPER_SECRET_KEY')
base_url = PAPER_BASE_URL
headers = {'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret}

print("==================================================")
print("🔬 ALPACA PAPER OPTIONS PROBE (READ-ONLY)")
print("==================================================")

# G1 & G2: Account & Options Configurations
acc_res = requests.get(f"{base_url}/v2/account", headers=headers)
cfg_res = requests.get(f"{base_url}/v2/account/configurations", headers=headers)

if acc_res.status_code == 200:
    acc = acc_res.json()
    cfg = cfg_res.json() if cfg_res.status_code == 200 else {}
    print(f"\n[G1: ACCOUNT STATUS]")
    print(f"  Status        : {acc.get('status')}")
    print(f"  Equity        : ${acc.get('equity')}")
    print(f"  Cash          : ${acc.get('cash')}")
    print(f"  Buying Power  : ${acc.get('buying_power')}")

    print(f"\n[G2: OPTIONS CONFIGURATION]")
    print(f"  Options Level : {acc.get('options_trading_level', 'N/A')}")
    print(f"  Approved Level: {acc.get('options_approved_level', 'N/A')}")
    print(f"  Max Level Cfg : {cfg.get('max_options_trading_level', 'N/A')}")
else:
    print(f"❌ Account Query Failed [{acc_res.status_code}]: {acc_res.text}")
    exit(1)

# G3: /v2/options/contracts 生データ検証
opt_res = requests.get(f"{base_url}/v2/options/contracts", headers=headers, params={'underlying_symbols': 'SPY', 'status': 'active', 'limit': 1})
if opt_res.status_code == 200:
    contracts = opt_res.json().get('option_contracts', [])
    if contracts:
        c = contracts[0]
        sym = c.get('symbol')
        print(f"\n[G3: RAW OPTION CONTRACT EVIDENCE]")
        print(f"  Symbol        : {sym}")
        print(f"  Type          : {c.get('type')}")
        print(f"  Strike        : ${c.get('strike_price')}")
        print(f"  Expiration    : {c.get('expiration_date')}")
        print(f"  Raw 'size'    : {c.get('size')} (Type: {type(c.get('size')).__name__})")
        print(f"  Raw Keys Found: {list(c.keys())}")

        # G4: Market Data (Latest Quote) 照会
        data_res = requests.get(f"{MARKET_DATA_BASE_URL}/v1beta1/options/quotes/latest", headers=headers, params={'symbols': sym, 'feed': 'indicative'})
        print(f"\n[G4: OPTIONS MARKET DATA PROBE]")
        print(f"  Data API Status: {data_res.status_code}")
        if data_res.status_code == 200:
            quotes = data_res.json().get('quotes', {})
            q = quotes.get(sym, {})
            print(f"  Latest Bid/Ask : Bid=${q.get('bp')} (Sz:{q.get('bs')}) / Ask=${q.get('ap')} (Sz:{q.get('as')})")
        else:
            print(f"  Data API Resp  : {data_res.text}")
    else:
        print("\n[G3: RAW OPTION CONTRACT] No contracts returned.")
else:
    print(f"\n❌ Options Contracts Query Failed [{opt_res.status_code}]: {opt_res.text}")

print("\n==================================================")
print("Probe Completed (HTTP POST/DELETE: 0 calls made)")
print("==================================================")
