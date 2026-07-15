"""Gap-risk stress test (partner review v2 §10): reprice spreads at SPY down 1/1.5/2 ATR.

Priority-0 fix item 5: the loss estimate now supports a Black-Scholes shock-grid reprice
(`stressed_spread_loss_bs`) in addition to the original intrinsic-value-only estimate
(`stressed_spread_loss`, kept unchanged for back-compat/comparison). The intrinsic model IGNORED
time value, IV expansion, skew, and gamma, so it materially UNDERSTATED the stressed loss (a spread
sitting just OTM after the shock could still read as "profitable"). The BS model reprices each leg
across an IV-shock x skew grid at the shocked spot and takes the WORST cell.

This module stays PURE (no network / no I/O). All IV sourcing lives in the `iv_fn` injected by the
orchestrator; DTE is derived from each position's `.expiry` vs the caller-supplied `today`."""
from datetime import datetime

from bot.portfolio.bs import bs_put


def stressed_spread_loss(short_strike, long_strike, credit, qty, spot, atr, drop_atr, wing_width=10.0):
    """Loss ($, positive = loss) on a bull put spread if SPY drops `drop_atr` ATRs, using a
    conservative intrinsic-value close estimate: stressed debit-to-close =
    min(wing_width, max(0, short-S) - max(0, long-S)) at S = spot - drop_atr*atr.
    Loss = (stressed_debit - credit) * 100 * qty  (negative means the position is still profitable)."""
    s = spot - drop_atr * atr
    stressed_debit = min(wing_width, max(0.0, short_strike - s) - max(0.0, long_strike - s))
    return (stressed_debit - credit) * 100.0 * qty


def stressed_spread_loss_bs(short_strike, long_strike, credit, qty, spot, atr, drop_atr,
                            dte, short_iv, long_iv, f, wing_width=10.0):
    """Black-Scholes shock-grid loss ($, positive = loss) on a bull put spread if SPY drops
    `drop_atr` ATRs. The spread's stressed debit-to-close is repriced at the shocked spot across an
    IV-shock x skew grid (short leg bumped extra on the stressed-skew cells) plus a bid/ask widening
    haircut, and the WORST (largest) resulting debit is taken -- then clamped to [0, wing_width].
    Loss = (worst_debit - credit) * 100 * qty."""
    T = max(dte, 0) / 365.0
    S = spot - drop_atr * atr
    worst = 0.0
    for iv_bump in f.gap_iv_shocks:
        for skew_extra in (0.0, f.gap_skew_bump):      # worst-case: bump the SHORT leg to maximize debit
            sp = bs_put(S, short_strike, T, short_iv + iv_bump + skew_extra, f.risk_free_rate)
            lp = bs_put(S, long_strike,  T, long_iv  + iv_bump,             f.risk_free_rate)
            mid = sp - lp
            nat = mid * (1.0 + f.gap_stress_widen)      # stressed-natural close (nat >= mid, since
            worst = max(worst, nat)                     # short>long puts -> mid>=0 and widen>0)
    worst = min(wing_width, max(0.0, worst))
    return (worst - credit) * 100.0 * qty


def _dte(expiry, today):
    """Calendar days from `today` to `expiry` (both "YYYY-MM-DD"). 0 on any parse failure/missing."""
    try:
        return (datetime.strptime(expiry, "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days
    except Exception:
        return 0


def gap_stress_losses(positions, spot, atr, wing_width=10.0, today=None, iv_fn=None, f=None):
    """positions: iterable of objects with .short_strike/.long_strike/.credit/.qty (open + proposed).
    Returns {1.0: total_loss, 1.5: total_loss, 2.0: total_loss} summed across all positions.

    Priority-0 fix item 5 (opt-in, backward-compatible): when `f.gap_stress_model == "bs"` AND both
    `iv_fn` and `today` are supplied, each position is repriced with the Black-Scholes shock grid
    (`stressed_spread_loss_bs`) -- DTE from `p.expiry` vs `today`, per-leg IV from `iv_fn(p)`.
    Otherwise (any of those omitted -> every legacy caller/test) it falls back to the intrinsic
    `stressed_spread_loss`, byte-identical to before."""
    use_bs = (f is not None and getattr(f, "gap_stress_model", None) == "bs"
              and iv_fn is not None and today is not None)
    out = {}
    for d in (1.0, 1.5, 2.0):
        total = 0.0
        for p in positions:
            if use_bs:
                dte = _dte(getattr(p, "expiry", None), today)
                short_iv, long_iv = iv_fn(p)
                total += stressed_spread_loss_bs(p.short_strike, p.long_strike, p.credit, p.qty,
                                                 spot, atr, d, dte, short_iv, long_iv, f, wing_width)
            else:
                total += stressed_spread_loss(p.short_strike, p.long_strike, p.credit, p.qty,
                                              spot, atr, d, wing_width)
        out[d] = round(total, 2)
    return out


def gap_stress_ok(positions, spot, atr, risk_equity, max_gap_stress_loss_pct, wing_width=10.0,
                  today=None, iv_fn=None, f=None):
    """True if the 1.5-ATR total stressed loss does not exceed the budget. Uses the BS model when
    `f`/`iv_fn`/`today` are supplied and `f.gap_stress_model == "bs"` (see gap_stress_losses)."""
    return gap_stress_losses(positions, spot, atr, wing_width,
                             today=today, iv_fn=iv_fn, f=f)[1.5] <= max_gap_stress_loss_pct * risk_equity
