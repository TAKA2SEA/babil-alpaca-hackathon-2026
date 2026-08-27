# Alpaca Hackathon 2026 — Architecture

Independent workspace. Not part of, and does not import from,
/root/vix_swing_bot (the separate BABIL V2.1 Moomoo trading system, which
is frozen and out of scope for this workspace). Paper trading only,
throughout every phase.

## Governance

- Paper trading only. Live Alpaca endpoint (api.alpaca.markets) must never
  appear in this workspace's code.
- Credential env vars: only ALPACA_PAPER_KEY_ID / ALPACA_PAPER_SECRET_KEY
  are permitted. The Live-side names (ALPACA_API_KEY_ID,
  ALPACA_API_SECRET_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY) and the
  Live credential files (/root/.alpaca_env, /root/.alpaca_keys.env,
  /root/vix_swing_bot/.env) must never be referenced.
- Paper/Live identity is established structurally (dedicated env var names
  + dedicated credential file + single-source-of-truth endpoint constant +
  static tests), not by inspecting the credential value itself - Alpaca
  API keys carry no machine-readable paper/live marker.
- config.py is the single source of truth for PAPER_BASE_URL and
  MARKET_DATA_BASE_URL. Each has an independent literal "canary" copy that
  config.py asserts against itself at import time, so a corrupted value
  fails closed before any network call can happen. No other file may
  hardcode either endpoint string.
- Order-mutating SDK methods (submit_order, cancel_order_by_id,
  cancel_orders, exercise_options_position, close_position,
  close_all_positions, replace_order_by_id) may only appear in
  execution_engine.py - test_static_security.py enforces this on every
  .py file in the workspace, and additionally verifies (per-function, not
  whole-file) that every such call site is textually preceded by its own
  `if mode != "LIVE":` early return, with `mode="DRY_RUN"` as the
  function's own default. This is what lets execution_engine.py safely
  contain real order-submission code while every other module in the
  workspace remains structurally incapable of submitting one.

## Phased history (what was actually built, not the original Phase-0 plan)

- **Phase 0** - repo/venv scaffolding, config.py, static security test
  suite. No network access, no credential reads, no orders.
- **Phase 1** - Paper connectivity (GET /v2/account, GET
  /v2/options/contracts). Confirmed real account fields
  (`options_trading_level`, `equity`) and that a real option contract's
  `multiplier`/`size` fields are numeric **strings** (`"100"`), not ints.
- **Phase 2** - contract_discovery.py: contract discovery, DTE/type/strike
  filters, quote fetching via the real alpaca-py SDK clients (not
  hand-rolled REST calls - verified against SDK source rather than
  guessed).
- **Phase 3** - risk_evaluator.py (G1-G4) + dry_run_engine.py: full
  discovery -> filter -> quote -> gate -> intent pipeline, DRY-RUN only.
- **Phase 4** - execution_engine.py: real single-leg BUY submission +
  fill-confirmation polling (`submit_and_confirm`), and the first real
  Paper order was placed (1 contract, qty forced to 1).
- **Phase 5** - `cancel_and_confirm` + G0 (market clock) gate, added
  directly in response to the Phase 4 order timing out unfilled because
  the market was closed - the real order was then canceled and confirmed.
- **Phase 6** - exit_evaluator.py (TP/SL/DTE-forced-exit) and
  mleg_builder.py (vertical spread construction + risk math). A real
  DRY-RUN run against live SPY data produced a spread with
  `max_profit = -$341` - a genuine, observed economically-dominated
  spread, not a hypothetical one.
- **Phase 7** - G5 (spread economics) gate, directly motivated by the
  Phase 6 finding, plus babil_alpaca_orchestrator.py (integrated
  multi-underlying DRY-RUN report). The first orchestrator run still
  produced negative-max_profit spreads for both SPY and QQQ (G5 correctly
  rejected both) - the spread *selector* itself needed a spot-proximity
  tie-break fix (see below) before it reliably chose sane pairs.

## Options API facts confirmed against the installed alpaca-py SDK

(Source: direct inspection of installed SDK classes/enums/model
validators/docstrings, and real Paper API responses - never assumed.)

- Single-leg option orders use `OrderClass.SIMPLE`.
- Multi-leg option strategies use `OrderClass.MLEG` with `OptionLegRequest`
  legs. Confirmed via the SDK's own `OrderRequest.root_validator` source:
  for `order_class == MLEG`, `symbol`/`side` must be **absent** at the top
  level (each leg carries its own symbol/side instead), `qty` is required
  (spread quantity, not per-leg), and 2-4 legs with unique symbols are
  required.
- `OrderClass.BRACKET` / `OCO` / `OTO` are documented by the SDK itself as
  equity-only order classes and are not used anywhere in this workspace
  for options. Options protection is a separate order/monitoring flow
  (exit_evaluator.py), not a native bracket/OCO/OTO.
- A real option contract's `multiplier` and `size` fields are both
  returned as the numeric **string** `"100"` (confirmed against a real
  SPY contract) - both require `int()` conversion; never hardcoded.
- A real account response carries a real integer `options_trading_level`
  field (observed `3` for this Paper account).
- The contract-discovery endpoint returns contracts ordered by nearest
  expiration first - fetching without server-side
  `expiration_date_gte`/`lte` can exhaust a page entirely on near-term
  expirations before ever reaching a later DTE window (a real, observed
  bug in an early implementation, fixed by pushing the DTE window into
  the query itself).
- Real SDK enum members (e.g. `ContractType.CALL`, `AssetStatus.ACTIVE`)
  stringify via `str()` as `"ContractType.CALL"`, not their value `"call"`
  - a real bug (silently broke type/status filtering, invisible to
    dict-based mocks) found only by running against live data, fixed with
    an explicit `.value`-preferring helper used everywhere a real SDK
    object might flow through string comparison logic.

## Risk gate pipeline (as actually implemented)

| Gate | Purpose | Rejects when |
|---|---|---|
| G0 Market Clock | market must be open | `clock.is_open` is `False` or missing |
| G1 Contract Validity | contract must be tradable | `status != "active"` or `tradable == False` |
| G2 Spread Liquidity | bid/ask must be sane and tight | invalid/non-positive quote, or spread % exceeds `SPREAD_MAX_PCT` (15%) |
| G3 Exposure Sizing | position risk capped by account equity | per-contract risk (`ask * multiplier`) exceeds `MAX_LOSS_PCT_OF_EQUITY` (2%) budget, i.e. `max_qty < 1` |
| G4 Options Level | account must be approved for the strategy | `options_trading_level < REQUIRED_OPTIONS_TRADING_LEVEL` (3) |
| G5 Spread Economics | multi-leg spread must be economically sound | `max_profit <= 0`, an implausible `max_loss <= 0`, or `max_profit/max_loss < MIN_RISK_REWARD_RATIO` (0.2) |

`evaluate_all_gates()` in risk_evaluator.py runs G1-G4 unconditionally;
G0 and G5 are optional (`clock=None` / `spread_risk=None` by default) so
existing single-leg callers/tests are unaffected - callers that care about
real-money-safe behavior (dry_run_engine.py, the orchestrator, the probe
scripts) always pass both.

G5 exists specifically because G0-G4 alone did **not** catch an
economically-dominated multi-leg spread - this was discovered by running
real code against real market data, not anticipated in advance.

## Multi-leg (MLEG) spread selection

`mleg_builder.select_vertical_spread_pair()` scores candidate strike pairs
lexicographically:

1. **Primary**: `abs(strike_width - target_width)` minimized (default
   target width $5).
2. **Secondary (tie-break)**: `abs(long_strike - spot_price)` minimized -
   among equally-width-matched pairs, prefer the one whose long leg sits
   closest to spot.
3. **Hard filter** (not a scoring criterion - failing pairs are excluded
   entirely): the long leg must not be more than 2% deep ITM
   (`long_strike >= spot_price * 0.98` for calls; symmetric for puts).

This three-part design exists because width-matching *alone* (the first
implementation) still selected deep-ITM, economically-dominated pairs for
both SPY and QQQ in a real DRY-RUN run - G5 correctly rejected both, but
the selector itself needed the spot-proximity tie-break and deep-ITM
exclusion added before it reliably produced a healthy (`max_profit > 0`)
spread. Re-run after the fix: both SPY and QQQ produced spreads with
positive max_profit and G1-G5 ALL PASS (only G0 rejected, correctly,
because the market was closed at the time).

## Order lifecycle state machines

Both independently implemented in execution_engine.py (conceptually
inspired by the separate BABIL system's fail-closed polling pattern, no
code shared):

- **Submit**: `[SUBMITTED] -> [POLLING] -> [FILLED] / [FAILED] / [FILL_UNKNOWN]`.
  Polls `get_order_by_id()` every 0.5s, up to 20 attempts (10s ceiling).
  A poll timeout returns `FILL_UNKNOWN`, **never** `FILLED` - the caller
  must never assume a fill happened just because polling stopped. Proven
  against a real order that timed out because the market was closed
  (Phase 4.1).
- **Cancel**: `[CANCEL_SUBMITTED] -> [POLLING] -> [CANCELED] / [CANCEL_FAILED] / [CANCEL_UNKNOWN]`.
  Polls every 0.5s, up to 10 attempts (5s ceiling). Explicitly handles the
  real race condition where an order fills before its cancel takes effect
  (`CANCEL_FAILED`, reason `filled_before_cancel`) rather than silently
  treating it as canceled. Proven against the real Phase 4.1 order
  (`CANCELED` confirmed on the first poll attempt; a follow-up check also
  surfaced and resolved a brief eventual-consistency gap between Alpaca's
  single-order and list-by-status endpoints).

Both functions accept mode="DRY_RUN" (hard default, builds the request
and logs it without any network call) or mode="LIVE" (the only path that
actually submits/cancels).

## Module map

- `config.py` - single source of truth for endpoints, credential env var
  names, and all gate thresholds.
- `contract_discovery.py` - GET-only contract/quote/spot-price fetching
  and filtering.
- `risk_evaluator.py` - G0-G5 pure evaluation logic, no network/SDK
  import.
- `dry_run_engine.py` - single-leg discovery -> gate -> intent pipeline.
- `mleg_builder.py` - vertical spread leg construction, spread selection,
  risk math.
- `execution_engine.py` - the only file allowed to submit/cancel real
  orders; both gated behind `mode="LIVE"`.
- `exit_evaluator.py` - pure TP/SL/DTE-forced-exit decision logic.
- `babil_alpaca_orchestrator.py` - integrated multi-underlying DRY-RUN
  report (terminal + JSON), including exit checks on any real positions.
- `test_static_security.py` - the mechanical enforcement layer for all of
  the above; must pass before any phase's real-data run.
- `tests/` - pytest suite, mock-only (via `unittest.mock.create_autospec`
  against the real installed SDK classes).
- `probe_single_paper_order.py`, `probe_cancel_paper_order.py`,
  `demo_phase6_mleg_and_exit.py` - one-shot scripts that exercised real
  Paper API calls under explicit human authorization each time; not part
  of the automated test suite.
