"""Transaction-cost profitability gate (partner review v2 §5)."""


def estimated_round_trip_cost(f):
    """f = S2bFeatures. Commission = est_commission_per_leg_rt * 2 legs; slippage = (entry+exit) est,
    using expected_entry_slippage as the per-side estimate, in dollars/contract."""
    commission = f.est_commission_per_leg_rt * 2.0
    slippage = (f.expected_entry_slippage + f.expected_entry_slippage) * 100.0
    return commission + slippage


def cost_gate_eval(expected_credit, f):
    """Return a dict: gross_target, round_trip_cost, target_to_cost_ratio, passes (bool)."""
    gross_target = expected_credit * f.take_profit_percent * 100.0
    rt = estimated_round_trip_cost(f)
    ratio = gross_target / rt if rt > 0 else float("inf")
    return {"gross_target": round(gross_target, 2), "round_trip_cost": round(rt, 2),
            "target_to_cost_ratio": round(ratio, 3), "passes": ratio >= f.min_target_to_cost_ratio}
