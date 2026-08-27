# Alpaca Hackathon 2026

Paper-only Options trading workspace: discovery, risk gates (G0-G5),
single-leg and multi-leg (MLEG) order construction, a real
submit/poll/cancel execution engine, an exit evaluator, and an integrated
DRY-RUN orchestrator. Independent from /root/vix_swing_bot (BABIL V2.1 /
Moomoo) - no shared code.

See ARCHITECTURE.md for the full design, gate pipeline, and the real
findings (bugs and near-misses) discovered by running this code against
live Paper market data across development.

## Governance

- **Paper trading only.** The Live Alpaca endpoint never appears anywhere
  in this workspace's code - enforced mechanically, not just by policy
  (see "Static security tests" below).
- Order-mutating calls (`submit_order`, `cancel_order_by_id`, ...) exist
  in exactly one file, `execution_engine.py`, and default to
  `mode="DRY_RUN"` - `mode="LIVE"` must be passed explicitly to submit or
  cancel a real order.
- Credentials: only `ALPACA_PAPER_KEY_ID` / `ALPACA_PAPER_SECRET_KEY` are
  ever read, from `.env.paper` (root-only permissions, gitignored, never
  committed).

## Environment setup

```bash
cd /root/alpaca_hackathon_2026
python3 -m venv .venv                       # if not already created
.venv/bin/python3 -m pip install -r requirements.txt
.venv/bin/python3 -m pip install -r requirements-dev.txt   # for pytest
```

Create `.env.paper` yourself (this file is never created or read by
anyone but you supplying the values):

```
ALPACA_PAPER_KEY_ID=<your paper key id>
ALPACA_PAPER_SECRET_KEY=<your paper secret key>
```

```bash
chmod 600 .env.paper
```

## Running the static security tests

```bash
python3 test_static_security.py
```

Expected: all checks PASS. Verifies (mechanically, via source-text
inspection - not by trusting anyone's memory) zero Live endpoint
references, zero Live credential references, single-source-of-truth
endpoints, and that every order-mutating call site in
`execution_engine.py` is gated behind `mode="LIVE"`.

## Running the unit tests

```bash
.venv/bin/python3 -m pytest tests/ -v
```

Mock-only - no network access, using `unittest.mock.create_autospec()`
against the real installed SDK classes so tests assert against the actual
method surface, not a hand-rolled double.

## Running a DRY-RUN (real market data, GET-only, no orders)

Single underlying, single-leg discovery:

```bash
.venv/bin/python3 dry_run_engine.py SPY
```

Integrated multi-underlying report (single-leg + vertical spread + exit
checks + JSON telemetry):

```bash
.venv/bin/python3 babil_alpaca_orchestrator.py SPY QQQ
```

## Real order lifecycle (mode="LIVE" - use deliberately, not casually)

These scripts submit/cancel a **real** order on your Paper account. Each
one was run only under explicit, turn-by-turn human authorization during
development - treat them the same way:

```bash
.venv/bin/python3 probe_single_paper_order.py    # submits 1 real Paper contract
.venv/bin/python3 probe_cancel_paper_order.py    # cancels a specific known order_id
```

## Layout

- `config.py` - single source of truth for endpoints, credential env var
  names, and gate thresholds.
- `contract_discovery.py` - GET-only contract/quote/spot-price fetching.
- `risk_evaluator.py` - G0-G5 pure evaluation logic.
- `dry_run_engine.py` - single-leg DRY-RUN pipeline.
- `mleg_builder.py` - vertical spread construction + selection.
- `execution_engine.py` - submit/cancel + fill/cancel-confirmation
  polling (the only file that can place a real order).
- `exit_evaluator.py` - take-profit / stop-loss / DTE-forced-exit logic.
- `babil_alpaca_orchestrator.py` - integrated DRY-RUN report.
- `test_static_security.py` - mechanical Paper/Live isolation enforcement.
- `tests/` - pytest suite.
- `requirements.txt` / `requirements-dev.txt` - pinned runtime / test
  dependencies, installed only into this workspace's own `.venv`.
