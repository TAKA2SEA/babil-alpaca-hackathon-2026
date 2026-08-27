# BABIL — Human-in-the-Loop AI Options Trading Agent (Alpaca Paper)

**Alpaca AI Trading Agents Hackathon 2026 submission.** BABIL is a
safety-first, AI-driven options trading agent that runs entirely on the
**Alpaca Paper API**. An AI model proposes only a fixed, five-field
decision; every price, quantity, strike, and premium is recomputed from
**real Alpaca Paper market data** downstream, then gated by a fail-closed
G0-G5 risk pipeline and an immutable
Authorization → Consumption → Human-Approval → Pre-Execution →
Paper-Execution chain behind a **kill switch**. It never touches the Live
Alpaca endpoint and never executes without explicit human confirmation.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design, gate pipeline,
and the real findings (bugs and near-misses) discovered by running this
code against live Paper market data during development.

---

## 1. Project Overview

BABIL keeps the AI at a deliberately tiny decision surface:

- The model may propose only: **action / underlying / strategy / width /
  rationale** (a fixed, validated schema in `ai_agent/proposal.py`).
- The AI is **never allowed to decide** strike, bid/ask, entry price,
  premium, quantity, order ID, account/position size, max loss/profit, or
  expected return. Any proposal containing those fields is **rejected at
  parse time**.
- A **Market Analyst** gathers read-only market context (clock, spot,
  option contracts, quotes, news) through a strictly allowlisted read-only
  MCP client (account / assets / stock-data / options-data / news only).
- The **Proposal Bridge** turns the validated proposal into a real
  vertical-spread structure and evaluates it with the existing G0-G5 gates.
- The **safe-execution core** (Authorization → Consumption → Human
  Approval → Pre-Execution → Paper Execution Request) is a chain of
  immutable, time-bound records that fail closed at every step.

BABIL is an independent workspace: no shared code with the separate BABIL
V2.1 / Moomoo system.

## 2. Why BABIL

- **The AI cannot decide execution inputs.** No LLM output can set a price,
  a quantity, a strike, or a premium. Those are always derived from real
  market data by the existing risk gates, the MLEG builder, and the
  execution boundary.
- **Every order-shaped step is gated.** A single rejection anywhere in
  G0-G5 (or in the authorization chain) means **no order is constructed**.
- **Execution is a firewall, not a convenience.** Paper orders require:
  explicit human approval, an enabled kill switch, a validated
  PaperExecutionRequest, and an injected broker. None of that is wired on
  by default.
- **It is honest about the market.** If no spread passes the gates at the
  current quote snapshot, the agent says `VALID_PROPOSAL_NOT_FOUND` and
  does nothing. That is a feature, not a failure.

BABIL is best described as a **human-in-the-loop AI trading agent** — the
AI proposes, the pipeline validates, and a human approves before any Paper
order can be sent. It is *not* an unattended, fully autonomous trader.

## 3. AI Decision Architecture

```
AI (fixed 5-field Proposal)
   │  action / underlying / strategy / width / rationale
   ▼
Market Analyst  ── read-only MCP allowlist (account, assets,
   │               stock-data, options-data, news)
   ▼
BABIL Proposal Bridge  ── real contracts / spot / quotes (GET-only)
   ▼
G0-G5 risk gates  ── ALLOW / REJECT (fail-closed)
```

- `ai_agent/proposal.py` — fixed schema; rejects any AI decision input.
- `ai_agent/market_analyst.py` — read-only market context provider.
- `ai_agent/mcp_tool_client.py` + `real_mcp_transport.py` — the read-only
  MCP boundary (5 toolsets; the trading toolset is never exposed).
- `babil_proposal_bridge.py` — Proposal → strategy mapping → real
  contracts → `mleg_builder` → `compute_spread_risk` → G0-G5 → ALLOW/REJECT.

## 4. G0-G5 Risk Gate Pipeline

Pure evaluation logic in `risk_evaluator.py` (thresholds in `config.py`):

| Gate | Checks |
|---|---|
| **G0** | Market clock is open (refuses closed-market runs explicitly) |
| **G1** | Contract validity: status=active, tradable=True |
| **G2** | Spread liquidity: (ask-bid)/mid ≤ 15% |
| **G3** | Exposure sizing: max loss ≤ 2% of equity |
| **G4** | Options trading level ≥ 3 |
| **G5** | Spread economics: max_profit / max_loss ≥ 0.20 |

`evaluate_all_gates()` returns `(all_passed, gate_results, sizing)`.
**Any** gate rejection → REJECT → no order path. Thresholds are constants
in `config.py` and are never adjusted to force an ALLOW.

## 5. Dynamic Proposal Discovery

`dynamic_proposal_discovery.py` searches the real options universe at the
**current quote snapshot** for vertical spreads that pass the **existing,
unchanged** G0-G5 gates:

- enumerates bounded near-ATM strike pairs (deep-ITM long legs excluded
  with the same 2% rule the production selector uses),
- evaluates **every** candidate with the unchanged gates and real quotes,
- keeps only ALL-PASS candidates,
- ranks valid candidates with a **fully documented, deterministic rule**
  (risk/reward → lower net premium → lower width → earlier expiration →
  lower long strike → symbol) — no hidden heuristics.

If no candidate passes, the verdict is **`VALID_PROPOSAL_NOT_FOUND`** and
nothing is constructed. The discovery layer never changes a gate, a
threshold, or a quote, and never submits anything.

## 6. Human-in-the-Loop Safety

The safe-execution chain is a series of immutable, time-bound records:

```
ALLOW → Authorization (GRANTED) → Consumption (CONSUMED)
      → Human Approval (APPROVED) → Pre-Execution (READY)
      → PaperExecutionRequest → STOP
```

- `babil_authorization.py` — GRANTED only when the decision is ALLOW and
  the intent exists; TTL-bound; revocable.
- `babil_authorization_consumer.py` — one-time consumption; replay is
  rejected (`ALREADY_CONSUMED`); proposal/decision fingerprints must match.
- `babil_human_approval.py` — **approval requires `explicit_confirmation`
  to be exactly `True`.** `False`, `None`, and AI strings such as
  "approve", "yes", "execute", "confirmed" are **rejected**. An AI identity
  can never be recorded as the approver.
- `babil_pre_execution.py` — READY only when every binding and TTL is
  still valid; `paper_only=True`, `not_executed=True`.
- `babil_paper_execution.py` — builds a `PaperExecutionRequest` from the
  validated intent only; `submit_paper_execution()` is **kill-switched off
  by default** (`execution_enabled=False`) and is the only place a broker
  could be called. In this repository no real broker is injected, so
  `broker.submit` is never invoked.

## 7. Paper-Only Security

- **Paper-only by construction:** every trading client is built with
  `paper=True`; the Live Alpaca endpoint (`api.alpaca.markets`) never
  appears anywhere in the code.
- **Mechanical enforcement:** `test_static_security.py` inspects every
  source file and fails if a Live endpoint, a Live credential path/name, or
  an unguarded order-mutating call site is found. Stage E-O static security
  tests add the read-only MCP allowlist, competition-account isolation, and
  the no-execution-path guarantees.
- **Credentials:** the dedicated Competition Paper account uses an isolated
  namespace (`ALPACA_COMPETITION_KEY_ID` / `ALPACA_COMPETITION_SECRET_KEY`)
  from `.env.competition` (gitignored, never committed). Values are never
  printed or logged.

### A note on the word "LIVE"
In this codebase `mode="LIVE"` (in `execution_engine.py` and the dev probe
scripts) means *actually submitting to the Paper account* — it does **not**
mean the Live Alpaca production endpoint. The Live endpoint itself is
banned from the workspace by the static security tests. Real order
submission in the modern chain additionally requires the explicit
human-approval + kill-switch path described above.

## 8. Real-World Findings

BABIL was developed and validated against **real Alpaca Paper market
data** (GET-only). Notable findings, fixed and re-verified in code:

- **G5 caught a real economically dominated spread.** A naive
  lowest-strike SPY bull-call-spread priced out to a negative max profit
  (net debit ≥ spread width). Nothing previously rejected it; G5
  (risk/reward ≥ 0.20) was added and it has since rejected such spreads in
  live runs — including during this submission preparation.
- **G0 blocks closed-market runs explicitly.** A real run discovered that
  a closed market was only surfacing as a poll timeout; G0 now rejects
  before any work.
- **Deep-ITM selection bug fixed.** Width matching alone picked pairs with
  negative max profit; the deep-ITM long-leg exclusion was added.
- **Honest discovery behavior:** `Dynamic Proposal Discovery` re-evaluates
  real quotes on every run. There are times when **no spread passes G5**
  (the current market can be economically poor). When that happens BABIL
  returns `VALID_PROPOSAL_NOT_FOUND` and does **not** trade. This is not a
  failure — it is the risk gates working.

## 9. Demo

Run these in a shell with the Competition credentials loaded into the
environment (values never printed):

1. **Competition Paper Account verification** — paper=True, ACTIVE, not
   blocked, options level, equity/buying power, positions:
   ```bash
   python3 probe_competition_account.py
   ```
2. **Real market data / AI proposal** — Market Analyst gathers read-only
   data and the fixed-schema proposal is validated:
   ```bash
   python3 probe_ai_proposal.py
   ```
3. **G0-G5 evaluation** — every gate PASS/REJECT with its reason.
4. **Dynamic Proposal Discovery** — current-snapshot search + deterministic
   ranking:
   ```bash
   python3 dynamic_proposal_discovery.py   # or the provided discovery harness
   ```
5. **Authorization → Consumption → Human Approval → Pre-Execution →
   Paper Execution Request chain** — shown as a DRY-RUN; approval stops at
   PENDING until a human explicitly confirms.
6. **Static security tests** — Paper/Live isolation is proven mechanically:
   ```bash
   python3 test_static_security.py
   ```
7. **Full pytest suite** — mock-only, no network:
   ```bash
   .venv/bin/python3 -m pytest tests/ -v
   ```

All demo steps are GET-only / DRY-RUN. No order is submitted.

## 10. Test / Security Evidence

- **Full pytest suite:** 476 passed (mock-only, no network).
- **`test_static_security.py`:** ALL PASS (10/10) — mechanical
  Paper/Live isolation.
- **Stage E-O static security tests:** 52 passed — read-only MCP allowlist,
  competition-account isolation, no-execution-path guarantees.
- **Dynamic Proposal Discovery tests:** 17 passed (all-reject, one-pass,
  multi-pass + deterministic ranking, quote variation, G5 fail, deep-ITM
  exclusion, empty universe, missing quotes, tie-breaks).

## 11. Submission Notes

- **Dedicated Competition Paper Account:** verified GET-only —
  `paper=True`, status `ACTIVE`, not blocked, options trading level 3,
  equity **$100,000.00** (the required starting balance), no positions.
- **Terminology:** use **"human-in-the-loop AI trading agent"**, not
  "fully autonomous". The AI proposes; gates validate; a human approves;
  Paper execution is kill-switched off by default.
- **No live orders were placed** during development of the submission. The
  Paper order path is fully built and tested (DRY-RUN + mock), but actual
  submission requires an explicit human approval plus `execution_enabled`
  plus an injected Paper broker.

---

## Governance

- **Paper trading only.** The Live Alpaca endpoint never appears anywhere
  in this workspace's code - enforced mechanically, not just by policy
  (see "Static security tests").
- Order-mutating calls (`submit_order`, `cancel_order_by_id`, ...) exist
  in exactly one file, `execution_engine.py`, and default to
  `mode="DRY_RUN"`. `mode="LIVE"` (Paper submission; see the note above)
  must be passed explicitly.
- Credentials are read only from gitignored env files
  (`.env.paper` / `.env.competition`); values are never committed.

## Environment setup

```bash
cd <repo root>
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt
.venv/bin/python3 -m pip install -r requirements-dev.txt   # for pytest
```

Create the gitignored credential file yourself:

```
# .env.competition (Competition account - used by competition_account.py)
ALPACA_COMPETITION_KEY_ID=<your competition paper key id>
ALPACA_COMPETITION_SECRET_KEY=<your competition paper secret key>
```

## Running the static security tests

```bash
python3 test_static_security.py
```

Expected: all checks PASS. Verifies mechanically zero Live endpoint
references, zero Live credential references, single-source-of-truth
endpoints, and that every order-mutating call site is gated behind
`mode="DRY_RUN"`/explicit `mode="LIVE"`.

## Running the unit tests

```bash
.venv/bin/python3 -m pytest tests/ -v
```

Mock-only - no network access, using `unittest.mock.create_autospec()`
against the real installed SDK classes.

## Running a DRY-RUN (real market data, GET-only, no orders)

```bash
.venv/bin/python3 dry_run_engine.py SPY            # single-leg
.venv/bin/python3 babil_alpaca_orchestrator.py SPY QQQ   # integrated report
```

## Real order lifecycle (dev probes - deliberate, human-authorized)

These dev scripts can submit/cancel a real order on a Paper account and
default to `mode="DRY_RUN"`. They were run only under explicit, turn-by-turn
human authorization during development:

```bash
.venv/bin/python3 probe_single_paper_order.py    # 1 real Paper contract (mode="LIVE" explicit)
.venv/bin/python3 probe_cancel_paper_order.py    # cancels a known order_id
```

## Layout

- `config.py` — single source of truth for endpoints, credential env var
  names, and gate thresholds.
- `contract_discovery.py` — GET-only contract/quote/spot-price fetching.
- `risk_evaluator.py` — G0-G5 pure evaluation logic.
- `dry_run_engine.py` — single-leg DRY-RUN pipeline.
- `mleg_builder.py` — vertical spread construction + selection.
- `execution_engine.py` — submit/cancel + fill/cancel-confirmation polling
  (the only file that can place a real order).
- `exit_evaluator.py` — take-profit / stop-loss / DTE-forced-exit logic.
- `babil_alpaca_orchestrator.py` — integrated DRY-RUN report.
- `competition_account.py` — isolated Competition Paper account namespace
  (GET-only validation).
- `dynamic_proposal_discovery.py` — current-snapshot G0-G5-passing proposal
  search with deterministic ranking.
- `ai_agent/` — Proposal schema, Market Analyst, read-only MCP client, and
  options-strategy mapping.
- `babil_proposal_bridge.py` — Proposal → G0-G5 → ALLOW/REJECT.
- `babil_authorization.py` / `babil_authorization_consumer.py` /
  `babil_human_approval.py` / `babil_pre_execution.py` /
  `babil_paper_execution.py` — the immutable human-in-the-loop execution
  chain.
- `test_static_security.py` — mechanical Paper/Live isolation enforcement.
- `tests/` — pytest suite.
- `requirements.txt` / `requirements-dev.txt` — pinned dependencies.
