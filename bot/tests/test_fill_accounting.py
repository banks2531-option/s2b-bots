"""Task 1.3 (spec §8): ExecutionResult + actual-fill accounting + synthetic costs.

Absolute requirement pinned by (d): with `actual_fill_accounting` OFF (the default, and ALWAYS
for the real-money live bot run_s2b_live.py), P&L and position credit/qty must be byte-identical
to the pre-change behavior -- even when the underlying open_spread/close_spread now return a full
ExecutionResult whose fill data disagrees with the requested/quoted values."""
from datetime import datetime

from bot.broker.order_state import ExecutionResult, result_status
from bot.strategy.manage import ManagedPosition
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
from bot.features import S2bFeatures
from bot.app.orchestrator import BotState, Deps, run_entry_cycle, run_management_cycle, tick


def _er(status="filled", requested_qty=1, filled_qty=1, avg_fill=None, submitted_limit=None,
        commissions=0.0, regulatory_fees=0.0, order_id=None):
    return ExecutionResult(status=status, requested_quantity=requested_qty,
                           filled_quantity=filled_qty, average_fill_price=avg_fill,
                           submitted_limit=submitted_limit, commissions=commissions,
                           regulatory_fees=regulatory_fees, order_id=order_id)


# ── (a) ExecutionResult shape from open_spread (wiring-level, fake broker, no network) ──────────

def test_open_spread_returns_execution_result_with_fill_fields():
    from bot.app.wiring import build_deps

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        if "/orders" in path and method == "POST":
            return {"order": {"id": 555, "status": "ok"}}
        if "/orders/555" in path:
            # sandbox: reports a fill price + quantity but NEVER commissions/fees
            return {"order": {"id": 555, "status": "filled",
                              "avg_fill_price": 1.65, "exec_quantity": 2}}
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01),
                      features=S2bFeatures(est_commission_per_leg_rt=0.70))
    result = deps.open_spread({"price": 1.70, "quantity[0]": 2})
    assert isinstance(result, ExecutionResult)
    assert result.status == "filled"
    assert result.filled_quantity == 2
    assert result.average_fill_price == 1.65        # actual broker fill, not the requested 1.70
    assert result.submitted_limit == 1.70
    # sandbox reported no commissions -> synthetic fallback: est_commission_per_leg_rt * 2 legs * qty
    assert result.commissions == 0.70 * 2 * 2
    assert result.regulatory_fees == 0.0


def test_open_spread_falls_back_to_submitted_limit_and_requested_qty_when_broker_reports_none():
    # this is the REAL sandbox behavior today: the order dict has no avg_fill_price/exec_quantity
    from bot.app.wiring import build_deps

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        if "/orders" in path and method == "POST":
            return {"order": {"id": 1, "status": "ok"}}
        if "/orders/1" in path:
            return {"order": {"id": 1, "status": "filled"}}   # no fill price, no fees
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    result = deps.open_spread({"price": 1.71, "quantity[0]": 3})
    assert result.average_fill_price == 1.71   # fallback: the credit/debit we sent
    assert result.filled_quantity == 3         # fallback: the requested quantity (status == filled)
    assert result.commissions == 0.65 * 2 * 3  # default est_commission_per_leg_rt = 0.65


def test_open_spread_not_filled_has_zero_filled_quantity():
    from bot.app.wiring import build_deps

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        if "/orders" in path and method == "POST":
            return {"order": {"id": 2, "status": "ok"}}
        if "/orders/2" in path:
            return {"order": {"id": 2, "status": "rejected"}}
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    result = deps.open_spread({"price": 1.71, "quantity[0]": 3})
    assert result.status == "rejected"
    assert result.filled_quantity == 0


# ── shared fixtures for orchestrator-level tests ─────────────────────────────────────────────────

def _chain():
    return [OptionQuote(568.0, 0.36, 3.40, 3.50), OptionQuote(565.0, 0.30, 2.80, 2.90),
            OptionQuote(560.0, 0.22, 2.00, 2.10), OptionQuote(558.0, 0.18, 1.60, 1.70)]


def _acct(today, conc):
    return AccountState(20_000.0, 20_000.0, 0.0, conc, 0.0, {}, today)


def _deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _chain(), pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=_acct,
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: _er(), close_spread=lambda p, a: _er(),
        broker_positions=lambda: [], broker_equity=lambda: 20_000.0,
        bot_equity=lambda: 20_000.0, alert_sink=lambda alerts: None,
    )
    base.update(over)
    return Deps(**base)


MONDAY = datetime(2026, 6, 15, 10, 5)


# ── (b) flag ON: opened position records the ACTUAL fill, and opening_fees is captured ──────────

def test_entry_records_actual_fill_credit_and_qty_when_flag_on():
    # the strategy would build order.credit=3.40-1.70=1.70, qty sized on the chain/account;
    # the (fake) broker actually filled at a WORSE credit (1.55) and a smaller quantity.
    state = BotState()
    d = _deps(features=S2bFeatures(actual_fill_accounting=True),
              open_spread=lambda payload: _er(avg_fill=1.55, filled_qty=1, commissions=2.60))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    pos = state.open_positions[0]
    assert pos.credit == 1.55          # actual fill, NOT the requested 1.70
    assert pos.qty == 1                # actual filled_quantity
    assert pos.opening_fees == 2.60    # synthetic (or broker-reported) commissions carried through


def test_entry_opening_fees_equals_commissions_plus_regulatory_fees():
    # the SYNTHESIS of commissions (when the broker reports none) is wiring.py's job (see the
    # wiring-level tests above); here we confirm the orchestrator faithfully carries whatever
    # commissions + regulatory_fees the ExecutionResult reports (synthetic or real) onto
    # ManagedPosition.opening_fees.
    state = BotState()
    d = _deps(features=S2bFeatures(actual_fill_accounting=True),
              open_spread=lambda payload: _er(avg_fill=1.70, filled_qty=2,
                                              commissions=1.30, regulatory_fees=0.04))
    state, info = run_entry_cycle(state, d, MONDAY)
    pos = state.open_positions[0]
    assert pos.opening_fees == 1.34   # commissions + regulatory_fees


def test_entry_fill_data_falls_back_to_requested_when_result_omits_it():
    # average_fill_price/filled_quantity None/0 on the ExecutionResult -> fall back to requested
    state = BotState()
    d = _deps(features=S2bFeatures(actual_fill_accounting=True),
              open_spread=lambda payload: _er(avg_fill=None, filled_qty=0))
    state, info = run_entry_cycle(state, d, MONDAY)
    pos = state.open_positions[0]
    assert pos.credit == 1.70   # order.credit (568 short bid 3.40 - 558 long ask 1.70)
    assert pos.qty == 2         # requested qty (20k equity, max_loss 830 -> floor(2000/830)=2)


# ── (c) realized P&L uses the ACTUAL close fill, nets fees, logs gross + net ─────────────────────

def test_realized_pnl_uses_actual_close_fill_not_triggering_mark():
    recs = []
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=2, expiry="2026-06-19",
                          opening_fees=1.30)
    state = BotState(open_positions=[pos])
    # mark_position (the value that TRIGGERS the stop) says 10.0 (a stop); the ACTUAL close fill
    # is materially better (9.20) because a limit reprice caught a better price on the way out.
    d = _deps(features=S2bFeatures(actual_fill_accounting=True),
              mark_position=lambda p: 10.0, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: _er(avg_fill=9.20, commissions=1.30, regulatory_fees=0.05),
              trade_log=lambda r: recs.append(r))
    run_management_cycle(state, d, today="2026-06-19")
    closes = [r for r in recs if r["event"] == "CLOSE"]
    assert len(closes) == 1
    rec = closes[0]
    # gross uses the ACTUAL fill (9.20), never the triggering mark (10.0):
    # gross = (3.0 - 9.20) * 100 * 2 = -1240.0
    assert rec["gross_pnl"] == -1240.0
    # net = gross - opening_fees(1.30) - close commissions(1.30) - close reg fees(0.05)
    assert rec["net_pnl"] == round(-1240.0 - 1.30 - 1.30 - 0.05, 2)
    assert rec["pnl"] == rec["net_pnl"]   # the bottom-line "pnl" field is cost-aware when flag ON


def test_realized_pnl_falls_back_to_triggering_mark_when_close_fill_missing():
    recs = []
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=1, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(features=S2bFeatures(actual_fill_accounting=True),
              mark_position=lambda p: 1.5, dte_of=lambda p, today: 5,   # TP mark
              close_spread=lambda p, a: _er(avg_fill=None),             # no actual fill reported
              trade_log=lambda r: recs.append(r))
    run_management_cycle(state, d, today="2026-06-19")
    closes = [r for r in recs if r["event"] == "CLOSE"]
    # gross = (3.0 - 1.5) * 100 * 1 = 150.0 (fell back to the triggering mark)
    assert closes[0]["gross_pnl"] == 150.0


# ── (d) REGRESSION: flag OFF -> byte-identical to pre-change behavior ────────────────────────────
# Fakes deliberately report a DIFFERENT fill than the requested/triggering values, to prove the
# flag-off path ignores fill data entirely (this is the pinned safety requirement for Bot C).

def test_flag_off_entry_uses_requested_credit_and_qty_not_actual_fill():
    state = BotState()
    d = _deps()   # default S2bFeatures() -> actual_fill_accounting is False
    assert d.features.actual_fill_accounting is False
    # the fake broker reports a WORSE fill (1.10, qty 1) than requested; flag-off must IGNORE it
    d = _deps(open_spread=lambda payload: _er(avg_fill=1.10, filled_qty=1, commissions=99.0))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    pos = state.open_positions[0]
    assert pos.credit == 1.70          # order.credit (requested), NOT the actual 1.10 fill
    assert pos.opening_fees == 0.0     # fees never touched when the flag is off


def test_flag_off_pnl_uses_triggering_mark_not_actual_close_fill():
    recs = []
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=2, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    # the fake close reports a materially different actual fill (9.20) than the triggering mark
    # (10.0); flag OFF must use the triggering mark, exactly like before this task.
    d = _deps(mark_position=lambda p: 10.0, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: _er(avg_fill=9.20, commissions=50.0),
              trade_log=lambda r: recs.append(r))
    assert d.features.actual_fill_accounting is False
    run_management_cycle(state, d, today="2026-06-19")
    closes = [r for r in recs if r["event"] == "CLOSE"]
    assert closes[0]["pnl"] == round((3.0 - 10.0) * 100 * 2, 2)   # == -1400.0, uses the mark (10.0)
    assert "gross_pnl" not in closes[0] and "net_pnl" not in closes[0]   # no new fields when OFF


def test_flag_off_legacy_bare_string_return_still_works():
    # back-compat: a test double / caller that still returns a plain status string (not an
    # ExecutionResult) must keep working identically -- result_status() duck-types both.
    state = BotState()
    d = _deps(open_spread=lambda payload: "filled")
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert state.open_positions[0].credit == 1.70
    assert state.open_positions[0].opening_fees == 0.0


def test_result_status_duck_types_string_and_execution_result():
    assert result_status("filled") == "filled"
    assert result_status(_er(status="timeout")) == "timeout"


# ── ManagedPosition.opening_fees default + state persistence round-trip ─────────────────────────

def test_managed_position_opening_fees_defaults_zero():
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=1, expiry="2026-06-19")
    assert pos.opening_fees == 0.0


def test_state_store_roundtrips_opening_fees(tmp_path):
    from bot.app.state_store import save_state, load_state
    p = str(tmp_path / "state.json")
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.71, qty=2, expiry="2026-07-02",
                          opening_fees=2.60)
    s = BotState(open_positions=[pos])
    save_state(s, p)
    loaded = load_state(p)
    assert loaded.open_positions[0].opening_fees == 2.60


def test_state_store_old_file_without_opening_fees_defaults_zero(tmp_path):
    import json
    p = str(tmp_path / "old_state.json")
    with open(p, "w") as f:
        json.dump({
            "open_positions": [{"ticker": "SPY", "short_strike": 568.0, "long_strike": 558.0,
                                "credit": 1.71, "qty": 7, "expiry": "2026-07-02"}],
            "halted": False, "halt_reason": "", "last_entry_date": "", "entries_today": 0,
        }, f)
    from bot.app.state_store import load_state
    loaded = load_state(p)
    assert loaded.open_positions[0].opening_fees == 0.0


# ── live bot (Bot C) must never enable the flag ──────────────────────────────────────────────────

def test_run_s2b_live_defaults_leave_actual_fill_accounting_off():
    # run_s2b_live.py does not pass `features=` to build_and_run/build_deps -> S2bFeatures() default
    import inspect
    from bot.app import run_s2b_live
    src = inspect.getsource(run_s2b_live)
    assert "features=" not in src   # live bot never threads a features override
    assert S2bFeatures().actual_fill_accounting is False


# ── full glue: build_deps + tick(), flag ON end-to-end (not just hand-built Deps) ────────────────

def test_build_deps_tick_end_to_end_records_actual_fill_when_flag_on():
    from bot.app.wiring import build_deps

    chain = {"options": {"option": [
        {"strike": 568.0, "option_type": "put", "bid": 3.40, "ask": 3.50, "greeks": {"delta": -0.36}},
        {"strike": 558.0, "option_type": "put", "bid": 1.60, "ask": 1.70, "greeks": {"delta": -0.18}},
    ]}}

    def http(method, path, params=None, data=None):
        if "/markets/options/expirations" in path:
            return {"expirations": {"date": ["2026-06-19", "2026-06-26"]}}
        if "/markets/options/chains" in path:
            return chain
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        if "/positions" in path:
            return {"positions": "null"}
        if "/orders" in path and method == "POST":
            return {"order": {"id": 42, "status": "ok"}}
        if "/orders/42" in path:
            # sandbox: actual fill WORSE than the requested credit, no fee/commission fields
            return {"order": {"id": 42, "status": "filled",
                              "avg_fill_price": 1.55, "exec_quantity": 1}}
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01),
                      features=S2bFeatures(actual_fill_accounting=True,
                                           est_commission_per_leg_rt=0.65))
    state = tick(BotState(), deps, datetime(2026, 6, 15, 10, 5))   # Monday 10:05
    assert len(state.open_positions) == 1
    pos = state.open_positions[0]
    assert pos.short_strike == 568.0
    assert pos.credit == 1.55          # actual broker fill, not the requested (568 bid - 558 ask)
    assert pos.qty == 1                # actual filled_quantity from the sandbox order
    assert pos.opening_fees == 0.65 * 2 * 1   # synthetic (sandbox reported no commissions)


def test_build_deps_tick_end_to_end_flag_off_unaffected_by_same_fill_data():
    # identical fake broker response as above, but flag OFF -> requested credit/qty, no fees
    from bot.app.wiring import build_deps

    chain = {"options": {"option": [
        {"strike": 568.0, "option_type": "put", "bid": 3.40, "ask": 3.50, "greeks": {"delta": -0.36}},
        {"strike": 558.0, "option_type": "put", "bid": 1.60, "ask": 1.70, "greeks": {"delta": -0.18}},
    ]}}

    def http(method, path, params=None, data=None):
        if "/markets/options/expirations" in path:
            return {"expirations": {"date": ["2026-06-19", "2026-06-26"]}}
        if "/markets/options/chains" in path:
            return chain
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        if "/positions" in path:
            return {"positions": "null"}
        if "/orders" in path and method == "POST":
            return {"order": {"id": 43, "status": "ok"}}
        if "/orders/43" in path:
            return {"order": {"id": 43, "status": "filled",
                              "avg_fill_price": 1.55, "exec_quantity": 1}}
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))   # default features -> flag OFF
    state = tick(BotState(), deps, datetime(2026, 6, 15, 10, 5))
    pos = state.open_positions[0]
    assert pos.credit == 1.70   # requested (568 bid 3.40 - 558 ask 1.70), NOT the actual 1.55 fill
    assert pos.opening_fees == 0.0


# ── critical regression guard: build_deps's close_spread now ALWAYS returns an ExecutionResult
# (regardless of the flag), so run_management_cycle/run_degross_cycle MUST unwrap `.status`
# correctly or a real stop/TP close would be misreported as "failed" and halt the bot. ─────────

def test_build_deps_close_spread_filled_status_is_unwrapped_correctly():
    from bot.app.wiring import build_deps

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        if "/markets/quotes" in path:
            symbol = (params or {}).get("symbols", "")
            # short leg deep ITM (blew out) -> spread value triggers STOP; long leg cheap
            if "P00568000" in symbol:
                return {"quotes": {"quote": {"bid": 10.40, "ask": 10.60}}}
            return {"quotes": {"quote": {"bid": 1.40, "ask": 1.60}}}
        if "/orders" in path and method == "POST":
            return {"order": {"id": 900, "status": "ok"}}
        if "/orders/900" in path:
            return {"order": {"id": 900, "status": "filled"}}   # sandbox: no fill price/fees
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))   # default features -> flag OFF
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=1, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    state, results = run_management_cycle(state, deps, today="2026-06-17")
    assert len(results) == 1
    assert results[0].close_status == "filled"   # NOT the stringified ExecutionResult object
    assert results[0].failed is False
    assert state.open_positions == []            # closed and removed, no false "failed close" halt
