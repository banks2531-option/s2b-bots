"""TDD for Task 4.1: downturn-detection shadow monitor (partner review v2 §13).

LOG ONLY. Behind deps.features.regime_shadow_monitor (opt-in, default OFF -> Bot C /
run_s2b_live.py unaffected). Must NEVER change any order, size, or entry decision.
"""
from datetime import datetime

from bot.regime.shadow_monitor import compute_shadow_signals, shadow_caution_score, ACTIONS
from bot.app.orchestrator import BotState, Deps, tick
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
from bot.features import S2bFeatures


# ── compute_shadow_signals ────────────────────────────────────────────────────────────────────

def _signals(**over):
    base = dict(
        spy=575.0, spy_vwap=570.0, spy_atr=5.0, session_high=578.0, session_low=568.0,
        opening_range={"high": 576.0, "low": 570.0},
        qqq_ret=0.01, dia_ret=0.005, soxx_ret=0.02, spy_ret=0.008,
        qqq_vs_vwap=1.2, soxx_vs_vwap=0.8,
        vix=15.0, vix1d=13.0, put_skew=0.02,
        short_delta=0.35, short_gamma=0.02, short_iv=0.18,
    )
    base.update(over)
    return base


def test_compute_shadow_signals_computes_derived_fields():
    sig = compute_shadow_signals(**_signals())
    # dist_from_vwap_atr = (spy - spy_vwap) / spy_atr = (575-570)/5 = 1.0
    assert sig["dist_from_vwap_atr"] == 1.0
    # dist_from_high_atr = (session_high - spy) / spy_atr = (578-575)/5 = 0.6
    assert sig["dist_from_high_atr"] == 0.6
    # vix1d_vix_ratio = 13/15
    assert abs(sig["vix1d_vix_ratio"] - (13.0 / 15.0)) < 1e-5
    # qqq_minus_dia = 0.01 - 0.005 = 0.005
    assert abs(sig["qqq_minus_dia"] - 0.005) < 1e-9
    # soxx_minus_spy = 0.02 - 0.008 = 0.012
    assert abs(sig["soxx_minus_spy"] - 0.012) < 1e-9
    # pass-through fields present unchanged
    assert sig["spy"] == 575.0
    assert sig["vix"] == 15.0
    assert sig["put_skew"] == 0.02
    assert sig["short_delta"] == 0.35


def test_compute_shadow_signals_opening_range_dict_and_tuple_both_work():
    sig_dict = compute_shadow_signals(**_signals(opening_range={"high": 580.0, "low": 565.0}))
    assert sig_dict["opening_range_high"] == 580.0
    assert sig_dict["opening_range_low"] == 565.0
    sig_tuple = compute_shadow_signals(**_signals(opening_range=(580.0, 565.0)))
    assert sig_tuple["opening_range_high"] == 580.0
    assert sig_tuple["opening_range_low"] == 565.0
    sig_none = compute_shadow_signals(**_signals(opening_range=None))
    assert sig_none["opening_range_high"] is None
    assert sig_none["opening_range_low"] is None


def test_compute_shadow_signals_tolerates_none_atr_no_raise():
    sig = compute_shadow_signals(**_signals(spy_atr=None))
    assert sig["dist_from_vwap_atr"] is None
    assert sig["dist_from_high_atr"] is None
    assert sig["spy"] == 575.0   # unaffected pass-through fields stay intact


def test_compute_shadow_signals_tolerates_none_vix_no_raise():
    sig = compute_shadow_signals(**_signals(vix=None, vix1d=None))
    assert sig["vix1d_vix_ratio"] is None
    assert sig["vix"] is None
    assert sig["vix1d"] is None


def test_compute_shadow_signals_tolerates_zero_atr_no_zerodivision():
    sig = compute_shadow_signals(**_signals(spy_atr=0.0))
    assert sig["dist_from_vwap_atr"] is None
    assert sig["dist_from_high_atr"] is None


def test_compute_shadow_signals_all_none_never_raises():
    all_none = {k: None for k in _signals().keys()}
    sig = compute_shadow_signals(**all_none)   # must not raise
    for v in sig.values():
        assert v is None


def test_compute_shadow_signals_breadth_up_down_vol_whale_flow_default_none():
    sig = compute_shadow_signals(**_signals())
    assert sig["breadth"] is None
    assert sig["up_down_vol"] is None
    assert sig["whale_flow"] is None


def test_compute_shadow_signals_passes_through_breadth_when_available():
    sig = compute_shadow_signals(**_signals(), breadth=0.65, up_down_vol=1.3, whale_flow=0.2)
    assert sig["breadth"] == 0.65
    assert sig["up_down_vol"] == 1.3
    assert sig["whale_flow"] == 0.2


# ── shadow_caution_score ──────────────────────────────────────────────────────────────────────

def _calm_signals():
    return compute_shadow_signals(**_signals(
        spy=572.0, spy_vwap=571.0, spy_atr=5.0,        # barely above vwap
        session_high=580.0,                              # far from the high (1.6 ATR)
        vix=15.0, vix1d=12.0,                             # ratio well below 1 (contango, calm)
        qqq_ret=0.005, dia_ret=0.004, soxx_ret=0.006, spy_ret=0.004,   # cross-asset all in line
        breadth=0.6,
    ))


def _stressed_signals():
    return compute_shadow_signals(**_signals(
        spy=580.0, spy_vwap=570.0, spy_atr=5.0,          # 2 ATR above vwap: very extended
        session_high=580.2,                                # basically AT the session high
        vix=20.0, vix1d=24.0,                              # inverted (near-term stress)
        qqq_ret=-0.01, dia_ret=0.01, soxx_ret=-0.02, spy_ret=0.0,   # tech + semis both lagging hard
        breadth=0.1,
    ))


def test_shadow_caution_score_bounded_0_1():
    for sig in (_calm_signals(), _stressed_signals()):
        score, action = shadow_caution_score(sig)
        assert 0.0 <= score <= 1.0
        assert action in ACTIONS


def test_shadow_caution_score_calm_scenario_is_low_and_normal():
    score, action = shadow_caution_score(_calm_signals())
    assert score < 0.35
    assert action in ("normal", "half_size")


def test_shadow_caution_score_stressed_scenario_is_high_and_defensive():
    score, action = shadow_caution_score(_stressed_signals())
    assert score > 0.6
    assert action in ("reduce_bullish", "block_bullish", "add_hedge", "activate_bearish")


def test_shadow_caution_score_stressed_scores_higher_than_calm():
    calm_score, calm_action = shadow_caution_score(_calm_signals())
    stressed_score, stressed_action = shadow_caution_score(_stressed_signals())
    assert stressed_score > calm_score
    assert ACTIONS.index(stressed_action) > ACTIONS.index(calm_action)


def test_shadow_caution_score_robust_to_all_none_signals():
    all_none_sig = compute_shadow_signals(**{k: None for k in _signals().keys()})
    score, action = shadow_caution_score(all_none_sig)
    assert score == 0.0
    assert action == "normal"


def test_shadow_caution_score_robust_to_partial_signals():
    # only vix1d/vix available, everything else missing -> must not raise, must stay bounded
    partial = {k: None for k in compute_shadow_signals(**_signals()).keys()}
    partial["vix1d_vix_ratio"] = 1.3   # a strong inversion
    score, action = shadow_caution_score(partial)
    assert 0.0 <= score <= 1.0
    assert action in ACTIONS


def test_actions_is_the_exact_spec_label_set():
    assert set(ACTIONS) == {"normal", "half_size", "probe_size", "block_bullish",
                            "reduce_bullish", "add_hedge", "activate_bearish"}


# ── orchestrator wiring: SHADOW logging must never change the entry decision ─────────────────────

def _pos():
    from bot.strategy.manage import ManagedPosition
    return ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=1, expiry="2026-06-19")


def _chain():
    return [
        OptionQuote(568.0, 0.36, 4.00, 4.10),
        OptionQuote(558.0, 0.18, 1.90, 2.00),
    ]


def _acct(today, conc):
    return AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today)


def _base_deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _chain(),
        pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=_acct,
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 100_000.0,
        bot_equity=lambda: 100_000.0, alert_sink=lambda alerts: None,
        risk_equity=lambda: 100_000.0,
    )
    base.update(over)
    return Deps(**base)


MONDAY = datetime(2026, 6, 15, 10, 5)   # Monday 10:05, expiry 2026-06-19


def _fake_shadow_data():
    return dict(
        spy=575.0, spy_vwap=570.0, spy_atr=6.0, session_high=578.0, session_low=568.0,
        opening_range={"high": 576.0, "low": 570.0},
        qqq_ret=0.01, dia_ret=0.005, soxx_ret=0.02, spy_ret=0.008,
        qqq_vs_vwap=1.2, soxx_vs_vwap=0.8, vix=15.0, vix1d=13.0, put_skew=0.02,
        short_delta=0.35, short_gamma=0.02, short_iv=0.18,
        breadth=None, up_down_vol=None, whale_flow=None,
    )


def test_shadow_monitor_on_logs_one_shadow_record_per_tick():
    logged = []
    f = S2bFeatures(regime_shadow_monitor=True)
    d = _base_deps(features=f, trade_log=lambda rec: logged.append(rec),
                  shadow_data=_fake_shadow_data)
    state = BotState()
    tick(state, d, MONDAY)
    shadow_recs = [r for r in logged if r.get("event") == "SHADOW"]
    assert len(shadow_recs) == 1
    rec = shadow_recs[0]
    assert "shadow_score" in rec and "shadow_action" in rec
    assert rec["shadow_action"] in ACTIONS
    assert rec["date"] == "2026-06-15"


def test_shadow_monitor_off_writes_no_shadow_records():
    logged = []
    d = _base_deps(trade_log=lambda rec: logged.append(rec), shadow_data=_fake_shadow_data)
    assert d.features.regime_shadow_monitor is False
    state = BotState()
    tick(state, d, MONDAY)
    shadow_recs = [r for r in logged if r.get("event") == "SHADOW"]
    assert shadow_recs == []


def test_shadow_monitor_never_changes_the_entry_outcome():
    """The critical safety property: identical ticks except for the shadow flag must produce
    byte-identical entry decisions/state (same open_positions, same qty, same strikes)."""
    def _run(shadow_on):
        f = S2bFeatures(regime_shadow_monitor=shadow_on)
        logged = []
        d = _base_deps(features=f, trade_log=lambda rec: logged.append(rec),
                       shadow_data=_fake_shadow_data)
        state = BotState()
        state = tick(state, d, MONDAY)
        return state, logged

    state_off, logged_off = _run(False)
    state_on, logged_on = _run(True)

    assert len(state_off.open_positions) == len(state_on.open_positions) == 1
    p_off, p_on = state_off.open_positions[0], state_on.open_positions[0]
    assert (p_off.short_strike, p_off.long_strike, p_off.qty, p_off.credit) == \
           (p_on.short_strike, p_on.long_strike, p_on.qty, p_on.credit)
    assert state_off.halted == state_on.halted
    assert state_off.entries_today == state_on.entries_today
    # the only difference allowed is the extra SHADOW record when the flag is on
    open_events_off = [r for r in logged_off if r["event"] == "OPEN"]
    open_events_on = [r for r in logged_on if r["event"] == "OPEN"]
    assert open_events_off == open_events_on


def test_shadow_monitor_runs_even_when_no_entry_happens():
    # Tuesday: wrong weekday -> no entry, but SHADOW must still be logged once per tick.
    logged = []
    f = S2bFeatures(regime_shadow_monitor=True)
    d = _base_deps(features=f, trade_log=lambda rec: logged.append(rec),
                  shadow_data=_fake_shadow_data)
    state = BotState()
    tick(state, d, datetime(2026, 6, 16, 10, 5))   # Tuesday
    shadow_recs = [r for r in logged if r.get("event") == "SHADOW"]
    assert len(shadow_recs) == 1
    assert state.open_positions == []


def test_shadow_monitor_never_raises_when_shadow_data_errors():
    logged = []
    f = S2bFeatures(regime_shadow_monitor=True)

    def _boom():
        raise RuntimeError("feed down")

    d = _base_deps(features=f, trade_log=lambda rec: logged.append(rec), shadow_data=_boom)
    state = BotState()
    tick(state, d, MONDAY)   # must not raise
    shadow_recs = [r for r in logged if r.get("event") == "SHADOW"]
    assert shadow_recs == []   # best-effort: no record when the fetch itself blows up
    assert len(state.open_positions) == 1   # entry still proceeded normally


def test_deps_default_shadow_data_returns_full_key_set():
    d = _base_deps()
    data = d.shadow_data()
    from bot.regime.shadow_monitor import compute_shadow_signals as _c
    # must not raise when fed straight into compute_shadow_signals
    sig = _c(**data)
    assert sig["spy"] is None


def test_s2b_features_regime_shadow_monitor_default_off():
    assert S2bFeatures().regime_shadow_monitor is False


def test_alldays_features_have_regime_shadow_monitor_on():
    from bot.app.run_s2b_alldays import ALLDAYS_FEATURES
    assert ALLDAYS_FEATURES.regime_shadow_monitor is True


def test_run_s2b_live_enables_regime_shadow_monitor():
    # regime_shadow_monitor enabled on the live bot 2026-07-16 (LIVE_FEATURES); default stays off.
    from bot.app.run_s2b_live import LIVE_FEATURES
    assert LIVE_FEATURES.regime_shadow_monitor is True
    assert S2bFeatures().regime_shadow_monitor is False


# ── trade-log field set: shadow columns only added when include_shadow_columns=True ─────────────

def test_make_trade_logger_default_has_no_shadow_columns(tmp_path):
    from bot.app.wiring import make_trade_logger
    p = tmp_path / "trades_live.csv"
    log = make_trade_logger(str(p))
    log({"event": "SHADOW", "date": "2026-06-15", "shadow_score": 0.5, "shadow_action": "normal",
         "spy": 575.0, "vix": 15.0})
    header = open(str(p)).readline().strip().split(",")
    assert "shadow_score" not in header and "shadow_action" not in header and "vix" not in header


def test_make_trade_logger_include_shadow_columns_true_adds_shadow_fields(tmp_path):
    from bot.app.wiring import make_trade_logger
    p = tmp_path / "trades_alldays.csv"
    log = make_trade_logger(str(p), include_shadow_columns=True)
    log({"event": "SHADOW", "date": "2026-06-15", "shadow_score": 0.5, "shadow_action": "normal",
         "spy": 575.0, "vix": 15.0})
    header = open(str(p)).readline().strip().split(",")
    for col in ("shadow_score", "shadow_action", "spy", "vix"):
        assert col in header


def test_build_and_run_gates_include_shadow_columns_on_regime_shadow_monitor_flag():
    import inspect
    from bot.app import run_s2b
    src = inspect.getsource(run_s2b.build_and_run)
    assert "include_shadow_columns=resolved_features.regime_shadow_monitor" in src


# ── real wiring: build_deps().shadow_data() against a fake Tradier http (not just a stub) ────────

def _shadow_fake_http(responses):
    def http(method, path, params=None, data=None):
        for key, resp in responses.items():
            if key in path:
                return resp
        return {}
    return http


def test_build_deps_shadow_data_fetches_basket_and_never_raises():
    from bot.app.wiring import build_deps
    chain = {"options": {"option": [
        {"strike": 568.0, "option_type": "put", "bid": 4.00, "ask": 4.10,
         "greeks": {"delta": -0.36, "gamma": 0.02, "mid_iv": 0.19}},
        {"strike": 558.0, "option_type": "put", "bid": 1.60, "ask": 1.70,
         "greeks": {"delta": -0.18, "gamma": 0.01, "mid_iv": 0.24}},
    ]}}
    quotes = {"quotes": {"quote": [
        {"symbol": "SPY", "last": 575.0, "high": 578.0, "low": 570.0, "change_percentage": 0.8},
        {"symbol": "QQQ", "last": 490.0, "high": 493.0, "low": 486.0, "change_percentage": 1.1},
        {"symbol": "DIA", "last": 400.0, "high": 402.0, "low": 398.0, "change_percentage": 0.4},
        {"symbol": "SOXX", "last": 230.0, "high": 235.0, "low": 225.0, "change_percentage": 2.0},
        {"symbol": "VIX", "last": 15.2},
        {"symbol": "VIX1D", "last": 13.1},
    ]}}
    timesales = {"series": {"data": [
        {"high": 574.0, "low": 572.0, "close": 573.0, "volume": 1000},
        {"high": 575.0, "low": 573.0, "close": 574.0, "volume": 1200},
    ]}}
    http = _shadow_fake_http({
        "/markets/quotes": quotes,
        "/markets/timesales": timesales,
        "/markets/options/expirations": {"expirations": {"date": ["2026-06-19"]}},
        "/markets/options/chains": chain,
    })
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    data = deps.shadow_data()   # must not raise
    assert data["spy"] == 575.0
    assert data["session_high"] == 578.0
    assert data["session_low"] == 570.0
    assert abs(data["spy_ret"] - 0.008) < 1e-9
    assert data["vix"] == 15.2
    assert data["vix1d"] == 13.1
    assert data["spy_vwap"] is not None
    assert data["opening_range"] == {"high": 575.0, "low": 572.0}
    assert data["short_delta"] is not None
    # feed straight into compute_shadow_signals -> must not raise
    sig = compute_shadow_signals(**data)
    assert sig["spy"] == 575.0


def test_build_deps_shadow_data_never_raises_when_all_feeds_are_down():
    from bot.app.wiring import build_deps

    def broken_http(method, path, params=None, data=None):
        raise ConnectionError("network down")

    deps = build_deps(broken_http, account_id="ABC", get_spot=lambda s: (_ for _ in ()).throw(RuntimeError()),
                      get_atr=lambda s: (_ for _ in ()).throw(RuntimeError()),
                      get_vix_regime=lambda: (0.5, 0.01))
    data = deps.shadow_data()   # must not raise even though every single feed blows up
    assert data["spy"] is None
    sig = compute_shadow_signals(**data)   # must not raise
    assert all(v is None for v in sig.values())
