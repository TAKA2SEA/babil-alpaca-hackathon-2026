"""
Stage G - READ-ONLY smoke: Market Analyst -> Proposal -> Bridge -> G0-G5.

Runs the integrated pipeline once against REAL read-only market data from
the Competition paper account:

    CompetitionReadOnlyTransport (GET-only)
      -> ReadOnlyMcpClient (Stage E read-only policy)
      -> MarketAnalyst (read-only market context)
      -> parse_proposal (fixed Stage C schema)
      -> babil_proposal_bridge.evaluate_proposal
      -> DRY-RUN decision ALLOW / REJECT

It never submits, cancels, replaces, or closes an order, never calls a
trading-MCP tool, and never imports execution_engine. The output is a
DRY-RUN decision only. Credentials come from the environment (source
.env.competition into the transient subprocess before running this).

Usage:
    py probe_ai_proposal.py [--underlying SPY] [--strategy bull_call_spread] [--width 5.0]
"""
import argparse
import sys

import competition_account as ca
import competition_market_transport
from babil_proposal_bridge import evaluate_proposal
from ai_agent.market_analyst import MarketAnalyst
from ai_agent.mcp_tool_client import ReadOnlyMcpClient
from ai_agent.options_strategy_mapper import strategy_spec_from_proposal
from ai_agent.proposal import parse_proposal_safe
from babil_proposal_bridge import evaluate_proposal


def main(argv=None):
    parser = argparse.ArgumentParser(description="Stage G read-only smoke (DRY-RUN)")
    parser.add_argument("--underlying", default="SPY")
    parser.add_argument("--strategy", default="bull_call_spread")
    parser.add_argument("--width", type=float, default=5.0)
    args = parser.parse_args(argv)

    if not ca.competition_credentials_configured():
        print("Stage G BLOCKED: competition credentials not configured")
        print(f"set {ca.COMPETITION_KEY_ID_ENV} and {ca.COMPETITION_SECRET_KEY_ENV}")
        return 2

    proposal, err = parse_proposal_safe(
        {
            "action": "TRADE",
            "underlying": args.underlying,
            "strategy": args.strategy,
            "width": args.width,
            "rationale": "Stage G read-only smoke - DRY-RUN only, no order",
        }
    )
    if proposal is None:
        print(f"Stage G BLOCKED: invalid smoke proposal: {err}")
        return 2

    transport = competition_market_transport.CompetitionReadOnlyTransport()
    client = ReadOnlyMcpClient(transport)
    analyst = MarketAnalyst(client)

    spec = strategy_spec_from_proposal(proposal)
    context = analyst.gather_market_context(
        proposal.underlying,
        option_type=spec["option_type"],
        include_news=False,
    )

    decision = evaluate_proposal(proposal, analyst)

    clock = context.get("clock", {})
    print(f"MARKET CONTEXT (READ-ONLY)")
    print(f"  market: {'OPEN' if clock.get('is_open') else 'CLOSED'}")
    print(f"  {proposal.underlying} spot: {context.get('spot_price')}")
    print(f"  {context.get('option_type')} contracts in DTE window: {len(context.get('contracts', []))}")
    print(f"  account options trading level: {context.get('account', {}).get('options_trading_level')}")
    print()
    print(f"PROPOSAL: TRADE {proposal.underlying} {proposal.strategy.value} width={proposal.width}")
    print(f"DECISION: {decision['decision']}")
    print(f"  reason: {decision['reason']}")
    for gate in decision.get("gates", []):
        print(f"    [{'PASS' if gate['passed'] else 'REJECTED'}] {gate['name']}: {gate['reason']}")
    if decision.get("intent"):
        intent = decision["intent"]
        print("  DRY-RUN INTENT (never submitted):")
        print(f"    legs: {intent['legs']}")
        print(f"    max_loss={intent['max_loss']:.2f} max_profit={intent['max_profit']:.2f}")
        print(f"    {intent['simulation_status']}")
    print()
    print("MODE: DRY-RUN ONLY - GET-only smoke, no order submitted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
