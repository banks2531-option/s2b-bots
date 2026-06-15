"""Position sizing for the S2b bot (spec §5)."""
import math


def contracts_for_risk(equity: float, max_loss_per_contract: float, risk_pct: float) -> int:
    """Number of spreads to trade: floor(equity*risk_pct / max_loss), min 1."""
    if max_loss_per_contract <= 0:
        raise ValueError("max_loss_per_contract must be > 0")
    budget = equity * risk_pct
    return max(1, math.floor(budget / max_loss_per_contract))


def regime_adjusted_risk_pct(base_pct: float, vix_pct_rank, vix_1d_change) -> float:
    """Halve size when VIX is elevated (pct_rank>0.80) or spiking (>+15% 1-day). Spec §5."""
    if vix_pct_rank is not None and vix_pct_rank > 0.80:
        return base_pct / 2.0
    if vix_1d_change is not None and vix_1d_change > 0.15:
        return base_pct / 2.0
    return base_pct
