"""Position sizing for the S2b bot (spec §5)."""
import math


def contracts_for_risk(equity: float, max_loss_per_contract: float, risk_pct: float) -> int:
    """Number of spreads to trade: floor(equity*risk_pct / max_loss), min 1."""
    if max_loss_per_contract <= 0:
        raise ValueError("max_loss_per_contract must be > 0")
    budget = equity * risk_pct
    return max(1, math.floor(budget / max_loss_per_contract))
