"""TDD for Task 1.1: feature-flag config foundation (opt-in, OFF by default)."""
from bot.features import S2bFeatures


def test_s2b_features_defaults_all_off():
    f = S2bFeatures()
    assert f.credit_tiers is False
    assert f.transaction_cost_gate is False
    assert f.entry_price_ladder is False
    assert f.tp_price_ladder is False
    assert f.aggregate_risk_budget is False
    assert f.actual_fill_accounting is False
    assert f.regime_shadow_monitor is False
    assert f.regime_entry_blocks is False
    assert f.automatic_hedging is False
    assert f.bearish_module is False
    assert f.early_loss_exit is False
    assert f.allocated_equity == 72000.0


def test_build_deps_with_no_features_gets_all_off_default():
    from bot.app.wiring import build_deps

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    assert deps.features == S2bFeatures()
    assert deps.features.credit_tiers is False


def test_build_deps_with_alldays_style_features_object():
    from bot.app.wiring import build_deps

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        return {}

    alldays_features = S2bFeatures(
        credit_tiers=True, transaction_cost_gate=True, entry_price_ladder=True,
        tp_price_ladder=True, aggregate_risk_budget=True, actual_fill_accounting=True,
        regime_shadow_monitor=True,
    )
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01), features=alldays_features)
    assert deps.features.credit_tiers is True
    assert deps.features.transaction_cost_gate is True
    assert deps.features.entry_price_ladder is True
    assert deps.features.tp_price_ladder is True
    assert deps.features.aggregate_risk_budget is True
    assert deps.features.actual_fill_accounting is True
    assert deps.features.regime_shadow_monitor is True
    # Phase-4 alpha flags stay off even in the all-days feature set
    assert deps.features.regime_entry_blocks is False
    assert deps.features.automatic_hedging is False
    assert deps.features.bearish_module is False
    assert deps.features.early_loss_exit is False


def test_build_deps_live_style_no_features_stays_all_off():
    from bot.app.wiring import build_deps

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 402.0}}
        return {}

    deps = build_deps(http, account_id="6YB71948", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    assert deps.features.credit_tiers is False


def test_min_equity_to_open_defaults_to_zero_no_gate():
    from bot.features import S2bFeatures
    assert S2bFeatures().min_equity_to_open == 0.0


def test_risk_classification_defaults_to_unclassified():
    from bot.features import S2bFeatures
    assert S2bFeatures().risk_classification == "UNCLASSIFIED"


def test_bot_c_declares_itself_an_aggressive_live_pilot():
    """Advisor nextsteps2 section 2: 'If immediate live trading remains the priority, retain the
    current one-contract configuration but classify it accurately.' The classification is a
    config value, not a comment, so it appears in decision logs alongside every trade."""
    from bot.app.run_s2b_live import LIVE_FEATURES
    assert LIVE_FEATURES.risk_classification == "AGGRESSIVE_LIVE_PILOT"
    assert LIVE_FEATURES.min_equity_to_open == 540.0
