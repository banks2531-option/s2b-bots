# bot/tests/test_regime_engine.py
from bot.regime.engine import compute_regime_state
from bot.regime.logger import make_regime_logger
import csv


def test_compute_regime_state_assembles_and_scores():
    bars = [{"high": c + 1, "low": c - 1, "close": c} for c in range(100, 160)]
    st = compute_regime_state(
        vix=(18.0, 0.4, 0.05), bars=bars, positions=[], equity=20000, equity_peak=20000,
        flow=("bullish", False))
    assert st.trend_bias == "up" and st.vix_level == 18.0
    assert st.size_multiplier == 1.0 and st.stress_score == 0.0   # scored, calm


def test_backwardation_flows_through_to_stress_end_to_end():
    # Locks deviation #1 end-to-end: a backwardated raw VIX pair (front 22 > 3M 20) must produce a
    # negative term slope AND raise stress through the scorer. Guards against a future maintainer
    # "fixing" term_slope back to front/3M-1 (which would silently zero out this stress signal).
    from bot.regime.vix_term import term_slope
    bars = [{"high": c + 1, "low": c - 1, "close": c} for c in range(160, 100, -1)]  # downtrend
    st = compute_regime_state(
        vix=(22.0, 0.95, term_slope(22.0, 20.0)), bars=bars, positions=[],
        equity=20000, equity_peak=20000, flow=("bearish", False))
    assert st.vix_term_slope < 0          # backwardation registered as negative
    assert st.stress_score > 0.0          # and it propagated into the composite
    assert st.size_multiplier < 1.0


def test_compute_regime_state_never_raises_on_bad_inputs():
    st = compute_regime_state(vix=(None, None, None), bars=[], positions=[],
                              equity=None, equity_peak=None, flow=("neutral", False))
    assert st.trend_bias == "neutral"     # degraded, not crashed


def test_make_regime_logger_writes_row(tmp_path):
    p = tmp_path / "regime.csv"
    log = make_regime_logger(str(p))
    from bot.regime.state import RegimeState
    log(RegimeState(vix_level=18.0), ts="2026-06-29T10:00", event="TICK")
    rows = list(csv.DictReader(open(p)))
    assert rows[0]["vix_level"] == "18.0" and rows[0]["event"] == "TICK"
