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
