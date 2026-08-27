"""
Phase 6 - Exit / position protection evaluator.

Pure evaluation logic. No network call, no Alpaca SDK import, no order
submission anywhere in this file. Takes plain position data and returns
an ExitIntent (CLOSE/HOLD + reason + suggested limit price); nothing here
calls the Alpaca API. Actual closing (if ever exercised) would go through
execution_engine.py, never this file directly.
"""


class ExitIntent:
    def __init__(self, action, reason, suggested_limit_price=None):
        self.action = action  # "CLOSE" or "HOLD"
        self.reason = reason
        self.suggested_limit_price = suggested_limit_price

    def __repr__(self):
        return f"ExitIntent({self.action}, {self.reason!r}, limit={self.suggested_limit_price})"

    def __eq__(self, other):
        if not isinstance(other, ExitIntent):
            return NotImplemented
        return (self.action, self.reason, self.suggested_limit_price) == (
            other.action,
            other.reason,
            other.suggested_limit_price,
        )


def evaluate_exit(
    entry_price,
    current_price,
    dte,
    target_profit_pct=0.50,
    stop_loss_pct=0.30,
    dte_force_exit_days=1,
):
    """
    entry_price / current_price: per-share option premium (not multiplied
    by contract multiplier - this is a %-based decision, so the multiplier
    cancels out of the comparison).

    Priority when multiple conditions are true simultaneously:
    DTE expiration risk > stop loss > take profit. DTE risk is checked
    first because a position about to expire must be closed regardless of
    its current P&L; stop loss is checked before take profit as the more
    safety-critical of the two remaining conditions.
    """
    entry_price = float(entry_price)
    current_price = float(current_price)
    dte = int(dte)

    if entry_price <= 0:
        return ExitIntent(
            "HOLD", f"invalid entry_price={entry_price} - cannot evaluate P&L, holding (fail-safe, not closing blind)"
        )

    pnl_pct = (current_price - entry_price) / entry_price

    if dte <= dte_force_exit_days:
        return ExitIntent(
            "CLOSE",
            f"DTE expiration risk: dte={dte} <= {dte_force_exit_days} day(s) - forced exit regardless of P&L "
            f"(pnl={pnl_pct:+.1%})",
            suggested_limit_price=current_price,
        )

    if pnl_pct <= -stop_loss_pct:
        return ExitIntent(
            "CLOSE",
            f"stop loss triggered: pnl={pnl_pct:+.1%} <= -{stop_loss_pct:.0%}",
            suggested_limit_price=current_price,
        )

    if pnl_pct >= target_profit_pct:
        return ExitIntent(
            "CLOSE",
            f"take profit triggered: pnl={pnl_pct:+.1%} >= +{target_profit_pct:.0%}",
            suggested_limit_price=current_price,
        )

    return ExitIntent("HOLD", f"no exit condition met: pnl={pnl_pct:+.1%}, dte={dte}")
