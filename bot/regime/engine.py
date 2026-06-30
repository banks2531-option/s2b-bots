"""Assemble a scored RegimeState from already-fetched inputs. Pure (no network) so it is fully
testable; the wiring layer fetches vix/bars/flow and passes them in. Never raises — bad inputs
degrade to a neutral state (Phase 0 must not break the bot)."""
from bot.regime.state import RegimeState
from bot.regime.trend import trend_bias, atr_pct, trend_regime
from bot.regime.concentration import concentration_score
from bot.regime.equity_dd import drawdown_from_peak
from bot.regime.scorer import score


def compute_regime_state(vix, bars, positions, equity, equity_peak, flow) -> RegimeState:
    """vix=(level,pct_rank,term_slope); bars=[{high,low,close}]; positions=[ManagedPosition];
    flow=(flow_bias, flow_extreme). Returns a scored RegimeState."""
    try:
        level, rank, slope = vix
        fbias, fextreme = flow
        raw = RegimeState(
            vix_level=level, vix_pct_rank=rank, vix_term_slope=slope,
            atr_pct=atr_pct(bars), trend_bias=trend_bias(bars), trend_regime=trend_regime(bars),
            equity_drawdown=drawdown_from_peak(equity or 0, equity_peak or 0),
            concentration=concentration_score(positions),
            flow_bias=fbias, flow_extreme=bool(fextreme))
        return score(raw)
    except Exception:
        return RegimeState()        # neutral, inert — never propagate
