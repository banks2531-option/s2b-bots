"""$5-wide fallback, SHADOW ONLY (T11; spec §15, advisor botbnextsteps Step 4).

When a $10-wide candidate clears every non-risk gate but is reduced to ZERO by a risk cap, the
interesting question is whether a narrower $5 wing -- same short strike, half the structural risk --
would have fitted. This module answers that question in the log and nowhere else.

**It cannot trade, structurally.** The evaluator returns a plain dict, not an order: there is no
payload to submit even by accident. The record carries `can_submit_order=False` and
`source="five_wide_shadow"`, and `assert_not_shadow` raises if such a record ever reaches an
execution path. That is three independent barriers, because "the flag is off" is not a guarantee.

Trigger discipline matters as much as the guard: the fallback runs ONLY for candidates that failed on
RISK. A candidate rejected for thin credit, stale quotes or a defensive regime does not get a second
look at a narrower wing -- a $5 wing does not fix a bad candidate, and evaluating one anyway would
manufacture encouraging research for trades that should never happen.

PURE: no network, no clock, no state."""

SHADOW_SOURCE = "five_wide_shadow"
FIVE_WIDE_WIDTH = 5.0


class ShadowOrderSubmissionError(Exception):
    """Raised if a shadow candidate reaches an order-submission path. Fatal by design: a shadow
    spread reaching the broker is a research feature spending real money."""


def assert_not_shadow(candidate):
    """Guard for the execution path. Call before submitting anything."""
    source = candidate.get("source") if isinstance(candidate, dict) else getattr(
        candidate, "source", None)
    if source == SHADOW_SOURCE:
        raise ShadowOrderSubmissionError(
            "shadow candidates may not submit orders (source=%r)" % source)


def should_evaluate_five_wide(ten_wide) -> bool:
    """True only when a $10-wide candidate passed every NON-RISK gate, was sized to a positive
    quality quantity, and was then reduced to zero by a named risk cap (spec §15)."""
    return bool(getattr(ten_wide, "passed_non_risk_gates", False)
                and getattr(ten_wide, "quality_adjusted_qty", 0) > 0
                and getattr(ten_wide, "final_qty", 0) == 0
                and getattr(ten_wide, "limiting_gate", None) is not None)


def _quote_at(chain, strike):
    for q in chain:
        if abs(q.strike - strike) < 1e-6:
            return q
    return None


def evaluate_five_wide_shadow(*, ten_wide, chain, spot, atr, f, risk_equity, open_positions,
                              foreign_positions, mark_fn, iv_fn, today, wing=FIVE_WIDE_WIDTH):
    """Hypothetical $5-wide evaluation at the $10-wide's short strike. Returns a log record, or None
    when the shadow feature is off.

    Reuses the same helpers as the live sizing path (cost_gate_eval, planned/structural risk,
    budget_quantity_caps, gap_quantity_caps, size_to_risk_limits) so the hypothetical quantity is
    computed the way a real one would be -- otherwise the research would be measuring a different
    system than the one it is meant to inform."""
    if not getattr(f, "enable_five_wide_shadow", False):
        return None

    from bot.strategy.execution_price import expected_executable_credit
    from bot.strategy.cost_gate import cost_gate_eval
    from bot.portfolio.risk_budget import (size_qty, planned_stop_loss_per_contract,
                                            structural_max_loss_per_contract,
                                            budget_quantity_caps, size_to_risk_limits,
                                            entry_ceiling_cap)
    from bot.portfolio.gap_stress import gap_quantity_caps, STRESS_SCENARIOS, gap_quantity_cap
    from bot.portfolio import exposure

    short_strike = ten_wide.short_strike
    long_strike = short_strike - wing
    record = {
        "event": "FIVE_WIDE_SHADOW",
        "source": SHADOW_SOURCE,
        "can_submit_order": False,
        "expiry": getattr(ten_wide, "expiry", None),
        "ten_wide_short_strike": short_strike,
        "ten_wide_long_strike": ten_wide.long_strike,
        "ten_wide_final_qty": ten_wide.final_qty,
        "ten_wide_limiting_gate": ten_wide.limiting_gate,
        "five_wide_short_strike": short_strike,
        "five_wide_long_strike": long_strike,
        "five_wide_available": False,
        "five_wide_credit": None,
        "five_wide_credit_ratio": None,
        "five_wide_target_to_cost_ratio": None,
        "five_wide_structural_risk": None,
        "five_wide_stop_risk": None,
        "five_wide_incremental_gap_risk": None,
        "five_wide_final_qty": 0,
        "five_wide_limiting_gate": None,
    }

    short_q, long_q = _quote_at(chain, short_strike), _quote_at(chain, long_strike)
    if short_q is None or long_q is None:
        # The narrower long leg simply is not listed. That is a real finding about the chain, not an
        # error -- record it and move on.
        return record

    credit = expected_executable_credit(short_q.bid, short_q.ask, long_q.bid, long_q.ask,
                                        f.expected_entry_slippage)
    cost = cost_gate_eval(credit, f)
    stop_per_contract = planned_stop_loss_per_contract(credit, wing, f.expected_stop_slippage)
    structural_per_contract = structural_max_loss_per_contract(credit, wing)
    record.update({
        "five_wide_available": True,
        "five_wide_credit": round(credit, 4),
        "five_wide_credit_ratio": round(credit / wing, 4),
        "five_wide_target_to_cost_ratio": cost["target_to_cost_ratio"],
        "five_wide_structural_risk": round(structural_per_contract, 2),
        "five_wide_stop_risk": round(stop_per_contract, 2),
    })

    class _Cand:
        """Minimal position-shaped view of the hypothetical $5 spread for the risk helpers."""
        ticker = "SPY"
        qty = 1
        def __init__(self, short, long_, credit, expiry):
            self.short_strike, self.long_strike = short, long_
            self.credit, self.expiry = credit, expiry

    candidate = _Cand(short_strike, long_strike, credit, record["expiry"])
    base_qty = size_qty(risk_equity, credit, wing, f, quality_multiplier=1.0)
    foreign = exposure.account_spy_exposure(foreign_positions)
    gap_book = list(open_positions) + list(foreign_positions)
    caps = (budget_quantity_caps(credit, wing, record["expiry"], today, open_positions, mark_fn,
                                 risk_equity, f, foreign_exposure=foreign)
            + gap_quantity_caps(gap_book, candidate, spot, atr, f, risk_equity,
                                iv_fn=iv_fn, today=today))
    ceiling = entry_ceiling_cap(f.max_entry_qty)
    if ceiling is not None:
        caps.append(ceiling)
    result = size_to_risk_limits(base_qty, caps)
    gap_caps = [c for c in caps if c.name.startswith("gap_")]
    record.update({
        "five_wide_final_qty": result.final_qty,
        "five_wide_limiting_gate": result.limiting_gate,
        "five_wide_incremental_gap_risk": (
            round(max(c.incremental_risk_per_contract for c in gap_caps), 2) if gap_caps else None),
    })
    return record
