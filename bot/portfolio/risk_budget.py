"""Aggregate dollar-risk budget sizing + book limits (partner review v2 §9)."""
import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# The four aggregate budgets, in the fixed order cap_to_budgets evaluates them. Centralized here so
# the budget-name strings live in exactly ONE place -- cap_to_budgets, RiskBudgetResult, and any
# caller reading exposure/limit for a binding budget all key off these constants, so a rename can't
# silently drift between call sites (which previously risked producing None telemetry downstream).
SAME_DAY_STOP = "same_day_stop"
EXPIRY_STOP = "expiry_stop"
TOTAL_STOP = "total_stop"
TOTAL_STRUCTURAL = "total_structural"
BUDGET_NAMES = (SAME_DAY_STOP, EXPIRY_STOP, TOTAL_STOP, TOTAL_STRUCTURAL)


@dataclass
class RiskBudgetResult:
    """Result of cap_to_budgets (Priority-0 fix item 7): the permitted quantity PLUS which budget
    (if any) bound it, and the raw exposure/limit numbers for every budget -- so a decision log can
    say WHICH of the four budgets rejected a candidate, not just the bare fact that one did.

    Kept int-compatible (__int__/__index__/__eq__/__hash__) so every existing call site that treats
    cap_to_budgets' return value as a plain int (arithmetic, comparisons) keeps working unchanged."""
    allowed_quantity: int
    limiting_budget: Optional[str]   # one of "same_day_stop"/"expiry_stop"/"total_stop"/
                                      # "total_structural", or None when unconstrained
    same_day_stop: float
    expiry_stop: float
    total_stop: float
    total_structural: float
    same_day_limit: float
    expiry_limit: float
    total_stop_limit: float
    structural_limit: float

    def __int__(self):
        return self.allowed_quantity

    def __index__(self):
        return self.allowed_quantity

    def __eq__(self, o):
        # NOTE: compares allowed_quantity ONLY (for int back-compat) -- two RiskBudgetResults with
        # the same qty but DIFFERENT limiting_budget compare EQUAL. Footgun for future test authors:
        # assert the limiting_budget field explicitly, never rely on == to distinguish them.
        if isinstance(o, RiskBudgetResult):
            return self.allowed_quantity == o.allowed_quantity
        if isinstance(o, int):
            return self.allowed_quantity == o
        return NotImplemented

    def __hash__(self):
        return hash(self.allowed_quantity)

    def limiting_exposure_and_limit(self):
        """(exposure, limit) dollar figures for THIS result's own limiting_budget, read straight off
        the populated fields (no name-keyed dict lookup that could silently miss on a rename).
        Returns (None, None) when unconstrained (limiting_budget is None)."""
        return {
            SAME_DAY_STOP: (self.same_day_stop, self.same_day_limit),
            EXPIRY_STOP: (self.expiry_stop, self.expiry_limit),
            TOTAL_STOP: (self.total_stop, self.total_stop_limit),
            TOTAL_STRUCTURAL: (self.total_structural, self.structural_limit),
        }.get(self.limiting_budget, (None, None))


@dataclass
class QuantityCap:
    """A single risk gate expressed as "the largest number of contracts that safely fit" (spec §3).

    Every risk category (each aggregate budget, each gap-stress scenario) yields one of these instead
    of a bare True/False, so the orchestrator can REDUCE an oversized order to the caps' minimum
    rather than rejecting it outright, and a decision log can name the exact binding gate plus its
    exposure/limit/remaining/incremental figures."""
    name: str
    maximum_qty: int
    current_exposure: float
    limit: float
    remaining_capacity: float
    incremental_risk_per_contract: float


def quantity_cap_from_budget(name, current_exposure, limit, incremental_risk_per_contract):
    """Largest qty that keeps current_exposure + qty*incremental within limit (spec §4).

    remaining = max(0, limit - current_exposure); qty = floor(remaining / incremental) when the
    incremental risk is positive, else 0 (a non-positive incremental permits zero -- never an
    unlimited/divide-by-zero result). maximum_qty is clamped to >= 0."""
    remaining = max(0.0, limit - current_exposure)
    if incremental_risk_per_contract <= 0:
        qty = 0
    else:
        qty = int(remaining // incremental_risk_per_contract)
    return QuantityCap(
        name=name,
        maximum_qty=max(0, qty),
        current_exposure=current_exposure,
        limit=limit,
        remaining_capacity=remaining,
        incremental_risk_per_contract=incremental_risk_per_contract,
    )


def structural_max_loss_per_contract(wing_width, credit):
    return (wing_width - credit) * 100.0


def planned_stop_loss_per_contract(credit, wing_width, expected_stop_slippage):
    """min(structural, (2*credit + stop_slippage)*100). §9."""
    structural = structural_max_loss_per_contract(wing_width, credit)
    return min(structural, (2.0 * credit + expected_stop_slippage) * 100.0)


def remaining_stop_risk(entry_credit, current_spread_debit, qty, expected_stop_slippage):
    """Per open position: max(0, (3*credit - current_debit + slippage)*100*qty). Do NOT count
    already-incurred loss as future stop risk (that's why we subtract current_spread_debit)."""
    stop_debit = entry_credit * 3.0
    return max(0.0, (stop_debit - current_spread_debit + expected_stop_slippage) * 100.0 * qty)


def apply_quality_multiplier(base_qty, multiplier, maximum_qty=None):
    """Apply a credit-quality tier multiplier to a base qty (spec §2).

    Guarantees a CANDIDATE contract for a probe: a probe multiplier (<1.0) applied to a valid base
    qty (>=1) no longer floors to 0 -- e.g. floor(2*0.40)=0 previously turned the probe TIER into a
    rejection TIER. This only produces a *candidate*; that candidate must STILL pass every hard risk
    cap downstream (cap_to_budgets), which can still cap it to 0. It never forces 1 past a risk limit.

    With multiplier=1.0 (full tier) this is exactly floor(base_qty*1.0)=base_qty, so the full-size
    path is byte-identical to the old raw floor. Only the probe path (multiplier<1.0) changes."""
    if base_qty <= 0 or multiplier <= 0:
        return 0
    if multiplier < 1.0:
        adjusted_qty = max(1, math.floor(base_qty * multiplier))
    else:
        adjusted_qty = math.floor(base_qty * multiplier)
    if maximum_qty is not None:
        adjusted_qty = min(adjusted_qty, maximum_qty)
    return adjusted_qty


def size_qty(risk_equity, credit, wing_width, f, quality_multiplier=1.0):
    """Base qty from entry-stop-risk and structural constraints, times quality multiplier.
    The quality multiplier flows through apply_quality_multiplier (spec §2): a probe multiplier no
    longer rounds a valid base qty to 0 -- it yields a >=1 CANDIDATE which still flows into
    cap_to_budgets (the hard risk caps) at the call site and can be capped to 0 there. NO forced
    one-contract minimum AFTER risk checks. §9. f = S2bFeatures."""
    psl = planned_stop_loss_per_contract(credit, wing_width, f.expected_stop_slippage)
    struct = structural_max_loss_per_contract(wing_width, credit)
    if psl <= 0 or struct <= 0:
        return 0
    qty_entry = math.floor(risk_equity * f.max_entry_stop_risk_pct / psl)
    qty_structural = math.floor(risk_equity * f.max_trade_structural_risk_pct / struct)
    base_qty = min(qty_entry, qty_structural)
    return apply_quality_multiplier(base_qty, quality_multiplier)


def _max_q_for_budget(qty, budget, book_amount, per_contract_amount):
    """Largest 0<=q<=qty s.t. book_amount + q*per_contract_amount <= budget."""
    if per_contract_amount <= 0:
        return qty          # this trade adds no risk under this budget -> unconstrained
    remaining = budget - book_amount
    if remaining <= 0:
        return 0
    return min(qty, math.floor(remaining / per_contract_amount))


def _book_exposures(wing_width, expiry, today, open_positions, mark_fn, f, foreign_exposure=None):
    """Sum the open book's four aggregate dollar-risk exposures. Shared by cap_to_budgets AND
    budget_quantity_caps so the two can NEVER diverge on how book exposure is computed (DRY).

    Returns (same_day_stop, expiry_stop, total_stop, total_structural). For each position p:
    stop risk = remaining_stop_risk(p.credit, mark_fn(p), p.qty, f.expected_stop_slippage);
    structural = structural_max_loss_per_contract(wing_width, p.credit) * p.qty (uses the PROPOSED
    trade's wing_width for every book position, matching the original cap_to_budgets behavior).
    'same-day' = p.entry_date == today; 'expiry' = p.expiry == expiry.

    foreign_exposure (partner review v2 §2): optional {"stop":..., "structural":...} SPY-spread
    dollar-risk at the broker this bot did NOT open. None -> zero (byte-identical to omitting it).
    It counts ONLY toward the TOTAL stop/structural exposures (not this bot's same-day/expiry)."""
    if foreign_exposure is None:
        foreign_exposure = {"stop": 0.0, "structural": 0.0}
    same_day_stop = 0.0
    expiry_stop = 0.0
    total_stop = 0.0
    total_structural = 0.0
    for p in open_positions:
        stop_risk = remaining_stop_risk(p.credit, mark_fn(p), p.qty, f.expected_stop_slippage)
        structural_risk = structural_max_loss_per_contract(wing_width, p.credit) * p.qty
        total_stop += stop_risk
        total_structural += structural_risk
        if p.entry_date == today:
            same_day_stop += stop_risk
        if p.expiry == expiry:
            expiry_stop += stop_risk
    total_stop += foreign_exposure.get("stop", 0.0) or 0.0
    total_structural += foreign_exposure.get("structural", 0.0) or 0.0
    return same_day_stop, expiry_stop, total_stop, total_structural


def cap_to_budgets(qty, credit, wing_width, expiry, today, open_positions, mark_fn, risk_equity, f,
                   foreign_exposure=None):
    """
    SUPERSEDED -- DO NOT CALL FROM NEW CODE (post-v2 refinement Task 5).

    The live entry path uses budget_quantity_caps + gap_quantity_caps + entry_ceiling_cap fed into
    size_to_risk_limits. This function considers ONLY the four aggregate budgets: it knows nothing
    about the three gap-stress scenarios or the live per-entry contract ceiling, so calling it would
    silently bypass both. Retained only so existing tests keep documenting the original budget maths.
    Scheduled for removal once those tests are ported.
Reduce qty (down to 0) until adding this trade keeps every budget satisfied:
    same-day stop (f.max_same_day_stop_risk_pct), expiry stop (f.max_expiry_stop_risk_pct),
    total stop (f.max_total_stop_risk_pct), total structural (f.max_total_structural_risk_pct).
    Book risk is computed from open_positions: for each p, stop risk = remaining_stop_risk(
    p.credit, mark_fn(p), p.qty, f.expected_stop_slippage); structural = structural_max_loss_per_contract(
    wing_width, p.credit) * p.qty. 'same-day' = p.entry_date == today; 'expiry' = p.expiry == expiry.
    The proposed trade's per-contract stop = planned_stop_loss_per_contract(credit, wing_width,
    f.expected_stop_slippage); per-contract structural = structural_max_loss_per_contract(wing_width, credit).

    foreign_exposure (partner review v2 §2): optional {"stop":..., "structural":...} dollar-risk from
    SPY spreads at the broker that this bot did NOT open (see bot.portfolio.exposure.foreign_spy_exposure).
    Defaults to None -> treated as zero, so omitting it is byte-identical to before. Foreign exposure
    counts ONLY toward the TOTAL stop/structural budgets (this bot's own same-day/expiry budgets stay
    this-bot-only -- a foreign position isn't "this bot's" same-day or same-expiry risk).

    Returns a RiskBudgetResult (Priority-0 fix item 7) whose allowed_quantity is the largest
    0<=q<=qty that fits ALL budgets (0 if none fit), and whose limiting_budget names whichever of
    "same_day_stop"/"expiry_stop"/"total_stop"/"total_structural" first drove q below the requested
    qty (None when unconstrained). The result is int-compatible (__int__/__eq__/...), so every
    existing caller that used the old bare-int return keeps working unchanged."""
    if qty <= 0:
        return RiskBudgetResult(0, None, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    same_day_stop, expiry_stop, total_stop, total_structural = _book_exposures(
        wing_width, expiry, today, open_positions, mark_fn, f, foreign_exposure)

    per_contract_stop = planned_stop_loss_per_contract(credit, wing_width, f.expected_stop_slippage)
    per_contract_structural = structural_max_loss_per_contract(wing_width, credit)

    same_day_limit = risk_equity * f.max_same_day_stop_risk_pct
    expiry_limit = risk_equity * f.max_expiry_stop_risk_pct
    total_stop_limit = risk_equity * f.max_total_stop_risk_pct
    structural_limit = risk_equity * f.max_total_structural_risk_pct

    # Evaluate the four budgets in a fixed order and track whichever one FIRST forces q strictly
    # below the running value -- that's the "binding"/limiting budget for this candidate.
    budget_caps = (
        (SAME_DAY_STOP, _max_q_for_budget(qty, same_day_limit, same_day_stop, per_contract_stop)),
        (EXPIRY_STOP, _max_q_for_budget(qty, expiry_limit, expiry_stop, per_contract_stop)),
        (TOTAL_STOP, _max_q_for_budget(qty, total_stop_limit, total_stop, per_contract_stop)),
        (TOTAL_STRUCTURAL, _max_q_for_budget(qty, structural_limit, total_structural,
                                             per_contract_structural)),
    )
    q = qty
    limiting_budget = None
    for name, cap_q in budget_caps:
        if cap_q < q:
            q = cap_q
            limiting_budget = name
    q = max(0, q)

    return RiskBudgetResult(q, limiting_budget, same_day_stop, expiry_stop, total_stop,
                            total_structural, same_day_limit, expiry_limit, total_stop_limit,
                            structural_limit)


def budget_quantity_caps(credit, wing_width, expiry, today, open_positions, mark_fn, risk_equity, f,
                         foreign_exposure=None):
    """The four aggregate budgets, each as a QuantityCap (spec §3/§4) -- "how many contracts fit"
    rather than the single reduced qty cap_to_budgets returns. Book exposure is computed by the SAME
    _book_exposures helper cap_to_budgets uses (so they cannot diverge). Per-contract incremental
    risk = planned_stop_loss_per_contract for the three stop budgets and
    structural_max_loss_per_contract for the structural budget; limits = risk_equity * the matching
    f.max_*_pct. Foreign exposure counts only toward the total stop/structural caps.

    Order: same_day_stop, expiry_stop, total_stop, total_structural. This does NOT wire into the
    orchestrator and does NOT replace cap_to_budgets (a later task assembles caps via
    size_to_risk_limits)."""
    same_day_stop, expiry_stop, total_stop, total_structural = _book_exposures(
        wing_width, expiry, today, open_positions, mark_fn, f, foreign_exposure)

    per_contract_stop = planned_stop_loss_per_contract(credit, wing_width, f.expected_stop_slippage)
    per_contract_structural = structural_max_loss_per_contract(wing_width, credit)

    return [
        quantity_cap_from_budget(SAME_DAY_STOP, same_day_stop,
                                 risk_equity * f.max_same_day_stop_risk_pct, per_contract_stop),
        quantity_cap_from_budget(EXPIRY_STOP, expiry_stop,
                                 risk_equity * f.max_expiry_stop_risk_pct, per_contract_stop),
        quantity_cap_from_budget(TOTAL_STOP, total_stop,
                                 risk_equity * f.max_total_stop_risk_pct, per_contract_stop),
        quantity_cap_from_budget(TOTAL_STRUCTURAL, total_structural,
                                 risk_equity * f.max_total_structural_risk_pct,
                                 per_contract_structural),
    ]


@dataclass
class RiskSizingResult:
    """Outcome of assembling every risk QuantityCap (budgets + gap-stress scenarios) against a
    requested qty (spec §10). Rather than a bare reject, it REDUCES the request to fit the strictest
    cap and names the binding gate + its own maximum -- so a decision log can say exactly which rule
    limited the trade and by how much. allowed is True iff at least one contract survives."""
    requested_qty: int
    final_qty: int
    allowed: bool
    limiting_gate: Optional[str]
    limiting_quantity: Optional[int]
    caps: list
    reason: str


def size_to_risk_limits(requested_qty, caps):
    """Reduce requested_qty to fit ALL caps and name the strictest (spec §10).

    requested_qty <= 0 -> blocked with limiting_gate="requested_qty" (the quality-adjusted qty was
    already zero upstream). Otherwise final_qty = min(requested_qty, strictest cap's maximum_qty);
    the strictest cap (min by maximum_qty) is the limiting gate. allowed iff final_qty > 0."""
    if requested_qty <= 0:
        return RiskSizingResult(
            requested_qty=requested_qty,
            final_qty=0,
            allowed=False,
            limiting_gate="requested_qty",
            limiting_quantity=0,
            caps=caps,
            reason="quality-adjusted quantity is zero",
        )

    # No caps -> unconstrained (never call min() on an empty list). final_qty stays the request.
    if not caps:
        return RiskSizingResult(
            requested_qty=requested_qty,
            final_qty=requested_qty,
            allowed=requested_qty > 0,
            limiting_gate=None,
            limiting_quantity=None,
            caps=caps,
            reason="ok",
        )

    limiting_cap = min(caps, key=lambda cap: cap.maximum_qty)
    final_qty = min(requested_qty, limiting_cap.maximum_qty)
    allowed = final_qty > 0
    return RiskSizingResult(
        requested_qty=requested_qty,
        final_qty=final_qty,
        allowed=allowed,
        limiting_gate=limiting_cap.name,
        limiting_quantity=limiting_cap.maximum_qty,
        caps=caps,
        reason="ok" if allowed else f"{limiting_cap.name} permits zero contracts",
    )


class DecisionOutcome(str, Enum):
    """The distinct entry-decision outcomes (spec §11), logged so a report can distinguish "reduced
    but taken" from the several block reasons instead of a single opaque "blocked". The credit /
    transaction-cost / quote-quality / duplicate / regime blocks are decided by their own upstream
    gates and passed in by the caller; classify_outcome below only covers the risk-sizing trio."""
    ALLOWED_FULL = "allowed_full"
    ALLOWED_REDUCED = "allowed_reduced"
    BLOCKED_ZERO_CAPACITY = "blocked_zero_capacity"
    BLOCKED_CREDIT_QUALITY = "blocked_credit_quality"
    BLOCKED_TRANSACTION_COST = "blocked_transaction_cost"
    BLOCKED_QUOTE_QUALITY = "blocked_quote_quality"
    BLOCKED_DUPLICATE = "blocked_duplicate"
    BLOCKED_REGIME = "blocked_regime"


def classify_outcome(requested_qty, final_qty, quality_maximum_qty=None):
    """Map a risk-sizing (requested, final) pair to a DecisionOutcome (spec §11):
    allowed_full (final == requested), allowed_reduced (0 < final < requested), or
    blocked_zero_capacity (final == 0). The credit/cost/quote/duplicate/regime blocks are decided by
    their own upstream gates -- this helper covers ONLY the risk-sizing outcomes. quality_maximum_qty
    is accepted for caller symmetry but does not change this classification."""
    if final_qty <= 0:
        return DecisionOutcome.BLOCKED_ZERO_CAPACITY
    if final_qty >= requested_qty:
        return DecisionOutcome.ALLOWED_FULL
    return DecisionOutcome.ALLOWED_REDUCED


def entry_ceiling_cap(max_entry_qty):
    """Advisor directive (botc.txt): a hard per-entry contract ceiling for a controlled live release,
    expressed as one more QuantityCap.

    Modelling it as a cap rather than a post-hoc clamp means it flows through the same min()-of-caps
    sizing and names itself in the limiting_gate telemetry when it binds -- and, critically, it can
    only ever REDUCE a quantity. A min() cannot raise a zero, so the directive's "do not force one
    contract when the risk-approved quantity is zero" holds by construction rather than by a
    separate guard someone could later forget.

    Returns None when no ceiling is configured (Bot B), so the caller can simply filter it out."""
    if max_entry_qty is None:
        return None
    return QuantityCap(
        name="live_entry_ceiling",
        maximum_qty=int(max_entry_qty),
        current_exposure=0.0,
        limit=float(max_entry_qty),
        remaining_capacity=float(max_entry_qty),
        incremental_risk_per_contract=1.0,
    )
