"""Black-Scholes European put pricer (Priority-0 fix item 5a).

Used by the gap-risk stress test (bot/portfolio/gap_stress.py) to reprice bull-put spreads under a
shock grid instead of the old intrinsic-value-only estimate, which materially UNDERESTIMATED the
stressed loss (it ignored time value, IV expansion, skew, and gamma). Pure math -- no I/O."""
import math


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put(S, K, T, iv, r=0.04):
    """European put, Black-Scholes. T in years, iv annualized. T<=0 or iv<=0 -> intrinsic."""
    if T <= 0 or iv <= 0 or S <= 0:
        return max(0.0, K - S)
    srt = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / srt
    d2 = d1 - srt
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)
