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
    return stressed_spread_loss_bs_detail(
        short_strike, long_strike, credit, qty, spot, atr, drop_atr, dte, short_iv, long_iv, f,
        wing_width=wing_width, iv_point_change=iv_point_change,
        stressed_close_slippage=stressed_close_slippage)["loss"]


def stressed_spread_loss_bs_detail(short_strike, long_strike, credit, qty, spot, atr, drop_atr,
                                   dte, short_iv, long_iv, f, wing_width=10.0,
                                   iv_point_change=None, stressed_close_slippage=0.0):
    """Advisor Step 2C: the same computation as stressed_spread_loss_bs, returning the full input and
    intermediate breakdown so every BS input is inspectable and testable rather than inferred from a
    single output number.

    Guards (Step 2C): spot, shocked spot, both strikes and both stressed IVs must be positive, and
    the strikes must be correctly ordered for a bull put spread. These are hard errors -- they mean
    the data feed produced nonsense, and silently repricing nonsense is how a bad risk number gets
    trusted. Time to expiry is the exception: T <= 0 is a LEGITIMATE state (a 0-DTE position on
    expiry day), so it is not an error; the option is valued at intrinsic, which is what bs_put's
    T=0 limit gives anyway."""
    if not (spot > 0):
        raise ValueError(f"gap stress: spot must be positive, got {spot}")
    if not (short_strike > 0 and long_strike > 0):
        raise ValueError(f"gap stress: strikes must be positive, got {short_strike}/{long_strike}")
    if not (short_strike > long_strike):
        raise ValueError("gap stress: bull put spread requires short_strike > long_strike, got "
                         f"{short_strike}/{long_strike}")
    # Advisor directive (botbnextsteps): ZERO time to expiry is a legitimate state (expiry day) and
    # prices at intrinsic; only NEGATIVE time is an error. `dte` is calendar days here, so the sign
    # check happens before the /365 conversion.
    if dte is None:
        raise ValueError("gap stress: dte cannot be None")
    if dte < 0:
        raise ValueError(f"gap stress: time_to_expiry_years cannot be negative, got dte={dte}")
    T = dte / 365.0
    S = spot - drop_atr * atr
    if not (S > 0):
        raise ValueError(f"gap stress: shocked spot must be positive, got {S} "
                         f"(spot={spot}, atr={atr}, drop_atr={drop_atr})")
    iv_bumps = f.gap_iv_shocks if iv_point_change is None else (iv_point_change / 100.0,)
    worst = 0.0
    worst_cell = None
    for iv_bump in iv_bumps:
        for skew_extra in (0.0, f.gap_skew_bump):      # worst-case: bump the SHORT leg to maximize debit
            short_stressed_iv = short_iv + iv_bump + skew_extra
            long_stressed_iv = long_iv + iv_bump
            if not (short_stressed_iv > 0 and long_stressed_iv > 0):
                raise ValueError("gap stress: stressed IV must be positive, got "
                                 f"{short_stressed_iv}/{long_stressed_iv}")
            sp = bs_put(S, short_strike, T, short_stressed_iv, f.risk_free_rate)
            lp = bs_put(S, long_strike,  T, long_stressed_iv,  f.risk_free_rate)
            # Clamp each leg to its no-arbitrage range before differencing: a put is worth at least
            # its intrinsic value and never more than its strike. A model value outside that band is
            # numerical noise, and differencing two noisy legs can invert the spread.
            sp = min(short_strike, max(max(0.0, short_strike - S), sp))
            lp = min(long_strike, max(max(0.0, long_strike - S), lp))
            mid = max(0.0, sp - lp)                     # short > long puts -> mid >= 0 by construction
            nat = mid * (1.0 + f.gap_stress_widen)      # stressed-natural close
            if nat > worst:
                worst, worst_cell = nat, {
                    "iv_bump": iv_bump, "skew_extra": skew_extra,
                    "short_iv_before": short_iv, "long_iv_before": long_iv,
                    "short_iv_after": short_stressed_iv, "long_iv_after": long_stressed_iv,
                    "short_put_value": sp, "long_put_value": lp, "spread_mid": mid,
                }
    debit_before_cap = worst + stressed_close_slippage
    # Cap at the structural maximum loss: a vertical can never cost more than its wing to close.
    debit = min(wing_width, max(0.0, debit_before_cap))
    return {
        "loss": (debit - credit) * 100.0 * qty,
        "scenario_spot": S, "spot": spot, "drop_atr": drop_atr, "atr": atr,
        "short_strike": short_strike, "long_strike": long_strike,
        "time_to_expiry_years": T, "dte": dte,
        "risk_free_rate": f.risk_free_rate, "dividend_assumption": 0.0,
        "option_type": "put", "wing_width": wing_width,
        "stressed_close_slippage": stressed_close_slippage,
        "spread_close_debit_before_cap": debit_before_cap,
        "spread_close_debit": debit, "structural_cap_applied": debit_before_cap > wing_width,
        "credit": credit, "qty": qty, "worst_cell": worst_cell,
    }


def _book_dte(expiry, today):
    """Calendar days to expiry for a BOOK position, floored at 0.

    A position whose expiry has already passed is worth its intrinsic value, not an error: the bot
    exits at time_exit_dte but a close that fails to fill can leave a position lingering past its
    expiry (observed in production -- 157 consecutive rejected stop-closes on the live account). The
    negative-time guard in stressed_spread_loss_bs_detail exists to catch a CALLER passing nonsense;
    an expired position in the book is a real state, and crashing the entry cycle over it would turn
    a stuck position into an outage."""
    return max(0, _dte(expiry, today))


def _dte(expiry, today):
    """Calendar days from `today` to `expiry` (both "YYYY-MM-DD"). 0 on any parse failure/missing.
    NEGATIVE when the expiry has passed -- use _book_dte for book positions."""
    try:
        return (datetime.strptime(expiry, "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days
    except Exception:
        return 0


def gap_stress_losses(positions, spot, atr, wing_width=10.0, today=None, iv_fn=None, f=None,
                      model="black_scholes"):
    """positions: iterable of objects with .short_strike/.long_strike/.credit/.qty (open + proposed).
    Returns {1.0: total_loss, 1.5: total_loss, 2.0: total_loss} summed across all positions.

    Advisor Step 2A: the model is now an EXPLICIT argument, not a config read. It used to be taken
    from `f.gap_stress_model`, a flag that looked like it chose the enforcement model but only ever
    affected logging -- so it was retired. Callers state what they want:

        model="black_scholes" (default) -- matches what gap_quantity_caps ENFORCES, so the logged
            OPEN-row numbers agree with the gate that actually bound.
        model="intrinsic" -- the Step 2B comparison figure ONLY. Never enforced.

    Black-Scholes additionally needs `iv_fn` and `today`; without them it falls back to intrinsic
    (a caller that supplies neither cannot be repriced)."""
    use_bs = (model == "black_scholes" and f is not None
              and iv_fn is not None and today is not None)
    out = {}
    for d in (1.0, 1.5, 2.0):
        total = 0.0
        for p in positions:
            if use_bs:
                dte = _book_dte(getattr(p, "expiry", None), today)
                short_iv, long_iv = iv_fn(p)
                total += stressed_spread_loss_bs(p.short_strike, p.long_strike, p.credit, p.qty,
                                                 spot, atr, d, dte, short_iv, long_iv, f, wing_width)
            else:
                total += stressed_spread_loss(p.short_strike, p.long_strike, p.credit, p.qty,
                                              spot, atr, d, wing_width)
        out[d] = round(total, 2)
    return out


def gap_stress_ok(positions, spot, atr, risk_equity, max_gap_stress_loss_pct, wing_width=10.0,
                  today=None, iv_fn=None, f=None, model="black_scholes"):
    """True if the 1.5-ATR total stressed loss does not exceed the budget. Uses Black-Scholes when
    `f`/`iv_fn`/`today` are supplied (see gap_stress_losses)."""
    return gap_stress_losses(positions, spot, atr, wing_width, today=today, iv_fn=iv_fn, f=f,
                             model=model)[1.5] <= max_gap_stress_loss_pct * risk_equity


def gap_quantity_cap(scenario_name, current_book_loss, one_contract_book_loss, loss_limit):
    """Largest qty of the candidate that keeps the stressed book within loss_limit (spec §8).

    incremental = max(0, one_contract_book_loss - current_book_loss) is ONE candidate contract's
    marginal stressed loss; remaining = max(0, loss_limit - current_book_loss); qty = floor(remaining
    / incremental) when incremental > 0 else 0. Returned as a QuantityCap so gap stress plugs into
    the same min()-of-caps sizing as the aggregate budgets."""
    incremental = max(0.0, one_contract_book_loss - current_book_loss)
    remaining = max(0.0, loss_limit - current_book_loss)
    if incremental <= 0:
        # Zero (or negative) marginal stressed loss: this candidate does not worsen the book's gap
        # position at all -- a deep-OTM spread whose credit exceeds its stressed close is still
        # PROFITABLE after the shock. This gate therefore imposes no constraint, and the other caps
        # in the min() decide the size.
        #
        # This branch previously returned 0, which inverted the gate's meaning: the SAFEST possible
        # candidates -- the ones that cannot lose money in the stress scenario -- were the ones gap
        # stress rejected outright. Verified reachable: a 520/510 spread at spot 575 with a $2.00
        # credit returned maximum_qty 0 from all three scenarios.
        qty = None if current_book_loss <= loss_limit else 0
    else:
        qty = int(remaining // incremental)
    return QuantityCap(
        name=scenario_name,
        maximum_qty=(None if qty is None else max(0, qty)),
        current_exposure=current_book_loss,
        limit=loss_limit,
        remaining_capacity=remaining,
        incremental_risk_per_contract=incremental,
    )


def _scenario_stressed_loss(inst, qty, scenario, spot, atr, f, iv_fn, today):
    """Stressed BS loss ($) for one instrument at `qty` under a StressScenario -- IV bump driven by
    the scenario's iv_point_change, plus STRESSED_CLOSE_SLIPPAGE, DTE from inst.expiry vs today,
    per-leg IV from iv_fn(inst) (or f.gap_fallback_iv when no iv_fn), own wing = short-long."""
    dte = _book_dte(getattr(inst, "expiry", None), today) if today is not None else 0
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


def _intrinsic_scenario_loss(inst, qty, scenario, spot, atr):
    """Intrinsic-value stressed loss for one instrument under a StressScenario -- the COMPARISON
    model only (advisor Step 2B). Never enforced: it ignores time value, the IV shock and the
    stressed-close slippage, which is precisely why it understates."""
    wing = inst.short_strike - inst.long_strike
    return stressed_spread_loss(inst.short_strike, inst.long_strike, inst.credit, qty,
                                spot, atr, -scenario.atr_move, wing_width=wing)


def gap_model_comparison(open_positions, candidate, spot, atr, f, risk_equity,
                         iv_fn=None, today=None):
    """Advisor Step 2B: the intrinsic-vs-Black-Scholes comparison for ONE candidate, for Bot B
    validation logging.

    Black-Scholes remains the enforcing model -- this function computes the intrinsic numbers ONLY so
    the difference can be measured rather than argued about. It has no effect on sizing; nothing here
    feeds a cap. Reported per the 1.5-ATR scenario (the headline budget), plus the flag the advisor
    most wants out of the sandbox: did Black-Scholes turn a tradeable candidate into an untradeable
    one."""
    scenario = STRESS_SCENARIOS[1]                       # down_1_5atr
    loss_limit = risk_equity * f.max_gap_stress_loss_pct

    bs_book = sum(_scenario_stressed_loss(p, p.qty, scenario, spot, atr, f, iv_fn, today)
                  for p in open_positions)
    bs_one = _scenario_stressed_loss(candidate, 1, scenario, spot, atr, f, iv_fn, today)
    bs_cap = gap_quantity_cap("gap_1_5atr", bs_book, bs_book + bs_one, loss_limit)

    intr_book = sum(_intrinsic_scenario_loss(p, p.qty, scenario, spot, atr)
                    for p in open_positions)
    intr_one = _intrinsic_scenario_loss(candidate, 1, scenario, spot, atr)
    intr_cap = gap_quantity_cap("gap_1_5atr_intrinsic", intr_book, intr_book + intr_one, loss_limit)

    return {
        "enforced_model": "black_scholes",
        "gap_loss_limit": round(loss_limit, 2),
        "bs_current_book_1_5": round(bs_book, 2),
        "intrinsic_current_book_1_5": round(intr_book, 2),
        "bs_incremental_1_5": round(bs_cap.incremental_risk_per_contract, 2),
        "intrinsic_incremental_1_5": round(intr_cap.incremental_risk_per_contract, 2),
        # An abstaining cap (maximum_qty None) means "no constraint from this scenario". Reported as
        # None rather than coerced to a number, so a reader cannot mistake "unconstrained" for a
        # literal permitted quantity; the difference is only meaningful when BOTH models bound.
        "bs_qty_1_5": bs_cap.maximum_qty,
        "intrinsic_qty_1_5": intr_cap.maximum_qty,
        "qty_difference_1_5": (None if (bs_cap.maximum_qty is None or intr_cap.maximum_qty is None)
                               else intr_cap.maximum_qty - bs_cap.maximum_qty),
        "bs_zeroed_the_candidate": (bs_cap.maximum_qty == 0
                                    and (intr_cap.maximum_qty is None or intr_cap.maximum_qty > 0)),
    }
