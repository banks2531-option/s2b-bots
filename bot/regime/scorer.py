"""Map raw RegimeState context -> derived/decision fields using the scoped §6 defaults.
Phase 0: these fields are LOGGED ONLY and never applied to trading. Pure + deterministic so the
thresholds can be re-tuned against the logged regime->outcome dataset before Phase 1 turns them on."""
from dataclasses import replace
from bot.regime.state import RegimeState

# §6 best-judgment thresholds (as fractions of equity / unit-free)
DD_DEGROSS = 0.06          # -6% from peak -> de-gross + pause
DD_KILL = 0.10             # -10% from peak -> kill + flat
VIX_RANK_STRESS = 0.80     # top-20% VIX
TERM_BACKWARDATION = 0.0   # slope < 0 = stress


def _stress_components(s: RegimeState):
    c = []
    if s.vix_pct_rank is not None:
        c.append(min(1.0, max(0.0, (s.vix_pct_rank - 0.5) / 0.5)))   # 0 at median, 1 at top
    if s.vix_term_slope is not None and s.vix_term_slope < TERM_BACKWARDATION:
        c.append(min(1.0, abs(s.vix_term_slope) / 0.10))             # backwardation depth
    if s.trend_bias == "down":
        c.append(1.0)
    if s.flow_extreme:
        c.append(1.0)
    return c


def score(s: RegimeState) -> RegimeState:
    """Return a copy of `s` with derived fields filled per the scoped defaults."""
    comps = _stress_components(s)
    stress = round(sum(comps) / len(comps), 4) if comps else 0.0

    kill = s.equity_drawdown >= DD_KILL
    degross = 1.0 if kill else (0.5 if s.equity_drawdown >= DD_DEGROSS else 0.0)
    # size dial: full size when calm, down to 0.5 at high stress
    size_mult = round(1.0 - 0.5 * stress, 4)
    pause = bool(kill or degross > 0 or stress >= 0.6 or s.flow_extreme)
    widen = round(0.5 * stress, 4)        # add up to +0.5 ATR cushion under stress

    return replace(s,
        stress_score=stress,
        risk_on=not pause,
        size_multiplier=size_mult,
        pause_entries=pause,
        widen_cushion_atr=widen,
        degross_target=degross,
        kill=kill)
