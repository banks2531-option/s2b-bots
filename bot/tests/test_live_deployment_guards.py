"""Bot C controlled-deployment safeguards (advisor directive `botc.txt`, 2026-07-19).

Bot C is the REAL-MONEY account. The advisor's revised position is to deploy the completed T1-T7
work immediately rather than wait five sandbox sessions, but under a hard temporary ceiling so an
undiscovered implementation fault costs one contract rather than a full-size position.

    live_final_qty = min(risk_approved_qty, BOT_C_MAX_ENTRY_QTY)

Implemented as one more QuantityCap rather than a post-hoc clamp, so it flows through the same
min()-of-caps sizing and names itself in the limiting_gate telemetry when it binds. That also makes
the "do not force one contract when the risk-approved quantity is zero" rule automatic: a min()
cannot raise a zero.
"""
import pytest

from bot.features import S2bFeatures, ConfigurationError, validate_live_config
from bot.portfolio.risk_budget import QuantityCap, size_to_risk_limits, entry_ceiling_cap


# ── the ceiling behaves as a cap, never as a floor ───────────────────────────────────────────────

def test_ceiling_reduces_an_approved_quantity():
    caps = [QuantityCap("expiry_stop", 5, 100.0, 200.0, 100.0, 20.0), entry_ceiling_cap(1)]
    result = size_to_risk_limits(5, caps)
    assert result.final_qty == 1
    assert result.limiting_gate == "live_entry_ceiling"


def test_ceiling_never_raises_a_zero_capacity_decision():
    """The advisor's explicit instruction: do NOT force one contract when the risk-approved quantity
    is zero. A full book must still reject, ceiling or no ceiling."""
    caps = [QuantityCap("expiry_stop", 0, 200.0, 200.0, 0.0, 20.0), entry_ceiling_cap(1)]
    result = size_to_risk_limits(5, caps)
    assert result.final_qty == 0
    assert result.limiting_gate == "expiry_stop"


def test_ceiling_does_not_bind_when_risk_already_permits_less():
    caps = [QuantityCap("gap_1_5atr", 1, 0.0, 500.0, 500.0, 400.0), entry_ceiling_cap(1)]
    result = size_to_risk_limits(3, caps)
    assert result.final_qty == 1
    assert result.limiting_gate in ("gap_1_5atr", "live_entry_ceiling")


def test_no_ceiling_configured_means_no_cap():
    assert entry_ceiling_cap(None) is None


# ── startup validation: refuse to start rather than trade a misunderstood config ─────────────────

def _live_like(**over):
    """A Bot-C-shaped config: its OWN calibrated caps, not the big-account defaults."""
    base = dict(credit_tiers=True, transaction_cost_gate=True, aggregate_risk_budget=True,
                max_entry_qty=1, enable_low_credit_08_to_10=False,
                max_total_structural_risk_pct=0.50, max_trade_structural_risk_pct=0.50,
                max_gap_stress_loss_pct=0.50)
    base.update(over)
    return S2bFeatures(**base)


def test_bot_c_shaped_config_validates():
    validate_live_config(_live_like(), live=True)


def test_live_requires_the_one_contract_ceiling():
    with pytest.raises(ConfigurationError, match="max_entry_qty"):
        validate_live_config(_live_like(max_entry_qty=None), live=True)
    with pytest.raises(ConfigurationError, match="max_entry_qty"):
        validate_live_config(_live_like(max_entry_qty=3), live=True)


def test_live_refuses_the_five_wide_and_bearish_features():
    with pytest.raises(ConfigurationError, match="five_wide"):
        validate_live_config(_live_like(enable_five_wide_live=True), live=True)
    with pytest.raises(ConfigurationError, match="bearish"):
        validate_live_config(_live_like(bearish_module=True), live=True)


def test_live_refuses_the_low_credit_lane_until_its_inputs_are_field_verified():
    """The 8-10% lane is code-complete and unit-tested, but its inputs depend on live feed fields
    (Tradier bid_date/ask_date and greeks.mid_iv) that have NOT been verified against real broker
    responses. Until they have, the lane stays off in live."""
    with pytest.raises(ConfigurationError, match="low_credit"):
        validate_live_config(_live_like(enable_low_credit_08_to_10=True), live=True)


def test_credit_floors_are_pinned():
    """These are module constants, not config -- the assertion guards against a future edit."""
    from bot.strategy.credit_quality import ABSOLUTE_CREDIT_FLOOR, FULL_SIZE_CREDIT_FLOOR
    assert ABSOLUTE_CREDIT_FLOOR == 0.08
    assert FULL_SIZE_CREDIT_FLOOR == 0.115
    validate_live_config(_live_like(), live=True)


def test_structural_limit_is_checked_against_bot_cs_own_calibration_not_the_default():
    """DEVIATION from the directive's `assert MAX_TOTAL_STRUCTURAL_RISK_PCT == 0.15`.

    Bot C does not run 0.15. It runs 0.50, set deliberately on 2026-07-17 because a single $5-wing
    spread is ~49% of its ~$842 account and the big-account default sized every trade to zero. The
    literal assertion would refuse to start Bot C.

    The directive's INTENT is "this deployment must not loosen risk limits", so the check is that the
    value matches what Bot C already runs -- a tightening or loosening from its deployed calibration
    is what should fail."""
    validate_live_config(_live_like(max_total_structural_risk_pct=0.50), live=True,
                         expected_structural_pct=0.50)
    with pytest.raises(ConfigurationError, match="structural"):
        validate_live_config(_live_like(max_total_structural_risk_pct=0.60), live=True,
                             expected_structural_pct=0.50)


def test_non_live_config_is_not_subject_to_the_live_ceiling():
    """Bot B (sandbox) must not inherit Bot C's one-contract ceiling -- it is validating full sizing."""
    validate_live_config(S2bFeatures(aggregate_risk_budget=True), live=False)


# ── the low-credit lane flag actually gates the lane ─────────────────────────────────────────────

from datetime import datetime
from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState


def _thin_chain(ts=None):
    """credit 0.90 on a $10 wing -> ratio 0.09, inside the 8-10% low-credit band.

    Timestamps are supplied so the lane's freshness condition PASSES -- otherwise the lane would fail
    closed on missing quote age and the flag test below would pass for the wrong reason."""
    ts = ts if ts is not None else NOW
    return [OptionQuote(570.0, 0.45, 5.00, 5.10, exchange_timestamp=ts, iv=0.12),
            OptionQuote(560.0, 0.28, 1.40, 1.50, exchange_timestamp=ts, iv=0.12),
            OptionQuote(550.0, 0.18, 0.50, 0.60, exchange_timestamp=ts, iv=0.12)]


def _deps(features, **over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _thin_chain(),
        pick_expiry=lambda today: "2026-07-24",
        get_vix_regime=lambda: (0.5, 0.01),
        account_state=lambda today, conc: AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today),
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 4,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 100_000.0,
        bot_equity=lambda: 100_000.0, alert_sink=lambda alerts: None,
        risk_equity=lambda: 100_000.0, features=features,
    )
    base.update(over)
    return Deps(**base)


NOW = datetime(2026, 7, 20, 10, 5)


def test_low_credit_lane_enabled_evaluates_the_lane():
    """Control for the test below: with the lane ON, the candidate reaches the safety gate and its
    inputs are populated -- proving the rejection below comes from the FLAG, not from the lane
    failing closed on missing data."""
    f = S2bFeatures(credit_tiers=True, decision_logging=True, enable_low_credit_08_to_10=True)
    logged = []
    run_entry_cycle(BotState(), _deps(f, trade_log=lambda r: logged.append(r)), NOW)
    rec = next(r for r in logged if r.get("event") == "DECISION")
    assert rec["lc_quote_fresh"] is True            # the lane really was evaluated
    assert rec["lc_safety_pass"] in (True, False)


def test_low_credit_lane_disabled_rejects_the_thin_candidate_outright():
    f = S2bFeatures(credit_tiers=True, decision_logging=True, enable_low_credit_08_to_10=False)
    logged = []
    state, info = run_entry_cycle(BotState(), _deps(f, trade_log=lambda r: logged.append(r)), NOW)
    assert info == "credit_too_low"
    assert state.open_positions == []
    rec = next(r for r in logged if r.get("event") == "DECISION")
    assert rec["decision_outcome"] == "blocked_credit_quality"
    assert "lc_safety_pass" not in rec              # lane never evaluated at all


def test_disabling_the_low_credit_lane_leaves_the_normal_probe_and_full_lanes_intact():
    """Turning the 8-10% exception off must NOT disturb the regular 10%+ lanes -- the advisor was
    explicit that this only postpones the new exception."""
    from bot.strategy.credit_quality import classify_credit_quality
    assert classify_credit_quality(0.12, 0.115)[0] == "full"
    assert classify_credit_quality(0.105, 0.115)[0] == "probe"
    f = S2bFeatures(credit_tiers=True, enable_low_credit_08_to_10=False)
    chain = [OptionQuote(570.0, 0.45, 5.00, 5.10, exchange_timestamp=NOW, iv=0.12),
             OptionQuote(560.0, 0.28, 2.60, 2.70, exchange_timestamp=NOW, iv=0.12),  # ratio 0.125 = full
             OptionQuote(550.0, 0.18, 0.50, 0.60, exchange_timestamp=NOW, iv=0.12)]
    state, info = run_entry_cycle(BotState(), _deps(f, get_chain=lambda s, e: chain), NOW)
    assert info == "filled"


def test_bot_c_declares_one_position_and_one_entry_per_day():
    """Advisor nextsteps2 section 2: Bot C's controlled pilot is ONE spread, ONE entry per day.
    The aggregate risk budget blocks a second position anyway, but the declared ceiling must not
    disagree with the intended one -- a reader (or a future risk-cap recalibration) would take
    max_open=3 at face value."""
    import inspect
    from bot.app import run_s2b_live
    src = inspect.getsource(run_s2b_live.main)
    assert "max_open=1" in src, "Bot C must declare max_open=1"
    assert "max_entries_per_day=1" in src, "Bot C must declare max_entries_per_day=1"
