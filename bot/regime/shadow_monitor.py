"""Shadow downturn-detection monitor (partner review v2 §13). LOG ONLY — never changes orders.

compute_shadow_signals() assembles the flat §13 signal set (raw pass-through fields + a handful
of derived ratios/distances). shadow_caution_score() maps that signal set to a bounded 0..1
caution score and the hypothetical shadow action the (not-yet-live) defensive engine WOULD have
taken. Both functions are pure, injectable, and NEVER raise -- any missing/None input degrades
that input (and anything derived from it) to None rather than blowing up the caller, since this
whole module runs best-effort inside the live tick loop.
"""

# §13 shadow action ladder, in ascending order of defensiveness. Exported so callers/tests can
# validate a returned action is in-set, and can compare escalation via ACTIONS.index(action).
ACTIONS = ("normal", "half_size", "probe_size", "reduce_bullish", "block_bullish",
          "add_hedge", "activate_bearish")


def _sub(a, b):
    return None if a is None or b is None else (a - b)


def _div(a, b):
    return None if a is None or b is None or b == 0 else round(a / b, 6)


def _or_bounds(opening_range):
    """opening_range may be a dict {"high":..,"low":..}, a (high, low) tuple/list, or None.
    Returns (or_high, or_low), defaulting to (None, None) for anything unrecognized."""
    if opening_range is None:
        return None, None
    if isinstance(opening_range, dict):
        return opening_range.get("high"), opening_range.get("low")
    try:
        return opening_range[0], opening_range[1]
    except Exception:
        return None, None


def compute_shadow_signals(spy, spy_vwap, spy_atr, session_high, session_low, opening_range,
                           qqq_ret, dia_ret, soxx_ret, spy_ret, qqq_vs_vwap, soxx_vs_vwap,
                           vix, vix1d, put_skew, short_delta, short_gamma, short_iv,
                           breadth=None, up_down_vol=None, whale_flow=None):
    """Return a flat dict of the §13 signal set.

    Pass-through fields: spy, spy_vwap, spy_atr, session_high, session_low, opening_range_high/
    low, qqq_ret, dia_ret, soxx_ret, spy_ret, qqq_vs_vwap, soxx_vs_vwap, vix, vix1d, put_skew,
    short_delta, short_gamma, short_iv, breadth, up_down_vol, whale_flow.

    Derived fields:
      dist_from_vwap_atr = (spy - spy_vwap) / spy_atr          (+ve = extended above VWAP)
      dist_from_high_atr = (session_high - spy) / spy_atr      (0 = at the session high)
      vix1d_vix_ratio     = vix1d / vix                         (> 1 = short-dated inversion)
      qqq_minus_dia       = qqq_ret - dia_ret                    (tech vs. a defensive/value proxy)
      soxx_minus_spy      = soxx_ret - spy_ret                   (high-beta semis vs. the market)

    Missing (None) inputs never raise: the pass-through field is None and any derived field that
    depends on it is also None.
    """
    or_high, or_low = _or_bounds(opening_range)
    return {
        "spy": spy, "spy_vwap": spy_vwap, "spy_atr": spy_atr,
        "session_high": session_high, "session_low": session_low,
        "opening_range_high": or_high, "opening_range_low": or_low,
        "qqq_ret": qqq_ret, "dia_ret": dia_ret, "soxx_ret": soxx_ret, "spy_ret": spy_ret,
        "qqq_vs_vwap": qqq_vs_vwap, "soxx_vs_vwap": soxx_vs_vwap,
        "vix": vix, "vix1d": vix1d,
        "put_skew": put_skew, "short_delta": short_delta, "short_gamma": short_gamma,
        "short_iv": short_iv,
        "breadth": breadth, "up_down_vol": up_down_vol, "whale_flow": whale_flow,
        # ── derived ──
        "dist_from_vwap_atr": _div(_sub(spy, spy_vwap), spy_atr),
        "dist_from_high_atr": _div(_sub(session_high, spy), spy_atr),
        "vix1d_vix_ratio": _div(vix1d, vix),
        "qqq_minus_dia": _sub(qqq_ret, dia_ret),
        "soxx_minus_spy": _sub(soxx_ret, spy_ret),
    }


def _clamp01(x):
    return max(0.0, min(1.0, x))


# score -> action thresholds (ascending defensiveness). The last bucket (score >= the final
# threshold) maps to the most defensive label, ACTIONS[-1] ("activate_bearish").
_THRESHOLDS = tuple(zip((0.20, 0.35, 0.50, 0.65, 0.80, 0.90), ACTIONS[:-1]))


def shadow_caution_score(signals):
    """(score, action) from a compute_shadow_signals() dict.

    score is the average of whichever caution components have data (0..1, higher = more
    defensive); components with a None input are simply skipped (never a ZeroDivisionError --
    an empty component list scores 0.0, i.e. "normal"). Weighted, in the partner-spec §13
    best-judgment sense (not backtested/tuned):
      - extension ABOVE vwap (dist_from_vwap_atr > 0)             -> caution as it grows past 0
      - proximity to the session high (small dist_from_high_atr)  -> caution near the top
      - VIX1D/VIX inversion (ratio above ~0.85, capped at 1.20)   -> caution
      - weak/negative cross-asset breadth (QQQ-DIA, SOXX-SPY < 0) -> caution
      - weak breadth (assumed in [0,1], 0.5 = neutral)            -> caution below 0.5

    action is chosen by thresholding score against the §13 action ladder (ACTIONS). Robust to a
    signals dict missing keys entirely (via .get()) -- never raises.
    """
    comps = []

    dva = signals.get("dist_from_vwap_atr")
    if dva is not None:
        comps.append(_clamp01(dva / 1.5))          # >=1.5 ATR above VWAP -> maximally cautious

    dha = signals.get("dist_from_high_atr")
    if dha is not None:
        comps.append(_clamp01(1.0 - dha / 1.0))     # at the high (0 ATR away) -> maximally cautious

    ratio = signals.get("vix1d_vix_ratio")
    if ratio is not None:
        comps.append(_clamp01((ratio - 0.85) / 0.35))   # 0.85 (typical contango) .. 1.20 (inverted)

    qd = signals.get("qqq_minus_dia")
    if qd is not None:
        comps.append(_clamp01(-qd / 0.01))          # -1% relative underperformance -> maximally cautious

    sx = signals.get("soxx_minus_spy")
    if sx is not None:
        comps.append(_clamp01(-sx / 0.01))

    breadth = signals.get("breadth")
    if breadth is not None:
        comps.append(_clamp01((0.5 - breadth) * 2.0))   # breadth in [0,1], 0.5 = neutral

    score = round(sum(comps) / len(comps), 4) if comps else 0.0

    action = ACTIONS[-1]
    for thr, label in _THRESHOLDS:
        if score < thr:
            action = label
            break
    return score, action
