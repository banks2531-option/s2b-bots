"""Advisor Step 2 (2A/2B/2C/2D): make Black-Scholes gap enforcement explicit, comparable and
verifiable.

The measured tightening is the branch's largest operational uncertainty: in a representative case
the intrinsic model permitted 2 contracts where Black-Scholes permits 0, because intrinsic ignores
time value, the scenario IV shock and the stressed-close slippage. The response is NOT to loosen the
budgets -- it is to (a) stop pretending a config flag chooses the model, (b) log both models side by
side during Bot B validation so the difference is measured rather than argued, and (c) guard and
expose the BS inputs so a suspicious number can be traced to a leg.
"""
import pytest

from bot.features import S2bFeatures, ConfigurationError, validate_gap_stress_config
from bot.portfolio.gap_stress import (gap_quantity_caps, gap_model_comparison,
                                       stressed_spread_loss_bs, stressed_spread_loss_bs_detail,
                                       gap_stress_losses)
from bot.strategy.manage import ManagedPosition


def _pos(short=568.0, long_=558.0, credit=2.0, qty=1, expiry="2026-06-19"):
    return ManagedPosition("SPY", short, long_, credit, qty, expiry, entry_date="2026-06-15")


IV = lambda p: (0.20, 0.20)
TODAY = "2026-06-15"
SPOT, ATR = 575.0, 6.0


# ── 2A: the retired flag cannot come back, and enforcement must be Black-Scholes ─────────────────

def test_default_config_is_valid():
    validate_gap_stress_config(S2bFeatures())


def test_non_black_scholes_enforcement_is_rejected_at_startup():
    f = S2bFeatures()
    f.gap_stress_enforcement_model = "intrinsic"
    with pytest.raises(ConfigurationError, match="must use Black-Scholes"):
        validate_gap_stress_config(f)


def test_retired_gap_stress_model_flag_is_rejected_outright():
    """A leftover `gap_stress_model` in a config file would sit there looking meaningful while
    controlling nothing -- the exact trap Step 2A exists to close. Refuse to start instead."""
    f = S2bFeatures()
    f.gap_stress_model = "intrinsic"
    with pytest.raises(ConfigurationError, match="retired"):
        validate_gap_stress_config(f)


def test_s2b_features_no_longer_accepts_the_retired_flag():
    with pytest.raises(TypeError):
        S2bFeatures(gap_stress_model="bs")


# ── 2B: both models logged, Black-Scholes enforcing ──────────────────────────────────────────────

def test_comparison_reports_both_models_and_the_quantity_difference():
    # credit 0.20 (not the 2.00 default) so the INTRINSIC stressed loss is positive and both models
    # yield a real, comparable quantity -- at a $2.00 credit intrinsic shows no loss at all.
    f = S2bFeatures(max_gap_stress_loss_pct=0.02)
    cmp = gap_model_comparison([], _pos(credit=0.20), SPOT, ATR, f, 100_000.0, iv_fn=IV, today=TODAY)
    assert cmp["bs_incremental_1_5"] > cmp["intrinsic_incremental_1_5"]
    assert cmp["bs_qty_1_5"] <= cmp["intrinsic_qty_1_5"]
    assert cmp["qty_difference_1_5"] == cmp["intrinsic_qty_1_5"] - cmp["bs_qty_1_5"]
    assert cmp["enforced_model"] == "black_scholes"


def test_comparison_flags_the_candidates_black_scholes_zeroes_out():
    """The number the advisor most wants from Bot B: how often BS turns a tradeable candidate into
    an untradeable one. Budget tuned so intrinsic permits contracts and BS permits none."""
    f = S2bFeatures(max_gap_stress_loss_pct=0.005)     # $500
    cmp = gap_model_comparison([], _pos(credit=0.20), SPOT, ATR, f, 100_000.0, iv_fn=IV, today=TODAY)
    assert cmp["intrinsic_qty_1_5"] > 0
    assert cmp["bs_qty_1_5"] == 0
    assert cmp["bs_zeroed_the_candidate"] is True


def test_comparison_does_not_flag_when_both_models_permit_size():
    f = S2bFeatures(max_gap_stress_loss_pct=0.50)
    cmp = gap_model_comparison([], _pos(credit=0.20), SPOT, ATR, f, 100_000.0, iv_fn=IV, today=TODAY)
    assert cmp["bs_qty_1_5"] > 0
    assert cmp["bs_zeroed_the_candidate"] is False


def test_comparison_includes_current_book_stress_under_both_models():
    f = S2bFeatures()
    book = [_pos(560.0, 550.0, 2.0, 3)]
    cmp = gap_model_comparison(book, _pos(), SPOT, ATR, f, 100_000.0, iv_fn=IV, today=TODAY)
    assert cmp["bs_current_book_1_5"] > cmp["intrinsic_current_book_1_5"]


def test_comparison_never_changes_what_is_enforced():
    """Belt and braces: the enforcing caps must be byte-identical whether or not the comparison ran."""
    f = S2bFeatures(max_gap_stress_loss_pct=0.02)
    before = [c.maximum_qty for c in
              gap_quantity_caps([], _pos(), SPOT, ATR, f, 100_000.0, iv_fn=IV, today=TODAY)]
    gap_model_comparison([], _pos(), SPOT, ATR, f, 100_000.0, iv_fn=IV, today=TODAY)
    after = [c.maximum_qty for c in
             gap_quantity_caps([], _pos(), SPOT, ATR, f, 100_000.0, iv_fn=IV, today=TODAY)]
    assert before == after


# ── 2C: BS inputs are guarded and inspectable ────────────────────────────────────────────────────

def _detail(**over):
    base = dict(short_strike=568.0, long_strike=558.0, credit=2.0, qty=1, spot=SPOT, atr=ATR,
                drop_atr=1.5, dte=4, short_iv=0.20, long_iv=0.20, f=S2bFeatures())
    base.update(over)
    return stressed_spread_loss_bs_detail(**base)


def test_detail_exposes_every_black_scholes_input():
    d = _detail()
    for key in ("scenario_spot", "short_strike", "long_strike", "time_to_expiry_years",
                "risk_free_rate", "dividend_assumption", "option_type", "spread_close_debit",
                "stressed_close_slippage", "wing_width", "structural_cap_applied"):
        assert key in d
    assert d["option_type"] == "put"
    assert d["scenario_spot"] == pytest.approx(SPOT - 1.5 * ATR)
    assert d["worst_cell"]["short_iv_after"] > d["worst_cell"]["short_iv_before"]


def test_detail_matches_the_plain_loss_function():
    kw = dict(short_strike=568.0, long_strike=558.0, credit=2.0, qty=1, spot=SPOT, atr=ATR,
              drop_atr=1.5, dte=4, short_iv=0.20, long_iv=0.20, f=S2bFeatures())
    assert stressed_spread_loss_bs(**kw) == pytest.approx(
        stressed_spread_loss_bs_detail(**kw)["loss"])


@pytest.mark.parametrize("over,match", [
    (dict(spot=0.0), "spot must be positive"),
    (dict(spot=-5.0), "spot must be positive"),
    (dict(short_strike=0.0), "strikes must be positive"),
    (dict(short_strike=558.0, long_strike=568.0), "short_strike > long_strike"),
    (dict(short_iv=-0.5), "IV must be positive"),
    (dict(drop_atr=200.0), "shocked spot must be positive"),
])
def test_invalid_inputs_raise_rather_than_repricing_nonsense(over, match):
    with pytest.raises(ValueError, match=match):
        _detail(**over)


def test_zero_dte_is_legitimate_and_prices_at_intrinsic_not_an_error():
    """Expiry day is a real state, not bad data -- so T <= 0 must NOT raise. The advisor's
    'assert time_to_expiry_years > 0' would have crashed the bot on every 0-DTE position."""
    d = _detail(dte=0)
    assert d["time_to_expiry_years"] == 0.0
    # At T=0 the spread is worth its intrinsic value; shocked spot 566 vs 568/558 -> 2.0 wide
    assert d["spread_close_debit"] == pytest.approx(2.0 + 0.0, abs=0.6)


def test_spread_loss_is_capped_at_the_structural_maximum():
    """A vertical can never cost more than its wing to close, however deep the shock."""
    d = _detail(drop_atr=6.0, wing_width=10.0)
    assert d["spread_close_debit"] <= 10.0
    assert d["structural_cap_applied"] is True
    assert d["loss"] <= (10.0 - 2.0) * 100.0


def test_leg_values_are_clamped_to_no_arbitrage_bounds():
    d = _detail(drop_atr=3.0)
    cell = d["worst_cell"]
    s = d["scenario_spot"]
    assert cell["short_put_value"] >= max(0.0, 568.0 - s) - 1e-9
    assert cell["short_put_value"] <= 568.0
    assert cell["spread_mid"] >= 0.0


# ── 2D: regression fixtures across the stated grid ───────────────────────────────────────────────

@pytest.mark.parametrize("name,kw", [
    ("deep_otm",       dict(short_strike=520.0, long_strike=510.0)),
    ("near_the_money", dict(short_strike=574.0, long_strike=564.0)),
    ("one_dte",        dict(dte=1)),
    ("ten_dte",        dict(dte=10)),
    ("low_iv",         dict(short_iv=0.08, long_iv=0.08)),
    ("high_iv",        dict(short_iv=0.90, long_iv=0.90)),
])
def test_regression_fixtures_produce_finite_bounded_losses(name, kw):
    """Fixed inputs across the advisor's grid. Each must produce a finite loss inside the structural
    bounds -- the point is to catch a future pricing change that silently breaks one corner."""
    import math
    d = _detail(**kw)
    assert math.isfinite(d["loss"])
    assert 0.0 <= d["spread_close_debit"] <= d["wing_width"]
    max_loss = (d["wing_width"] - d["credit"]) * 100.0
    assert -d["credit"] * 100.0 <= d["loss"] <= max_loss


def test_near_the_money_stresses_worse_than_deep_otm():
    assert _detail(short_strike=574.0, long_strike=564.0)["loss"] > \
           _detail(short_strike=520.0, long_strike=510.0)["loss"]


def test_more_time_to_expiry_stresses_worse_than_less():
    assert _detail(dte=10)["loss"] > _detail(dte=1)["loss"]


def test_higher_iv_stresses_worse_than_lower():
    assert _detail(short_iv=0.90, long_iv=0.90)["loss"] > _detail(short_iv=0.08, long_iv=0.08)["loss"]


@pytest.mark.parametrize("book,label", [([], "empty_book"),
                                         ([_pos(560.0, 550.0, 2.0, 5)], "already_stressed_book")])
def test_current_book_fixtures(book, label):
    f = S2bFeatures()
    caps = gap_quantity_caps(book, _pos(), SPOT, ATR, f, 100_000.0, iv_fn=IV, today=TODAY)
    assert [c.name for c in caps] == ["gap_1atr", "gap_1_5atr", "gap_2atr"]
    assert all(c.maximum_qty >= 0 for c in caps)
    assert all(c.incremental_risk_per_contract > 0 for c in caps)


@pytest.mark.parametrize("budget_pct,expected_qty", [(0.0130, 3), (0.0100, 2), (0.0060, 1), (0.0040, 0)])
def test_candidate_fits_at_exactly_the_expected_quantity(budget_pct, expected_qty):
    """Pinned fixtures for the fits-at-N cases. The 1.5-ATR BS incremental is ~422.30/contract on
    this 568/558 candidate at a $2.00 credit, so the budgets above bracket 3/2/1/0 contracts."""
    f = S2bFeatures(max_gap_stress_loss_pct=budget_pct)
    caps = {c.name: c for c in
            gap_quantity_caps([], _pos(), SPOT, ATR, f, 100_000.0, iv_fn=IV, today=TODAY)}
    assert caps["gap_1_5atr"].maximum_qty == expected_qty


# ── regression: zero marginal stressed loss must not be read as "no contracts" ───────────────────

def test_risk_free_candidate_is_not_blocked_by_gap_stress():
    """A deep-OTM 520/510 spread at spot 575 with a $2.00 credit stays PROFITABLE even at -2 ATR, so
    its marginal stressed loss is zero. Gap stress must impose no constraint on it.

    Before this fix every scenario returned maximum_qty 0, meaning the gate rejected precisely the
    candidates that cannot lose money in the stress scenario -- the meaning of the cap was inverted.
    """
    f = S2bFeatures()
    caps = gap_quantity_caps([], _pos(short=520.0, long_=510.0, credit=2.0), SPOT, ATR, f,
                             100_000.0, iv_fn=IV, today=TODAY)
    assert all(c.incremental_risk_per_contract == 0.0 for c in caps)
    assert all(c.maximum_qty is None for c in caps)


def test_zero_marginal_loss_still_blocks_when_the_book_is_already_over_its_limit():
    """No marginal risk is not a licence to add to a book that has already breached the budget."""
    from bot.portfolio.gap_stress import gap_quantity_cap
    cap = gap_quantity_cap("gap_1_5atr", current_book_loss=9000.0,
                           one_contract_book_loss=9000.0, loss_limit=6000.0)
    assert cap.maximum_qty == 0


def test_a_risk_free_candidate_still_obeys_the_other_caps():
    """The sentinel must never become the binding cap -- the aggregate budgets still size the trade."""
    from bot.portfolio.risk_budget import size_to_risk_limits, QuantityCap
    caps = [QuantityCap("expiry_stop", 4, 100.0, 200.0, 100.0, 25.0),
            QuantityCap("gap_1_5atr", None, 0.0, 6000.0, 6000.0, 0.0)]
    result = size_to_risk_limits(10, caps)
    assert result.final_qty == 4
    assert result.limiting_gate == "expiry_stop"


# ── the advisor's named tests (botbnextsteps decision 1), verbatim intent ────────────────────────

def test_zero_incremental_gap_risk_does_not_block_candidate():
    from bot.portfolio.gap_stress import gap_quantity_cap
    cap = gap_quantity_cap(scenario_name="gap_2atr", current_book_loss=0.0,
                           one_contract_book_loss=0.0, loss_limit=500.0)
    assert cap.maximum_qty is None


def test_zero_gap_risk_does_not_override_structural_cap():
    from bot.portfolio.risk_budget import QuantityCap, size_to_risk_limits
    caps = [
        QuantityCap(name="gap_2atr", maximum_qty=None, current_exposure=0.0, limit=500.0,
                    remaining_capacity=500.0, incremental_risk_per_contract=0.0),
        QuantityCap(name="total_structural", maximum_qty=2, current_exposure=1000.0, limit=3000.0,
                    remaining_capacity=2000.0, incremental_risk_per_contract=900.0),
    ]
    result = size_to_risk_limits(requested_qty=5, caps=caps)
    assert result.final_qty == 2
    assert result.limiting_gate == "total_structural"


# ── decision 2: zero time is legitimate, negative time is not ───────────────────────────────────

def test_zero_time_prices_at_intrinsic_value():
    """At T=0 every BS cell collapses to intrinsic, so the grid's worst cell is just the intrinsic
    spread -- then the stressed-close widen haircut still applies on top (it models the bid/ask you
    would actually pay to close into a gapping market, which does not vanish at expiry)."""
    d = _detail(dte=0, drop_atr=1.5)
    assert d["time_to_expiry_years"] == 0.0
    s = d["scenario_spot"]
    intrinsic = max(0.0, 568.0 - s) - max(0.0, 558.0 - s)
    assert intrinsic == pytest.approx(2.0)
    widened = intrinsic * (1.0 + S2bFeatures().gap_stress_widen)
    assert d["spread_close_debit"] == pytest.approx(widened, abs=1e-9)


def test_negative_time_is_rejected():
    with pytest.raises(ValueError, match="cannot be negative"):
        _detail(dte=-1)
