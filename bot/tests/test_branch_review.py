"""Formal branch review before the Bot B deployment (advisor botbnextsteps, "Formal branch review").

Encoded as tests rather than a one-off script so the guardrails keep holding after this branch
merges. Every item here is something the directive asks to be verified by hand; a test that fails
loudly on a future edit is worth more than a checklist someone ran once.
"""
import inspect
import pytest

from bot.features import S2bFeatures
from bot.strategy.credit_quality import (ABSOLUTE_CREDIT_FLOOR, STANDARD_PROBE_FLOOR,
                                          FULL_SIZE_CREDIT_FLOOR)
from bot.app.run_s2b_alldays import ALLDAYS_FEATURES as BOT_B
from bot.app.run_s2b_live import LIVE_FEATURES as BOT_C, BOT_C_STRUCTURAL_PCT


# ── guardrails the directive names explicitly ───────────────────────────────────────────────────

def test_credit_floors_unchanged():
    assert ABSOLUTE_CREDIT_FLOOR == 0.08
    assert STANDARD_PROBE_FLOOR == 0.10
    assert FULL_SIZE_CREDIT_FLOOR == 0.115


def test_bot_b_structural_limit_is_the_specified_fifteen_percent():
    assert BOT_B.max_total_structural_risk_pct == 0.15


def test_bot_c_structural_limit_matches_its_own_calibration():
    """The directive's literal `assert MAX_TOTAL_STRUCTURAL_RISK_PCT == 0.15` holds for Bot B but
    NOT Bot C, which runs 0.50 by deliberate calibration (2026-07-17): a single $5-wing is ~49% of
    its ~$842 account, and the big-account default sized every trade to zero. Asserting 0.15 there
    would refuse to start the live bot. The intent -- do not loosen limits in this deployment -- is
    enforced against each bot's own deployed value."""
    assert BOT_C.max_total_structural_risk_pct == BOT_C_STRUCTURAL_PCT == 0.50


def test_gap_enforcement_is_black_scholes_on_both_bots():
    assert BOT_B.gap_stress_enforcement_model == "black_scholes"
    assert BOT_C.gap_stress_enforcement_model == "black_scholes"


def test_five_wide_live_trading_is_off_everywhere():
    assert BOT_B.enable_five_wide_live is False
    assert BOT_C.enable_five_wide_live is False
    assert S2bFeatures().enable_five_wide_live is False      # and off by default


def test_bearish_module_is_off_everywhere():
    assert BOT_B.bearish_module is False
    assert BOT_C.bearish_module is False


# ── the bots must not silently share configuration ──────────────────────────────────────────────

def test_bot_c_carries_the_one_contract_ceiling_and_bot_b_does_not():
    """The sandbox exists to demonstrate the cap system choosing 1, 2 or more contracts under the
    real budgets; inheriting Bot C's controlled-release ceiling would defeat that."""
    assert BOT_C.max_entry_qty == 1
    assert BOT_B.max_entry_qty is None


def test_live_config_does_not_inherit_sandbox_only_features():
    assert BOT_C.enable_alternate_expirations is False       # T8 is Bot B validation first
    assert BOT_C.enable_five_wide_shadow is False
    assert BOT_C.enable_low_credit_08_to_10 is False         # inputs not field-verified yet


def test_bot_b_has_the_validation_features_the_directive_requires():
    assert BOT_B.enable_alternate_expirations is True
    assert BOT_B.log_intrinsic_gap_comparison is True
    assert BOT_B.enable_five_wide_shadow is True
    assert BOT_B.entry_state_tracking is True


# ── behavior the branch must NOT have changed ───────────────────────────────────────────────────

def test_hard_stop_and_take_profit_calculations_unchanged():
    from bot.strategy.manage import ManageConfig, decide_exit, ExitAction
    cfg = ManageConfig()
    assert (cfg.stop_mult, cfg.tp_pct, cfg.time_exit_dte) == (2.0, 0.50, 1)
    # stop at credit * (1 + stop_mult); take profit at credit * (1 - tp_pct)
    assert decide_exit(3.00, 1.00, 5, cfg) is ExitAction.STOP
    assert decide_exit(2.99, 1.00, 5, cfg) is not ExitAction.STOP
    assert decide_exit(0.50, 1.00, 5, cfg) is ExitAction.TAKE_PROFIT


def test_order_submission_ladders_remain_off_on_both_bots():
    for f in (BOT_B, BOT_C):
        assert f.entry_price_ladder in (True, False)       # explicit, not accidental
    assert BOT_C.entry_price_ladder is False
    assert BOT_C.tp_price_ladder is False


def test_actual_fill_accounting_still_off_on_the_live_bot():
    """Known broken against the real broker (credit sign + leg-count quantity). This branch must not
    have quietly turned it on."""
    assert BOT_C.actual_fill_accounting is False


def test_regime_entry_blocking_still_off():
    assert BOT_B.regime_entry_blocks is False
    assert BOT_C.regime_entry_blocks is False


def test_default_features_are_all_off_so_a_new_bot_opts_in():
    d = S2bFeatures()
    for flag in ("enable_alternate_expirations", "entry_state_tracking", "enable_five_wide_shadow",
                 "enable_five_wide_live", "log_intrinsic_gap_comparison", "aggregate_risk_budget",
                 "credit_tiers", "bearish_module"):
        assert getattr(d, flag) is False, flag
    assert d.max_entry_qty is None


# ── retired paths: one shared implementation, no private copies ─────────────────────────────────

def test_retired_gap_stress_model_flag_cannot_be_constructed():
    with pytest.raises(TypeError):
        S2bFeatures(gap_stress_model="bs")


def test_one_shared_quarter_hour_bucket_implementation():
    """The directive asks for exactly one. Three existed before Task 7 (candidate dedup,
    credit-history dedup, markout dedup) and they disagreed about when a window rolls."""
    from bot.strategy.credit_quality import bucket15_of, candidate_key
    from datetime import datetime
    t = datetime(2026, 7, 20, 10, 8)
    assert bucket15_of(t) == t.hour * 4 + t.minute // 15
    # candidate_key's bucket identifies the same window
    assert candidate_key("2026-07-20", "e", 1.0, 2.0, t)[4] == (t.hour, t.minute // 15)
    import bot.app.orchestrator as orch
    src = inspect.getsource(orch)
    assert "minutes_since_open" not in src, "a private quarter-hour calc came back"


def test_one_shared_credit_ratio_change_comparison():
    """The 0.005 threshold must have a single implementation -- it had two, and they disagreed at the
    exact boundary because of binary float representation."""
    from bot.strategy.credit_quality import is_new_candidate, should_record_observation
    last = {"date": "d", "expiry": "e", "short": 1.0, "long": 2.0, "ratio": 0.12, "bucket15": 40}
    for delta in (0.005, -0.005, 0.0049, -0.0049):
        ratio = round(0.12 + delta, 6)
        assert should_record_observation(last, date="d", expiry="e", short=1.0, long=2.0,
                                         ratio=ratio, bucket15=40) is is_new_candidate(
            key=("k",), ratio=ratio, seen={("k",): 0.12})


def test_the_gap_shrink_loop_is_gone():
    import bot.app.orchestrator as orch
    src = inspect.getsource(orch)
    assert "while order.qty > 0 and gap_losses" not in src


def test_superseded_helpers_are_marked_and_uncalled():
    """cap_to_budgets and credit_tier predate the gap caps and the entry ceiling, so calling either
    would silently bypass both. They are retained for their tests but must stay out of the live path
    and must carry the warning."""
    from bot.portfolio.risk_budget import cap_to_budgets
    from bot.strategy.credit_quality import credit_tier
    assert "SUPERSEDED" in (cap_to_budgets.__doc__ or "")
    assert "SUPERSEDED" in (credit_tier.__doc__ or "")
    import bot.app.orchestrator as orch
    src = inspect.getsource(orch)
    for name in ("cap_to_budgets(", "credit_tier("):
        assert name not in src, f"{name} is reachable from the entry path"


def test_gap_rejections_no_longer_use_the_retired_reason_string():
    """External queries must move from reason == "gap_stress" to limiting_gate.startswith("gap_")
    or reason.startswith("risk_budget:gap_"). Nothing in the bot emits the old string any more."""
    import bot.app.orchestrator as orch
    src = inspect.getsource(orch)
    assert '"gap_stress"' not in src
    assert 'return state, "gap_stress"' not in src


def test_bot_c_actual_fill_accounting_stays_off_until_live_validated():
    """The leg-level normalizer is IMPLEMENTED but not yet validated against five live
    executions. Until the FILL_RECONCILE ledger is checked against the broker statement, Bot C
    keeps accounting synthetically. Implementing a fix is not the same as proving it."""
    from bot.app.run_s2b_live import LIVE_FEATURES
    assert LIVE_FEATURES.actual_fill_accounting is False


def test_bot_c_ladders_and_experimental_lanes_stay_off():
    """Advisor nextsteps2 section 1: no ladders, no alternate expirations, no low-credit lane
    until actual-fill accounting is fixed AND live-validated."""
    from bot.app.run_s2b_live import LIVE_FEATURES
    assert LIVE_FEATURES.entry_price_ladder is False
    assert LIVE_FEATURES.tp_price_ladder is False
    assert LIVE_FEATURES.enable_alternate_expirations is False
    assert LIVE_FEATURES.enable_low_credit_08_to_10 is False
    assert LIVE_FEATURES.enable_five_wide_live is False


def test_min_equity_to_open_is_enforced_in_the_entry_path():
    """Task 7b (landed 2026-07-19). This replaces the tripwire test that asserted the gate was
    NOT yet wired -- the config field shipped one release ahead of its enforcement, and the
    tripwire existed so that gap could not be forgotten.

    A stated risk floor that is checked nowhere is worse than no floor: it invites a reader to
    believe they are protected. The behavioural assertions live in test_min_equity_gate.py; this
    one guards against the reference being deleted from the entry path during a refactor while
    the config field survives, which would silently restore exactly that gap."""
    import inspect
    from bot.app import orchestrator
    assert "min_equity_to_open" in inspect.getsource(orchestrator.run_entry_cycle), (
        "min_equity_to_open is no longer checked in run_entry_cycle -- Bot C's declared capital "
        "floor is unenforced again. See bot/tests/test_min_equity_gate.py.")


def test_bot_c_capital_floor_is_the_operator_set_value_not_the_advisor_default():
    """The advisor recommends 2000. The operator set 540 on 2026-07-19 -- the account's existing
    implicit floor -- so the live pilot keeps producing data at its current ~$842. This test is a
    change-detector on purpose: raising the floor is a deliberate risk decision that should not
    ride along inside an unrelated commit."""
    from bot.app.run_s2b_live import LIVE_FEATURES
    assert LIVE_FEATURES.min_equity_to_open == 540.0
