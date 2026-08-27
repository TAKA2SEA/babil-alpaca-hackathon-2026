"""
Stage G - BABIL Proposal Bridge (workspace-root BABIL adapter).

Wires a validated AI Proposal through the read-only pipeline:

    Proposal
    -> options_strategy_mapper (strategy -> option_type / builder / width)
    -> read-only market data (MarketAnalyst over ReadOnlyMcpClient)
    -> mleg_builder (select pair, build legs)
    -> compute_spread_risk
    -> risk_evaluator G0-G5
    -> DRY-RUN decision ALLOW / REJECT

The final output is a DRY-RUN decision only. Nothing in this module (or
anything it imports) submits, cancels, replaces, closes, or exercises an
order, and there is no import of execution_engine. The DRY-RUN intent is
a lightweight structure (like dry_run_engine.build_order_intent) that is
only ever inspected - never sent to an order API.

Fail-closed: any unparseable proposal, unmappable strategy, missing
market data, construction failure, or rejected gate produces REJECT with
a reason. A NO_TRADE proposal is always REJECT.
"""
import datetime as dt
import uuid

import config
import contract_discovery as discovery
import mleg_builder as mb
import risk_evaluator as risk

from ai_agent.market_analyst import MarketAnalyst
from ai_agent.options_strategy_mapper import ProposalMappingError, strategy_spec_from_proposal
from ai_agent.proposal import Proposal, ProposalAction, parse_proposal_safe


class ProposalBridgeError(Exception):
    pass


def _value(enum_value):
    if hasattr(enum_value, "value"):
        return str(enum_value.value)
    return str(enum_value)


def _decision(proposal_action, underlying, strategy, reason, gates=None, intent=None, sizing=None):
    return {
        "decision": "ALLOW" if intent is not None else "REJECT",
        "mode": "DRY_RUN",
        "proposal_action": proposal_action,
        "underlying": underlying,
        "strategy": strategy,
        "reason": reason,
        "gates": gates or [],
        "intent": intent,
        "sizing": sizing,
    }


def _build_dry_run_intent(proposal, legs, summary, spread_risk, net_premium):
    return {
        "intent_id": str(uuid.uuid4()),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "DRY_RUN",
        "underlying": proposal.underlying,
        "strategy": _value(proposal.strategy),
        "legs": [
            {
                "symbol": leg.symbol,
                "side": _value(leg.side),
                "ratio_qty": leg.ratio_qty,
                "position_intent": _value(leg.position_intent),
            }
            for leg in legs
        ],
        "strike_width": summary["strike_width"],
        "net_premium": net_premium,
        "max_loss": spread_risk["max_loss"],
        "max_profit": spread_risk["max_profit"],
        "simulation_status": "DRY-RUN / NO ORDER SUBMITTED",
    }


def evaluate_proposal(proposal, analyst):
    """
    Proposal -> read-only data -> mleg_builder -> G0-G5 -> ALLOW/REJECT.

    `proposal` may be a Proposal object or raw dict/JSON string (parsed
    and validated via proposal.parse_proposal_safe, which rejects every AI
    decision input). `analyst` must be a MarketAnalyst (or duck-typed
    equivalent) exposing market_clock/account_summary/option_contracts/
    spot_price/option_quote. GET-only throughout.
    """
    if not isinstance(proposal, Proposal):
        parsed, err = parse_proposal_safe(proposal)
        if parsed is None:
            return _decision(
                "INVALID", None, None, f"proposal rejected at parse: {err}"
            )
        proposal = parsed

    if proposal.action is ProposalAction.NO_TRADE:
        return _decision(
            "NO_TRADE",
            proposal.underlying,
            None,
            "proposal action is NO_TRADE - no trade decision",
        )

    try:
        spec = strategy_spec_from_proposal(proposal)
    except ProposalMappingError as exc:
        return _decision(
            "TRADE",
            proposal.underlying,
            _value(proposal.strategy) if proposal.strategy else None,
            f"strategy cannot be mapped to a spread: {exc}",
        )

    try:
        clock = analyst.market_clock()
        account = analyst.account_summary()
        equity = float(account["equity"])
        options_level = int(account["options_trading_level"])

        today = dt.date.today()
        exp_gte = (today + dt.timedelta(days=config.DTE_MIN_DAYS)).isoformat()
        exp_lte = (today + dt.timedelta(days=config.DTE_MAX_DAYS)).isoformat()
        contracts = analyst.option_contracts(proposal.underlying, spec["option_type"], exp_gte, exp_lte)
        contracts = discovery.filter_by_type(contracts, spec["option_type"])
        contracts = [
            c for c, _dte in discovery.filter_by_dte(contracts, config.DTE_MIN_DAYS, config.DTE_MAX_DAYS, today=today)
        ]
        if not contracts:
            return _decision(
                "TRADE",
                proposal.underlying,
                _value(proposal.strategy),
                f"no {spec['option_type']} contracts within DTE "
                f"[{config.DTE_MIN_DAYS},{config.DTE_MAX_DAYS}] for {proposal.underlying}",
            )
        spot = analyst.spot_price(proposal.underlying)
    except (TypeError, ValueError, KeyError) as exc:
        return _decision(
            "TRADE",
            proposal.underlying,
            _value(proposal.strategy),
            f"read-only market data unavailable: {exc}",
        )

    try:
        long_c, short_c = mb.select_vertical_spread_pair(
            contracts, spot, target_width=spec["target_width"], option_type=spec["option_type"]
        )
        legs, summary = spec["builder"](long_c, short_c)

        long_quote = analyst.option_quote(summary["long_symbol"])
        short_quote = analyst.option_quote(summary["short_symbol"])
        long_bid = float(long_quote["bid_price"])
        long_ask = float(long_quote["ask_price"])
        short_bid = float(short_quote["bid_price"])

        net_premium = max(long_ask - short_bid, 0.0)
        spread_risk = mb.compute_spread_risk(
            net_premium, summary["strike_width"], summary["multiplier"], is_debit=True
        )
    except (mb.SpreadBuilderError, TypeError, ValueError, KeyError) as exc:
        return _decision(
            "TRADE",
            proposal.underlying,
            _value(proposal.strategy),
            f"spread construction failed: {exc}",
        )

    all_passed, gate_results, sizing = risk.evaluate_all_gates(
        long_c,
        long_bid,
        long_ask,
        equity,
        options_level,
        clock=clock,
        spread_risk=spread_risk,
    )
    gates = [{"name": g.name, "passed": g.passed, "reason": g.reason} for g in gate_results]

    if all_passed:
        intent = _build_dry_run_intent(proposal, legs, summary, spread_risk, net_premium)
        return _decision(
            "TRADE",
            proposal.underlying,
            _value(proposal.strategy),
            "all G0-G5 gates passed (DRY-RUN)",
            gates=gates,
            intent=intent,
            sizing=sizing,
        )

    failed = [g["name"] for g in gates if not g["passed"]]
    return _decision(
        "TRADE",
        proposal.underlying,
        _value(proposal.strategy),
        f"gates rejected: {failed}",
        gates=gates,
        sizing=sizing,
    )
