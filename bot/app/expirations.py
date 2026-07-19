"""Alternate-expiration evaluation (T8; spec §14, advisor botbnextsteps Step 1).

The bot previously evaluated ONLY the nearest eligible expiration. If that one had no capacity it
polled it all day and never noticed that a later expiration would have qualified. T8 evaluates up to
three and picks the best.

The rule that keeps this safe: **a later expiration is an ALTERNATIVE, never an EXEMPTION.** Each
candidate runs the complete independent evaluation -- credit quality, low-credit safety, cost gate,
all four aggregate risk caps, all three gap-stress caps -- and nothing here can wave a candidate past
a gate the nearest expiration would have failed.

Selection deliberately ranks on expected NET target profit rather than gross credit: a fatter credit
further out can easily be the worse trade once costs and the permitted quantity are accounted for.

PURE: no network, no clock, no state. The orchestrator supplies the evaluations."""
from dataclasses import dataclass
from datetime import datetime


MINIMUM_DTE = 4
MAXIMUM_EXPIRATION_COUNT = 3


@dataclass
class CandidateEvaluation:
    """One fully-evaluated expiration. `allowed` means it cleared every gate AND has a positive
    permitted quantity; `rejection_reason` carries the gate that stopped it otherwise, so a
    no-candidate-fits outcome can still say WHY each expiration failed."""
    expiry: str
    dte: int

    short_strike: float
    long_strike: float
    wing_width: float

    expected_credit: float
    credit_ratio: float
    credit_tier: str

    quote_age_seconds: float
    target_to_cost_ratio: float

    requested_qty: int
    quality_adjusted_qty: int
    final_qty: int

    expected_net_target_profit_per_contract: float
    expected_net_target_profit: float

    risk_result: object          # RiskSizingResult

    allowed: bool
    rejection_reason: str


def get_candidate_expirations(available_expirations, today, minimum_dte=MINIMUM_DTE,
                              maximum_count=MAXIMUM_EXPIRATION_COUNT):
    """The soonest `maximum_count` expirations at least `minimum_dte` calendar days out.

    Returns [(expiry, dte)] sorted soonest-first. Duplicates are collapsed so one expiry can never be
    evaluated twice, and unparseable dates are skipped rather than raising -- a malformed entry in a
    broker's expiration list should cost us that one date, not the whole entry cycle."""
    try:
        d0 = datetime.strptime(today, "%Y-%m-%d")
    except (TypeError, ValueError):
        return []
    seen, out = set(), []
    for expiry in available_expirations or []:
        if expiry in seen:
            continue
        try:
            dte = (datetime.strptime(expiry, "%Y-%m-%d") - d0).days
        except (TypeError, ValueError):
            continue
        seen.add(expiry)
        if dte < minimum_dte:
            continue
        out.append((expiry, dte))
    out.sort(key=lambda pair: pair[1])
    return out[:maximum_count]


def select_best_candidate(evaluations):
    """The best tradeable candidate, or None when none qualifies (spec §14).

    Eligible = cleared every gate, has a positive permitted quantity, AND is expected to make money
    after costs. Ranked by expected net target profit, then credit ratio, then SHORTER dte (less time
    at risk for the same expected return).

    Ranking on net profit rather than gross credit is the point: a later expiration usually collects
    a bigger credit, and choosing on that alone would systematically drag the bot out in time for
    trades that are worse once costs and permitted size are counted."""
    eligible = [c for c in evaluations
                if c.allowed and c.final_qty > 0 and c.expected_net_target_profit > 0]
    if not eligible:
        return None
    return max(eligible, key=lambda c: (c.expected_net_target_profit, c.credit_ratio, -c.dte))


def evaluate_expiration(*, expiry, dte, chain, spot, atr, deps, state, now, regime,
                        credit_history, iv_fn, prime_leg_ivs, open_positions, foreign_positions,
                        today):
    """Evaluate ONE expiration end to end and return a CandidateEvaluation (T8 / spec §14).

    This is a PRE-SELECTION pass, and deliberately so. It calls the very same helpers the entry path
    uses -- build_spread_order, quotes_valid/package_too_wide, expected_executable_credit,
    classify_credit_quality, cost_gate_eval, apply_quality_multiplier, budget_quantity_caps,
    gap_quantity_caps, size_to_risk_limits -- rather than re-deriving any rule, so the two cannot
    drift apart on the numbers that decide anything.

    It is READ-ONLY: no counters, no credit-history appends, no logging, no order submission. Its
    only job is to rank expirations. Whichever one wins is then run through the REAL entry path in
    full, where the authoritative gates fire and all state mutation happens. That means this pass can
    only ever NARROW the choice -- it can never wave a candidate past a gate, which is exactly the
    property the directive requires of alternate expirations."""
    from bot.strategy.s2b import build_spread_order
    from bot.strategy.execution_price import (quotes_valid, package_too_wide,
                                               expected_executable_credit)
    from bot.strategy.credit_quality import (classify_credit_quality, dte_bucket,
                                              full_size_threshold)
    from bot.strategy.cost_gate import cost_gate_eval
    from bot.portfolio.risk_budget import (size_qty, apply_quality_multiplier,
                                            budget_quantity_caps, size_to_risk_limits,
                                            entry_ceiling_cap)
    from bot.portfolio.gap_stress import gap_quantity_caps
    from bot.portfolio import exposure
    from dataclasses import replace

    f = deps.features
    wing = deps.s2b_cfg.wing_width

    def blocked(reason, **kw):
        base = dict(expiry=expiry, dte=dte, short_strike=0.0, long_strike=0.0, wing_width=wing,
                    expected_credit=0.0, credit_ratio=0.0, credit_tier="", quote_age_seconds=None,
                    target_to_cost_ratio=0.0, requested_qty=0, quality_adjusted_qty=0, final_qty=0,
                    expected_net_target_profit_per_contract=0.0, expected_net_target_profit=0.0,
                    risk_result=None, allowed=False, rejection_reason=reason)
        base.update(kw)
        return CandidateEvaluation(**base)

    order = build_spread_order(spot, atr, chain, deps.s2b_cfg)
    if order is None:
        return blocked("no_order")
    order.expiry = expiry

    exec_credit = None
    if f.credit_tiers or f.transaction_cost_gate:
        if order.short_bid is not None:
            if not quotes_valid(order.short_bid, order.short_ask, order.long_bid, order.long_ask):
                return blocked("quote_invalid", short_strike=order.short_strike,
                               long_strike=order.long_strike)
            if package_too_wide(order.short_bid, order.short_ask, order.long_bid, order.long_ask,
                                f.max_package_width_ratio):
                return blocked("quote_wide", short_strike=order.short_strike,
                               long_strike=order.long_strike)
            exec_credit = expected_executable_credit(order.short_bid, order.short_ask,
                                                     order.long_bid, order.long_ask,
                                                     f.expected_entry_slippage)
    credit = exec_credit if exec_credit is not None else order.credit
    ratio = credit / wing
    common = dict(short_strike=order.short_strike, long_strike=order.long_strike,
                  expected_credit=round(credit, 4), credit_ratio=round(ratio, 4))

    tier, quality_mult, quality_max = "full", 1.0, None
    if f.credit_tiers:
        prior = list(credit_history.get(dte_bucket(dte), []))
        thr = full_size_threshold(prior, f.full_size_credit_floor, f.full_size_credit_ceiling,
                                  f.credit_adapt_min_signals)
        tier, quality_mult, quality_max = classify_credit_quality(ratio, thr)
        common["credit_tier"] = tier
        if tier == "reject":
            return blocked("credit_too_low", **common)
        if tier == "low_credit_safety" and not f.enable_low_credit_08_to_10:
            return blocked("credit_too_low", **common)

    cost = cost_gate_eval(credit, f)
    common["target_to_cost_ratio"] = cost["target_to_cost_ratio"]
    if f.transaction_cost_gate and not cost["passes"]:
        return blocked("cost_gate", **common)

    if not f.aggregate_risk_budget:
        # Legacy sizing path has no quantity-cap model to rank on; treat the nearest expiry as the
        # only candidate rather than inventing a comparison the live config never uses.
        return blocked("alternate_expirations_requires_aggregate_risk_budget", **common)

    req = deps.risk_equity()
    base_qty = size_qty(req, credit, wing, f, quality_multiplier=1.0)
    quality_qty = apply_quality_multiplier(base_qty, quality_mult, quality_max)
    if quality_qty <= 0:
        return blocked("risk_budget", requested_qty=base_qty, quality_adjusted_qty=0, **common)

    foreign = exposure.account_spy_exposure(foreign_positions)
    gap_book = list(open_positions) + list(foreign_positions)
    candidate_view = replace(order, credit=credit)
    prime_leg_ivs(gap_book + [candidate_view])
    caps = (budget_quantity_caps(credit, wing, expiry, today, open_positions, deps.mark_position,
                                 req, f, foreign_exposure=foreign)
            + gap_quantity_caps(gap_book, candidate_view, spot, atr, f, req,
                                iv_fn=iv_fn, today=today))
    ceiling = entry_ceiling_cap(f.max_entry_qty)
    if ceiling is not None:
        caps.append(ceiling)
    result = size_to_risk_limits(quality_qty, caps)

    # Expected NET target profit: what the take-profit exit actually clears after round-trip costs.
    per_contract = credit * f.take_profit_percent * 100.0 - cost["round_trip_cost"]
    total = per_contract * result.final_qty
    ev = CandidateEvaluation(
        expiry=expiry, dte=dte, wing_width=wing, quote_age_seconds=None,
        requested_qty=base_qty, quality_adjusted_qty=quality_qty, final_qty=result.final_qty,
        expected_net_target_profit_per_contract=round(per_contract, 2),
        expected_net_target_profit=round(total, 2),
        risk_result=result, allowed=result.final_qty > 0,
        rejection_reason=(None if result.final_qty > 0
                          else "risk_budget:%s" % (result.limiting_gate or "requested_qty")),
        credit_tier=common.get("credit_tier", tier), **{k: v for k, v in common.items()
                                                        if k != "credit_tier"})
    return ev
