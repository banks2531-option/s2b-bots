# S2B All-Days — Post-v2 Refinement Spec (verbatim, user-provided 2026-07-17)

> Source: uploaded Word doc. Exact instructions to implement for Bot B (sandbox) + Bot C (live).

S2B All-Days Bot - Post-v2 Refinement Instructions
Comprehensive implementation instructions (excluding inherited-position and migration-mode logic).
Below are the specific implementation instructions, excluding all inherited-position or migration-mode logic.
The goal is to preserve the current v2 protections while preventing valid trades from being rejected simply because the initially requested quantity is too large. The post-v2 report showed that aggregate-risk and gap-stress gates caused most entry blocks, so these changes should focus on quantity reduction, transparency, and fallback evaluation, not weakening the credit rules.
1. Keep the current credit rules unchanged
Do not alter these thresholds yet:
ABSOLUTE_CREDIT_FLOOR = 0.08
STANDARD_PROBE_FLOOR = 0.10
FULL_SIZE_CREDIT_FLOOR = 0.115

LOW_CREDIT_MAX_QTY = 1
PROBE_SIZE_MULTIPLIER = 0.40
The qualification lanes should remain:
def classify_credit_quality(
    credit_ratio: float,
    adaptive_full_size_threshold: float,
) -> tuple[str, float, int | None]:
    """
    Returns:
        tier,
        size_multiplier,
        maximum_quantity
    """

    if credit_ratio < ABSOLUTE_CREDIT_FLOOR:
        return "reject", 0.0, 0

    if credit_ratio < STANDARD_PROBE_FLOOR:
        return "low_credit_safety", 0.25, LOW_CREDIT_MAX_QTY

    if credit_ratio < adaptive_full_size_threshold:
        return "probe", PROBE_SIZE_MULTIPLIER, None

    return "full", 1.0, None
Low-credit entries must still pass their additional safety conditions:
low_credit_safety_pass = (
    target_to_cost_ratio >= 4.5
    and package_width_ratio <= 0.20
    and quote_age_seconds <= 2.0
    and cushion_atr >= 1.15
    and (
        expected_move_cushion >= 0.90
        or short_delta <= 0.30
    )
    and not defensive_market_state
)
Do not let the risk-sizing changes bypass those entry-quality conditions.
2. Fix probe quantity rounding
The current sizing can produce:
Base quantity: 2
Probe multiplier: 40%
floor(2 × 0.40) = 0
That unintentionally turns the probe tier into a rejection tier.
Replace the quality-sizing function with:
import math


def apply_quality_multiplier(
    base_qty: int,
    multiplier: float,
    maximum_qty: int | None = None,
) -> int:
    if base_qty <= 0 or multiplier <= 0:
        return 0

    if multiplier < 1.0:
        adjusted_qty = max(
            1,
            math.floor(base_qty * multiplier),
        )
    else:
        adjusted_qty = math.floor(base_qty * multiplier)

    if maximum_qty is not None:
        adjusted_qty = min(adjusted_qty, maximum_qty)

    return adjusted_qty
Important:
This only guarantees one candidate contract.
That candidate must still pass every hard risk limit.
Never force one contract when one contract exceeds the portfolio budget.
Never retain the old global minimum 1 contract behavior before risk checks.
Correct sequence:
base_qty = calculate_base_quantity(...)
quality_qty = apply_quality_multiplier(
    base_qty=base_qty,
    multiplier=quality_multiplier,
    maximum_qty=quality_maximum_qty,
)

final_qty = cap_quantity_to_all_risk_limits(
    requested_qty=quality_qty,
    ...
)
If `final_qty == 0`, reject.
3. Convert every risk gate into a quantity cap
Do not let any portfolio-risk function return only `True` or `False`.
Every gate should calculate:
What is the largest number of contracts that can safely fit?
Create:
from dataclasses import dataclass


@dataclass
class QuantityCap:
    name: str
    maximum_qty: int

    current_exposure: float
    limit: float
    remaining_capacity: float
    incremental_risk_per_contract: float
Each risk category returns a `QuantityCap`.
The final quantity is:
final_qty = min(
    requested_qty,
    same_day_cap.maximum_qty,
    expiry_cap.maximum_qty,
    total_stop_cap.maximum_qty,
    structural_cap.maximum_qty,
    gap_1atr_cap.maximum_qty,
    gap_1_5atr_cap.maximum_qty,
    gap_2atr_cap.maximum_qty,
)
Reject only when:
if final_qty <= 0:
    reject_trade(...)
4. Implement same-day stop-risk quantity sizing
Calculate the proposed trade’s planned loss at the configured stop:
def planned_stop_loss_per_contract(
    expected_credit: float,
    stop_loss_multiple: float,
    expected_stop_slippage: float,
    structural_max_loss: float,
) -> float:
    modeled_stop_loss = (
        stop_loss_multiple * expected_credit
        + expected_stop_slippage
    ) * 100

    return min(
        structural_max_loss,
        modeled_stop_loss,
    )
For the current stop at a loss of 2× credit:
STOP_LOSS_MULTIPLE = 2.0
EXPECTED_STOP_SLIPPAGE = 0.10
Same-day capacity:
def quantity_cap_from_budget(
    name: str,
    current_exposure: float,
    limit: float,
    incremental_risk_per_contract: float,
) -> QuantityCap:
    remaining = max(0.0, limit - current_exposure)

    if incremental_risk_per_contract <= 0:
        qty = 0
    else:
        qty = int(
            remaining // incremental_risk_per_contract
        )

    return QuantityCap(
        name=name,
        maximum_qty=max(0, qty),
        current_exposure=current_exposure,
        limit=limit,
        remaining_capacity=remaining,
        incremental_risk_per_contract=incremental_risk_per_contract,
    )
Use:
same_day_cap = quantity_cap_from_budget(
    name="same_day_stop",
    current_exposure=current_same_day_stop_risk,
    limit=risk_equity * MAX_SAME_DAY_STOP_RISK_PCT,
    incremental_risk_per_contract=planned_stop_loss,
)
5. Implement expiration stop-risk quantity sizing
For every open position in the same expiration, calculate remaining loss from its current mark to its planned stop.
def remaining_stop_risk(
    entry_credit: float,
    current_debit: float,
    qty: int,
    stop_loss_multiple: float,
    expected_stop_slippage: float,
    structural_max_loss_per_contract: float,
) -> float:
    stop_debit = entry_credit * (1 + stop_loss_multiple)

    remaining_per_contract = max(
        0.0,
        (
            stop_debit
            - current_debit
            + expected_stop_slippage
        ) * 100,
    )

    remaining_per_contract = min(
        remaining_per_contract,
        structural_max_loss_per_contract,
    )

    return remaining_per_contract * qty
Then:
expiry_cap = quantity_cap_from_budget(
    name="expiry_stop",
    current_exposure=current_expiry_remaining_stop_risk,
    limit=risk_equity * MAX_EXPIRY_STOP_RISK_PCT,
    incremental_risk_per_contract=planned_stop_loss,
)
Do not double-count current unrealized loss as future remaining stop risk. Track current P&L separately.
6. Implement total-book stop-risk quantity sizing
total_stop_cap = quantity_cap_from_budget(
    name="total_stop",
    current_exposure=current_total_remaining_stop_risk,
    limit=risk_equity * MAX_TOTAL_STOP_RISK_PCT,
    incremental_risk_per_contract=planned_stop_loss,
)
Current starting limit:
MAX_TOTAL_STOP_RISK_PCT = 0.04
Do not change the percentage yet.
7. Implement structural-risk quantity sizing
Structural maximum loss per contract:
structural_max_loss_per_contract = (
    wing_width - expected_executable_credit
) * 100
Then:
structural_cap = quantity_cap_from_budget(
    name="total_structural",
    current_exposure=current_total_structural_risk,
    limit=risk_equity * MAX_TOTAL_STRUCTURAL_RISK_PCT,
    incremental_risk_per_contract=structural_max_loss_per_contract,
)
Keep:
MAX_TOTAL_STRUCTURAL_RISK_PCT = 0.15
Do not derive this limit from `max_open`.
8. Convert gap stress into an incremental quantity model
This is one of the most important changes.
The gap engine should calculate two values:
Current book stressed loss
Current book plus one candidate contract stressed loss
The incremental loss of one candidate contract is:
incremental_gap_loss_per_contract = max(
    0.0,
    stressed_loss_with_one_candidate
    - current_book_stressed_loss,
)
Quantity allowed:
def gap_quantity_cap(
    scenario_name: str,
    current_book_loss: float,
    one_contract_book_loss: float,
    loss_limit: float,
) -> QuantityCap:
    incremental = max(
        0.0,
        one_contract_book_loss - current_book_loss,
    )

    remaining = max(
        0.0,
        loss_limit - current_book_loss,
    )

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
Run separately for:
−1.0 ATR
−1.5 ATR
−2.0 ATR
Example:
gap_1atr_cap = gap_quantity_cap(
    scenario_name="gap_1atr",
    current_book_loss=current_gap_loss_1atr,
    one_contract_book_loss=gap_loss_1atr_with_candidate,
    loss_limit=risk_equity * GAP_1ATR_LIMIT_PCT,
)

gap_1_5atr_cap = gap_quantity_cap(
    scenario_name="gap_1_5atr",
    current_book_loss=current_gap_loss_1_5atr,
    one_contract_book_loss=gap_loss_1_5atr_with_candidate,
    loss_limit=risk_equity * MAX_GAP_STRESS_LOSS_PCT,
)

gap_2atr_cap = gap_quantity_cap(
    scenario_name="gap_2atr",
    current_book_loss=current_gap_loss_2atr,
    one_contract_book_loss=gap_loss_2atr_with_candidate,
    loss_limit=risk_equity * GAP_2ATR_LIMIT_PCT,
)
The bot should reject only when the strictest scenario permits zero contracts.
9. Use realistic option repricing in gap stress
Do not use intrinsic value alone.
At each scenario, reprice both legs using either:
Black-Scholes with current implied volatility and an IV shock,
Delta-gamma-vega approximation,
Or historical quote replay.
Minimum scenario matrix:
Underlying move:
−1.0 ATR
−1.5 ATR
−2.0 ATR

IV shock:
+3 volatility points
+5 volatility points
+10 volatility points
For each underlying move, use the appropriate IV shock:
STRESS_SCENARIOS = [
    StressScenario(
        name="down_1atr",
        atr_move=-1.0,
        iv_point_change=3.0,
    ),
    StressScenario(
        name="down_1_5atr",
        atr_move=-1.5,
        iv_point_change=5.0,
    ),
    StressScenario(
        name="down_2atr",
        atr_move=-2.0,
        iv_point_change=10.0,
    ),
]
Add stressed execution slippage:
STRESSED_CLOSE_SLIPPAGE = 0.10
Stressed spread loss:
stressed_loss = max(
    0.0,
    (
        stressed_close_debit
        + STRESSED_CLOSE_SLIPPAGE
        - entry_credit
    ) * 100 * qty,
)
Cap at structural maximum loss.
10. Return a complete risk-budget result
Create:
@dataclass
class RiskSizingResult:
    requested_qty: int
    final_qty: int
    allowed: bool

    limiting_gate: str | None
    limiting_quantity: int | None
    caps: list[QuantityCap]

    reason: str
Implementation:
def size_to_risk_limits(
    requested_qty: int,
    caps: list[QuantityCap],
) -> RiskSizingResult:
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

    limiting_cap = min(
        caps,
        key=lambda cap: cap.maximum_qty,
    )

    final_qty = min(
        requested_qty,
        limiting_cap.maximum_qty,
    )

    allowed = final_qty > 0

    return RiskSizingResult(
        requested_qty=requested_qty,
        final_qty=final_qty,
        allowed=allowed,
        limiting_gate=limiting_cap.name,
        limiting_quantity=limiting_cap.maximum_qty,
        caps=caps,
        reason=(
            "ok"
            if allowed
            else f"{limiting_cap.name} permits zero contracts"
        ),
    )
11. Record the exact limiting rule
Do not write only:
risk_budget
Log:
{
  "decision": "blocked",
  "limiting_gate": "gap_1_5atr",
  "requested_qty": 2,
  "quality_adjusted_qty": 1,
  "final_qty": 0,
  "current_exposure": 4210.0,
  "limit": 4320.0,
  "remaining_capacity": 110.0,
  "incremental_risk_per_contract": 285.0
}
For a reduced but accepted trade:
{
  "decision": "allowed_reduced",
  "limiting_gate": "expiry_stop",
  "requested_qty": 4,
  "quality_adjusted_qty": 2,
  "final_qty": 1
}
Create distinct decision outcomes:
allowed_full
allowed_reduced
blocked_zero_capacity
blocked_credit_quality
blocked_transaction_cost
blocked_quote_quality
blocked_duplicate
blocked_regime
12. Deduplicate candidate signals
Do not count every 60-second polling cycle as a distinct opportunity.
Create:
def candidate_key(
    trading_date: str,
    expiry: str,
    short_strike: float,
    long_strike: float,
    timestamp,
) -> tuple:
    minute_bucket = (
        timestamp.hour,
        timestamp.minute // 15,
    )

    return (
        trading_date,
        expiry,
        round(short_strike, 3),
        round(long_strike, 3),
        minute_bucket,
    )
Maintain:
seen_candidate_keys: set[tuple]
A candidate is considered new when:
Its strike pair changes,
Expiration changes,
A new 15-minute bucket begins,
Or credit ratio changes by at least 0.005.
Example:
CREDIT_RATIO_REEVALUATION_CHANGE = 0.005
Store two sets of statistics:
Raw polling evaluations
Unique candidate opportunities
Do not use raw polling evaluations in profitability reports.
13. Deduplicate adaptive credit-history observations
The rolling credit threshold must not receive the same candidate every minute.
Only append to the rolling history when:
should_record_credit_observation = (
    new_candidate_key
    or abs(
        current_credit_ratio
        - last_recorded_credit_ratio_for_key
    ) >= 0.005
)
Store:
@dataclass
class CreditObservation:
    timestamp: str
    expiry: str
    dte_bucket: str
    short_strike: float
    long_strike: float
    expected_executable_credit: float
    credit_ratio: float
    candidate_key: tuple
Only prior observations may be used to calculate the current threshold.
14. Add alternate-expiration evaluation
Evaluate up to three eligible expirations instead of repeatedly testing only the nearest expiration.
candidate_expirations = get_next_eligible_expirations(
    available_expirations=available_expirations,
    today=today,
    minimum_dte=4,
    count=3,
)
For each expiration:
Build the best valid spread.
Calculate credit quality.
Calculate target-to-cost ratio.
Calculate all risk quantity caps.
Calculate final permitted quantity.
Calculate expected net profit per permitted contract.
Create:
@dataclass
class CandidateEvaluation:
    expiry: str
    dte: int
    short_strike: float
    long_strike: float
    expected_credit: float
    credit_ratio: float
    target_to_cost_ratio: float
    requested_qty: int
    final_qty: int
    expected_target_profit: float
    risk_result: RiskSizingResult
Selection rule:
eligible_candidates = [
    candidate
    for candidate in evaluations
    if candidate.final_qty > 0
]

if not eligible_candidates:
    return "no_candidate_fits"

selected = max(
    eligible_candidates,
    key=lambda c: (
        c.expected_target_profit,
        c.credit_ratio,
        -c.dte,
    ),
)
Do not allow a later expiration to bypass credit, cost, trend or regime rules.
15. Add a $5-wing fallback in shadow first
Do not immediately trade the $5 wing automatically.
For every otherwise valid $10-wide candidate that is reduced to zero by risk limits, construct a $5-wide alternative using the same short strike:
fallback_candidate = build_spread_for_short_strike(
    short_strike=original.short_strike,
    wing_width=5.0,
)
Evaluate:
Credit ratio
Target-to-cost ratio
Expected target profit
Structural maximum loss
Planned stop loss
Incremental gap risk
Bid/ask width
Final permissible quantity
Log:
Would the $5 wing have qualified?
What quantity?
What eventual markout?
Feature flags:
ENABLE_FIVE_DOLLAR_WING_SHADOW = True
ENABLE_FIVE_DOLLAR_WING_LIVE = False
Only activate after sufficient testing shows:
Positive cost-adjusted expectancy,
Acceptable liquidity,
Better risk-adjusted return than the $10 wing,
No material increase in fill failures.
16. Add separate operational states
Create:
class EntryState(str, Enum):
    ACTIVE = "active"
    NO_QUALIFIED_CREDIT = "no_qualified_credit"
    NO_RISK_CAPACITY = "no_risk_capacity"
    NO_GAP_CAPACITY = "no_gap_capacity"
    NO_VALID_QUOTES = "no_valid_quotes"
    NO_VALID_EXPIRATION = "no_valid_expiration"
    RISK_OFF = "risk_off"
    DATA_FAILURE = "data_failure"
Report the final state once per unique candidate or state transition—not every polling cycle.
17. Required daily report changes
Every report should contain:
Raw polling cycles
Unique candidate opportunities
Unique strike pairs
Unique expirations evaluated

Candidates rejected by:
- Credit floor
- Low-credit safety conditions
- Transaction-cost gate
- Quote/liquidity gate
- Regime gate
- Same-day risk
- Expiration risk
- Total stop risk
- Structural risk
- 1.0-ATR gap stress
- 1.5-ATR gap stress
- 2.0-ATR gap stress

Candidates:
- Allowed at requested quantity
- Allowed at reduced quantity
- Allowed at one-contract probe size
- Blocked at zero quantity
Include hypothetical fallback results:
Later-expiration candidate available
$5-wing candidate available
Balanced risk profile would allow
18. Add unit tests
Your friend should add at least these tests.
Probe sizing
def test_probe_size_never_rounds_valid_base_qty_to_zero():
    assert apply_quality_multiplier(
        base_qty=2,
        multiplier=0.40,
    ) == 1
Hard risk still wins
def test_probe_minimum_does_not_override_zero_risk_capacity():
    requested = apply_quality_multiplier(2, 0.40)
    cap = QuantityCap(
        name="gap_1_5atr",
        maximum_qty=0,
        current_exposure=4300,
        limit=4320,
        remaining_capacity=20,
        incremental_risk_per_contract=250,
    )

    result = size_to_risk_limits(
        requested_qty=requested,
        caps=[cap],
    )

    assert result.final_qty == 0
    assert not result.allowed
Quantity reduction
def test_oversized_order_is_reduced_not_rejected():
    caps = [
        QuantityCap(
            name="expiry_stop",
            maximum_qty=1,
            current_exposure=1800,
            limit=2160,
            remaining_capacity=360,
            incremental_risk_per_contract=250,
        ),
        QuantityCap(
            name="total_stop",
            maximum_qty=4,
            current_exposure=1000,
            limit=2880,
            remaining_capacity=1880,
            incremental_risk_per_contract=250,
        ),
    ]

    result = size_to_risk_limits(
        requested_qty=3,
        caps=caps,
    )

    assert result.allowed
    assert result.final_qty == 1
    assert result.limiting_gate == "expiry_stop"
Incremental gap capacity
def test_gap_gate_uses_incremental_candidate_loss():
    cap = gap_quantity_cap(
        scenario_name="gap_1_5atr",
        current_book_loss=3000,
        one_contract_book_loss=3250,
        loss_limit=3600,
    )

    assert cap.incremental_risk_per_contract == 250
    assert cap.maximum_qty == 2
Candidate deduplication
def test_same_signal_same_bucket_is_not_counted_twice():
    key1 = candidate_key(
        "2026-07-20",
        "2026-07-31",
        745,
        735,
        timestamp_10_01,
    )

    key2 = candidate_key(
        "2026-07-20",
        "2026-07-31",
        745,
        735,
        timestamp_10_08,
    )

    assert key1 == key2
Alternate expiration
def test_later_expiration_selected_when_nearest_has_zero_capacity():
    nearest.final_qty = 0
    next_expiry.final_qty = 1
    next_expiry.expected_target_profit = 45

    selected = select_best_candidate(
        [nearest, next_expiry]
    )

    assert selected is next_expiry
Recommended rollout
Activate immediately in sandbox
One-contract probe-floor fix
Quantity-based risk caps
Incremental gap-risk sizing
Exact limiting-gate telemetry
Candidate-signal deduplication
Adaptive-history deduplication
Alternate-expiration evaluation
Shadow only
$5-wing fallback
Slightly wider risk-profile comparison
Bearish alternative module
Do not loosen yet
8% absolute credit floor
11.5% full-size floor
4.5× low-credit target-to-cost requirement
15% total structural limit
Current hard stop
Trend gate
After these updates, the bot will still refuse genuinely unsafe trades, but it will no longer treat “the requested size does not fit” as equivalent to “no version of this trade can fit.”