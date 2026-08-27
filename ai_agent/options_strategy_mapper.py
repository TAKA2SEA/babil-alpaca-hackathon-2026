"""
Stage C - Proposal to MLEG mapper.

Converts a validated ai_agent Proposal into the exact inputs the existing
mleg_builder functions consume. This is the only place that knows how a
Proposal strategy/width becomes an mleg_builder call.

The mapping is structural and thin:

    Proposal.action  -> TRADE (NO_TRADE has no mleg spec)
    Proposal.strategy-> vertical spread builder + option_type
    Proposal.width   -> select_vertical_spread_pair target_width (an
                        approximate spread-width *target*, not a price)

No price, quantity, P/L, order, or sizing number is ever read from or
forwarded by this module. The builder/selection functions it delegates to
(mleg_builder) are pure construction over real contract/quote data, and
the downstream risk gates recompute every decision input from market
data. This module never imports the execution engine, never touches an
Alpaca trading client, and contains no order-mutating code.
"""
import mleg_builder as mb

from ai_agent.proposal import OptionsStrategy, Proposal, ProposalAction


class ProposalMappingError(Exception):
    """Raised when a Proposal cannot be mapped to an mleg_builder call."""


# strategy -> option_type expected by mleg_builder.select_vertical_spread_pair
STRATEGY_TO_OPTION_TYPE = {
    OptionsStrategy.BULL_CALL_SPREAD: "call",
    OptionsStrategy.BEAR_PUT_SPREAD: "put",
}

# strategy -> the mleg_builder vertical spread builder that consumes a
# selected (long, short) pair. Extend in lockstep with OptionsStrategy.
STRATEGY_TO_BUILDER = {
    OptionsStrategy.BULL_CALL_SPREAD: mb.build_vertical_call_spread,
    OptionsStrategy.BEAR_PUT_SPREAD: mb.build_vertical_put_spread,
}


def strategy_spec_from_proposal(proposal):
    """
    Pure mapping: Proposal -> mleg_builder parameters.

    Returns {"strategy", "option_type", "target_width", "builder"}. The
    builder is the callable from mleg_builder to invoke with the selected
    (long, short) pair. Raises ProposalMappingError for NO_TRADE or any
    strategy without an mleg mapping.
    """
    if not isinstance(proposal.action, ProposalAction):
        raise ProposalMappingError(f"proposal has no valid action: {proposal.action!r}")
    if proposal.action is not ProposalAction.TRADE:
        raise ProposalMappingError(
            f"cannot map a {proposal.action.value} proposal to a spread; only TRADE proposals "
            "produce an mleg spec"
        )
    if proposal.strategy not in STRATEGY_TO_OPTION_TYPE:
        raise ProposalMappingError(f"no mleg mapping for strategy {proposal.strategy!r}")
    if proposal.strategy not in STRATEGY_TO_BUILDER:
        raise ProposalMappingError(f"no mleg builder for strategy {proposal.strategy!r}")

    return {
        "strategy": proposal.strategy.value,
        "option_type": STRATEGY_TO_OPTION_TYPE[proposal.strategy],
        "target_width": proposal.width,
        "builder": STRATEGY_TO_BUILDER[proposal.strategy],
    }


def select_vertical_pair_from_proposal(proposal, contracts, spot_price, max_itm_pct=0.02):
    """
    Select the (long, short) contract pair for this proposal's strategy
    and width using mleg_builder.select_vertical_spread_pair.
    `contracts` must already be the DTE/type-filtered candidate list from
    real market data (e.g. contract_discovery output); `spot_price` is the
    real underlying spot. No AI-supplied number is used.
    """
    spec = strategy_spec_from_proposal(proposal)
    return mb.select_vertical_spread_pair(
        contracts,
        spot_price,
        target_width=spec["target_width"],
        option_type=spec["option_type"],
        max_itm_pct=max_itm_pct,
    )


def build_vertical_spread_from_proposal(proposal, long_contract, short_contract, ratio_qty=1):
    """
    Build the (legs, summary) for this proposal from an already-selected
    (long, short) pair by delegating to the mapped mleg_builder function.
    The pair must come from real market data (see
    select_vertical_pair_from_proposal); no AI price/quantity is used.
    """
    spec = strategy_spec_from_proposal(proposal)
    return spec["builder"](long_contract, short_contract, ratio_qty=ratio_qty)


def build_spread_from_proposal(proposal, contracts, spot_price, ratio_qty=1, max_itm_pct=0.02):
    """
    One-call convenience: select the pair from real contract data, then
    build the legs + summary. Returns (legs, summary) exactly as the
    mleg_builder builders do. NET premium / quotes are deliberately NOT
    computed here - they must come from live quotes downstream so G0-G5
    and execution sizing use only real market data.
    """
    long_contract, short_contract = select_vertical_pair_from_proposal(
        proposal, contracts, spot_price, max_itm_pct=max_itm_pct
    )
    return build_vertical_spread_from_proposal(proposal, long_contract, short_contract, ratio_qty=ratio_qty)
