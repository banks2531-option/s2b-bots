"""The single shared market-state object, recomputed each tick. Phase 0 logs it; it does NOT
gate any trading decision yet (decision fields default to no-ops so they are inert if ever read)."""
from dataclasses import dataclass


@dataclass
class RegimeState:
    # ── raw context (None/"neutral" = unknown, so a missing feed is never mistaken for a signal) ──
    vix_level: float = None
    vix_pct_rank: float = None        # vs trailing ~120 trading days
    vix_term_slope: float = None      # 3M/front - 1 ; < 0 = backwardation (front>3M) = stress
    atr_pct: float = None             # ATR(14)/spot
    trend_bias: str = "neutral"       # "up" | "down" | "neutral"
    equity_drawdown: float = 0.0      # fraction below the bot's own equity peak (>= 0)
    concentration: float = 0.0        # 0..1, higher = more clustered book
    flow_bias: str = "neutral"        # UW: "bullish" | "bearish" | "neutral"
    flow_extreme: bool = False        # UW: blow-out flow/skew -> risk-off veto
    # ── derived / decision (computed for LOGGING in Phase 0; NOT applied to trading) ──
    stress_score: float = 0.0         # 0..1 composite
    risk_on: bool = True
    size_multiplier: float = 1.0      # 0..1 -> would scale base_risk_pct in Phase 1
    pause_entries: bool = False
    widen_cushion_atr: float = 0.0
    degross_target: float = 0.0       # 0..1 fraction of book to trim
    kill: bool = False
