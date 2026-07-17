"""Gap-risk stress test (partner review v2 §10): reprice spreads at SPY down 1/1.5/2 ATR.

Priority-0 fix item 5: the loss estimate now supports a Black-Scholes shock-grid reprice
(`stressed_spread_loss_bs`) in addition to the original intrinsic-value-only estimate
(`stressed_spread_loss`, kept unchanged for back-compat/comparison). The intrinsic model IGNORED
time value, IV expansion, skew, and gamma, so it materially UNDERSTATED the stressed loss (a spread
sitting just OTM after the shock could still read as "profitable"). The BS model reprices each leg
across an IV-shock x skew grid at the shocked spot and takes the WORST cell.

This module stays PURE (no network / no I/O). All IV sourcing lives in the `iv_fn` injected by the
orchestrator; DTE is derived from each position's `.expiry` vs the caller-supplied `today`."""
from dataclasses import dataclass
from datetime import datetime

from bot.portfolio.bs import bs_put
from bot.portfolio.risk_budget import QuantityCap


@dataclass
class StressScenario:
    """One gap-stress scenario (spec §9): a downside underlying move (in ATRs, negative) paired with
    an IV shock (in volatility POINTS, e.g. 3.0 == +0.03 absolute IV)."""
    name: str
    atr_move: float          # negative = down, in ATRs
    iv_point_change: float   # +volatility points applied on top of current IV


# Spec §9 minimum scenario matrix: deeper move -> larger IV shock.
STRESS_SCENARIOS = [
    StressScenario(name="down_1atr", atr_move=-1.0, iv_point_change=3.0),
    StressScenario(name="down_1_5atr", atr_move=-1.5, iv_point_change=5.0),
    StressScenario(name="down_2atr", atr_move=-2.0, iv_point_change=10.0),
]

# Extra debit-to-close slippage assumed when closing INTO a stressed/gapping market (spec §9).
STRESSED_CLOSE_SLIPPAGE = 0.10

# Per-scenario budget-pct feature-field names, in scenario order (1.0/1.5/2.0 ATR). The 1.5-ATR
# scenario keeps the original max_gap_stress_loss_pct; the flanking scenarios use the new fields.
_SCENARIO_LIMIT_FIELDS = ("gap_1atr_limit_pct", "max_gap_stress_loss_pct", "gap_2atr_limit_pct")
# Cap names the orchestrator keys off (spec §3/§8: gap_1atr_cap/gap_1_5atr_cap/gap_2atr_cap),
# distinct from the StressScenario.name ("down_*") which labels the underlying-move scenario.
_SCENARIO_CAP_NAMES = ("gap_1atr", "gap_1_5atr", "gap_2atr")


def stressed_spread_loss(short_strike, long_strike, credit, qty, spot, atr, drop_atr, wing_width=10.0):
    """Loss ($, positive = loss) on a bull put spread if SPY drops `drop_atr` ATRs, using a
    conservative intrinsic-value close estimate: stressed debit-to-close =
    min(wing_width, max(0, short-S) - max(0, long-S)) at S = spot - drop_atr*atr.
    Loss = (stressed_debit - credit) * 100 * qty  (negative means the position is still profitable)."""
    s = spot - drop_atr * atr
    stressed_debit = min(wing_width, max(0.0, short_strike - s) - max(0.0, long_strike - s))
    return (stressed_debit - credit) * 100.0 * qty


def stressed_spread_loss_bs(short_strike, long_strike, credit, qty, spot, atr, drop_atr,
                            dte, short_iv, long_iv, f, wing_width=10.0,
                            iv_point_change=None, stressed_close_slippage=0.0):
    """Black-Scholes shock-grid loss ($, positive = loss) on a bull put spread if SPY drops
    `drop_atr` ATRs. The spread's stressed debit-to-close is repriced at the shocked spot across an
    IV-shock x skew grid (short leg bumped extra on the stressed-skew cells) plus a bid/ask widening
    haircut, and the WORST (largest) resulting debit is taken -- then clamped to [0, wing_width].
    Loss = (worst_debit - credit) * 100 * qty.

    iv_point_change (spec §9, opt-in): when given, the base IV bump is driven by this single scenario
    shock (points -> absolute IV, /100) INSTEAD of iterating f.gap_iv_shocks -- so the per-scenario
    matrix controls the shock. Left None (every existing caller) -> byte-identical to before.
    stressed_close_slippage (spec §9): extra debit added to the stressed close before the
    structural (wing_width) clamp. Defaults to 0.0 -> byte-identical to before."""
    T = max(dte, 0) / 365.0
    S = spot - drop_atr * atr
    iv_bumps = f.gap_iv_shocks if iv_point_change is None else (iv_point_change / 100.0,)
    worst = 0.0
    for iv_bump in iv_bumps:
        for skew_extra in (0.0, f.gap_skew_bump):      # worst-case: bump the SHORT leg to maximize debit
            sp = bs_put(S, short_strike, T, short_iv + iv_bump + skew_extra, f.risk_free_rate)
            lp = bs_put(S, long_strike,  T, long_iv  + iv_bump,             f.risk_free_rate)
            mid = sp - lp
            nat = mid * (1.0 + f.gap_stress_widen)      # stressed-natural close (nat >= mid, since
            worst = max(worst, nat)                     # short>long puts -> mid>=0 and widen>0)
    worst = min(wing_width, max(0.0, worst + stressed_close_slippage))
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


def gap_quantity_cap(scenario_name, current_book_loss, one_contract_book_loss, loss_limit):
    """Largest qty of the candidate that keeps the stressed book within loss_limit (spec §8).

    incremental = max(0, one_contract_book_loss - current_book_loss) is ONE candidate contract's
    marginal stressed loss; remaining = max(0, loss_limit - current_book_loss); qty = floor(remaining
    / incremental) when incremental > 0 else 0. Returned as a QuantityCap so gap stress plugs into
    the same min()-of-caps sizing as the aggregate budgets."""
    incremental = max(0.0, one_contract_book_loss - current_book_loss)
    remaining = max(0.0, loss_limit - current_book_loss)
    if incremental <= 0:
        qty = 0
    else:
        qty = int(remaining // incremental)
    return QuantityCap(
        name=scenario_name,
        maximum_qty=max(0, qty),
        current_exposure=current_book_loss,
        limit=loss_limit,
        remaining_capacity=remaining,
        incremental_risk_per_contract=incremental,
    )


def _scenario_stressed_loss(inst, qty, scenario, spot, atr, f, iv_fn, today):
    """Stressed BS loss ($) for one instrument at `qty` under a StressScenario -- IV bump driven by
    the scenario's iv_point_change, plus STRESSED_CLOSE_SLIPPAGE, DTE from inst.expiry vs today,
    per-leg IV from iv_fn(inst) (or f.gap_fallback_iv when no iv_fn), own wing = short-long."""
    dte = _dte(getattr(inst, "expiry", None), today) if today is not None else 0
    if iv_fn is not None:
        short_iv, long_iv = iv_fn(inst)
    else:
        short_iv = long_iv = f.gap_fallback_iv
    wing = inst.short_strike - inst.long_strike
    return stressed_spread_loss_bs(inst.short_strike, inst.long_strike, inst.credit, qty,
                                   spot, atr, -scenario.atr_move, dte, short_iv, long_iv, f,
                                   wing_width=wing, iv_point_change=scenario.iv_point_change,
                                   stressed_close_slippage=STRESSED_CLOSE_SLIPPAGE)


def gap_quantity_caps(open_positions, candidate, spot, atr, f, risk_equity, iv_fn=None, today=None):
    """One QuantityCap per gap-stress scenario (spec §8/§9), in order gap_1atr/gap_1_5atr/gap_2atr.

    For each scenario: current_book_loss = summed stressed loss of the existing book; the candidate's
    ONE-contract marginal stressed loss is added to get one_contract_book_loss; gap_quantity_cap then
    yields the largest candidate qty that keeps the stressed book within
    risk_equity * <scenario limit pct> (1.0-ATR: f.gap_1atr_limit_pct, 1.5-ATR: f.max_gap_stress_loss_pct,
    2.0-ATR: f.gap_2atr_limit_pct). PURE -- all IV sourcing lives in the injected iv_fn."""
    caps = []
    for scenario, cap_name, limit_field in zip(STRESS_SCENARIOS, _SCENARIO_CAP_NAMES,
                                               _SCENARIO_LIMIT_FIELDS):
        current_book_loss = sum(
            _scenario_stressed_loss(p, p.qty, scenario, spot, atr, f, iv_fn, today)
            for p in open_positions)
        candidate_one = _scenario_stressed_loss(candidate, 1, scenario, spot, atr, f, iv_fn, today)
        loss_limit = risk_equity * getattr(f, limit_field)
        caps.append(gap_quantity_cap(cap_name, current_book_loss,
                                     current_book_loss + candidate_one, loss_limit))
    return caps
