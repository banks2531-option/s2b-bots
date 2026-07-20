"""Task 1.3 (spec §8): ExecutionResult + actual-fill accounting + synthetic costs.

Absolute requirement pinned by (d): with `actual_fill_accounting` OFF (the default, and ALWAYS
for the real-money live bot run_s2b_live.py), P&L and position credit/qty must be byte-identical
to the pre-change behavior -- even when the underlying open_spread/close_spread now return a full
ExecutionResult whose fill data disagrees with the requested/quoted values."""
from datetime import datetime

from bot.broker.order_state import ExecutionResult, result_status
from bot.strategy.manage import ManagedPosition, ExitAction
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
from bot.features import S2bFeatures
from bot.ops.monitor import Severity
from bot.app.orchestrator import (BotState, Deps, run_entry_cycle, run_management_cycle,
                                  run_degross_cycle, run_flow_degross_cycle, run_reconcile_cycle, tick)


def _er(status="filled", requested_qty=1, filled_qty=1, avg_fill=None, submitted_limit=None,
        commissions=0.0, regulatory_fees=0.0, order_id=None, normalized_fill=None,
        legs_balanced=True):
    return ExecutionResult(status=status, requested_quantity=requested_qty,
                           filled_quantity=filled_qty, average_fill_price=avg_fill,
                           submitted_limit=submitted_limit, commissions=commissions,
                           regulatory_fees=regulatory_fees, order_id=order_id,
                           normalized_fill=normalized_fill, legs_balanced=legs_balanced)


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
                      features=S2bFeatures(commission_per_contract_per_leg_per_side=0.70))
    result = deps.open_spread({"price": 1.70, "quantity[0]": 2})
    assert isinstance(result, ExecutionResult)
    assert result.status == "filled"
    assert result.filled_quantity == 2
    assert result.average_fill_price == 1.65        # actual broker fill, not the requested 1.70
    assert result.submitted_limit == 1.70
    # sandbox reported no commissions -> synthetic fallback:
    # commission_per_contract_per_leg_per_side * 2 legs * qty
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
    assert result.commissions == 0.65 * 2 * 3  # default commission_per_contract_per_leg_per_side = 0.65


def test_open_spread_live_shaped_leg_array_normalizes_credit_and_contracts():
    """Live-shaped order: order-level avg_fill_price is NEGATIVE (-0.92) and exec_quantity counts
    LEGS (2.0) for a 1-contract vertical -- both poisonous if read directly (advisor nextsteps2
    section 1). _to_execution_result must prefer the leg array and report a POSITIVE credit and
    the CONTRACT count, not the leg count."""
    from bot.app.wiring import build_deps
    from bot.tests.test_spread_fill import _open_order

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        if "/orders" in path and method == "POST":
            return {"order": {"id": 9001, "status": "ok"}}
        if "/orders/9001" in path:
            # live-shaped: leg array with sell_to_open 1.55 / buy_to_open 0.63
            return {"order": _open_order()}
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01),
                      features=S2bFeatures(commission_per_contract_per_leg_per_side=0.70))
    result = deps.open_spread({"price": 0.90, "quantity[0]": 1})
    assert isinstance(result, ExecutionResult)
    assert result.average_fill_price == 0.92       # POSITIVE credit (1.55 - 0.63), not -0.92
    assert result.filled_quantity == 1              # contracts, not the leg count (2.0)
    assert result.normalized_fill is not None
    assert result.legs_balanced is True
    assert result.commissions == 0.70 * 2 * 1       # synthetic fallback uses the CONTRACT count


def test_fill_reconcile_ledger_emitted_for_live_shaped_order(capsys):
    """Advisor nextsteps2 section 1: the line a human reconciles against the broker statement.

    It must carry BOTH the derived numbers and the order-level ones we ignored -- the whole claim
    under validation is that those two order-level fields are wrong on the live broker, and a
    ledger that omits them cannot demonstrate it."""
    from bot.app.wiring import build_deps
    from bot.tests.test_spread_fill import _open_order

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        if "/orders" in path and method == "POST":
            return {"order": {"id": 9001, "status": "ok"}}
        if "/orders/9001" in path:
            return {"order": _open_order()}
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01), features=S2bFeatures())
    deps.open_spread({"price": 0.90, "quantity[0]": 1})
    out = capsys.readouterr().out

    assert "FILL_RECONCILE" in out
    assert "derived_contracts=1" in out          # contracts, not the leg count
    assert "derived_net=0.9200" in out           # positive magnitude
    assert "cash_flow=credit" in out
    assert "balanced=True" in out
    # the numbers we deliberately did NOT use, carried for side-by-side comparison
    assert "ignored_order_level_qty=2.0" in out
    assert "ignored_order_level_px=-0.92" in out


def test_fill_reconcile_ledger_silent_for_sandbox_shaped_order(capsys):
    """No leg array -> nothing was normalized -> there is nothing to reconcile. Emitting a line
    here would imply the leg reconstruction ran when it did not."""
    from bot.app.wiring import build_deps

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        if "/orders" in path and method == "POST":
            return {"order": {"id": 7001, "status": "ok"}}
        if "/orders/7001" in path:
            return {"order": {"id": 7001, "status": "filled", "avg_fill_price": 0.90}}
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01), features=S2bFeatures())
    deps.open_spread({"price": 0.90, "quantity[0]": 1})
    assert "FILL_RECONCILE" not in capsys.readouterr().out


def test_fill_reconcile_ledger_silent_for_unfilled_ladder_rung(capsys):
    """A still-working ladder rung normalizes to a legitimate zero-fill. Bot B runs both ladders
    ON, so without this gate one order would emit 6-12 lines, nearly all derived_contracts=0. A
    ledger you have to filter noise out of is one nobody finishes reading."""
    from bot.app.wiring import build_deps
    from bot.tests.test_spread_fill import _open_order

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        if "/orders" in path and method == "POST":
            return {"order": {"id": 9001, "status": "ok"}}
        if "/orders/9001" in path:
            order = _open_order(short_qty=0, long_qty=0)
            order["status"] = "canceled"
            return {"order": order}
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01), features=S2bFeatures())
    deps.open_spread({"price": 0.90, "quantity[0]": 1})
    assert "FILL_RECONCILE" not in capsys.readouterr().out


def test_open_spread_sandbox_shaped_order_unaffected_by_leg_normalization():
    """Regression guard (advisor nextsteps2 section 1): the sandbox order shape carries no leg
    array, so normalize_spread_fill returns None and _to_execution_result must fall back to
    EXACTLY the pre-existing order-level behaviour -- same filled_quantity, same
    average_fill_price, same commissions as before this change. This is the same order shape as
    test_open_spread_returns_execution_result_with_fill_fields above."""
    from bot.app.wiring import build_deps

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        if "/orders" in path and method == "POST":
            return {"order": {"id": 555, "status": "ok"}}
        if "/orders/555" in path:
            # sandbox: reports a fill price + quantity but NEVER commissions/fees, and NO leg array
            return {"order": {"id": 555, "status": "filled",
                              "avg_fill_price": 1.65, "exec_quantity": 2}}
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01),
                      features=S2bFeatures(commission_per_contract_per_leg_per_side=0.70))
    result = deps.open_spread({"price": 1.70, "quantity[0]": 2})
    assert result.filled_quantity == 2
    assert result.average_fill_price == 1.65
    assert result.commissions == 0.70 * 2 * 2
    assert result.normalized_fill is None
    assert result.legs_balanced is True


def test_open_and_close_synthetic_commissions_sum_to_round_trip_cross_check():
    # Cross-check tying wiring's synthetic per-order commission to the cost-gate model: an OPEN plus
    # its matching CLOSE (each = one side = 2 legs) must sum to EXACTLY the cost gate's
    # round_trip_commission_per_contract for the same features + filled qty. Fails the instant the
    # two sides desync -- the exact bug this task fixed.
    from bot.app.wiring import build_deps
    from bot.strategy.cost_gate import round_trip_commission_per_contract

    f = S2bFeatures(commission_per_contract_per_leg_per_side=0.70)

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        if path == "/markets/quotes":                      # close_spread looks up the two put legs
            sym = params["symbols"]
            bid, ask = ((3.40, 3.50) if sym.endswith("P00568000") else (1.60, 1.70))
            return {"quotes": {"quote": {"symbol": sym, "bid": bid, "ask": ask}}}
        if "/orders" in path and method == "POST":
            return {"order": {"id": 900, "status": "ok"}}
        if "/orders/900" in path:                          # sandbox: fill + qty, NEVER commissions
            return {"order": {"id": 900, "status": "filled",
                              "avg_fill_price": 1.65, "exec_quantity": 2}}
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01), features=f)
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.70, qty=2, expiry="2026-06-19")

    open_res = deps.open_spread({"price": 1.70, "quantity[0]": 2})
    close_res = deps.close_spread(pos, ExitAction.STOP)

    assert open_res.filled_quantity == close_res.filled_quantity == 2
    total = open_res.commissions + close_res.commissions
    assert total == round_trip_commission_per_contract(f) * open_res.filled_quantity
    assert total == 0.70 * 2 * 2 * 2   # 2 legs x 2 sides x 0.70 x 2 qty = 5.60


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


# ── (c2) de-gross / flow-degross cycles must honor actual-fill accounting too (Fix 1) ────────────
# These cycles close real positions exactly like run_management_cycle, so they must mirror its
# flag-ON gross/net accounting, and stay byte-identical to the pre-Task-1.3 mark-based pnl when off.

def test_degross_pnl_uses_actual_close_fill_and_nets_fees_when_flag_on():
    from bot.regime.state import RegimeState
    recs = []
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=2, expiry="2026-06-19",
                          opening_fees=1.30)
    state = BotState(open_positions=[pos])
    # triggering mark says 10.0; the ACTUAL close fill is materially better (9.20)
    d = _deps(features=S2bFeatures(actual_fill_accounting=True),
              mark_position=lambda p: 10.0, degross_on_risk_off=True,
              close_spread=lambda p, a: _er(avg_fill=9.20, commissions=1.30, regulatory_fees=0.05),
              trade_log=lambda r: recs.append(r))
    run_degross_cycle(state, d, today="2026-06-17", regime=RegimeState(trend_regime="risk_off"))
    deg = [r for r in recs if r["event"] == "DEGROSS"]
    assert len(deg) == 1
    rec = deg[0]
    # gross uses the ACTUAL fill (9.20), never the triggering mark (10.0):
    # gross = (3.0 - 9.20) * 100 * 2 = -1240.0
    assert rec["gross_pnl"] == -1240.0
    assert rec["net_pnl"] == round(-1240.0 - 1.30 - 1.30 - 0.05, 2)
    assert rec["pnl"] == rec["net_pnl"]


def test_degross_pnl_off_flag_matches_legacy_mark_based_calc():
    from bot.regime.state import RegimeState
    recs = []
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=2, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 10.0, degross_on_risk_off=True,
              close_spread=lambda p, a: _er(avg_fill=9.20, commissions=50.0),
              trade_log=lambda r: recs.append(r))
    assert d.features.actual_fill_accounting is False
    run_degross_cycle(state, d, today="2026-06-17", regime=RegimeState(trend_regime="risk_off"))
    deg = [r for r in recs if r["event"] == "DEGROSS"]
    assert deg[0]["pnl"] == round((3.0 - 10.0) * 100 * 2, 2)   # == -1400.0, uses the mark, not the fill
    assert "gross_pnl" not in deg[0] and "net_pnl" not in deg[0]


def test_flow_degross_pnl_uses_actual_close_fill_and_nets_fees_when_flag_on():
    from bot.regime.state import RegimeState
    recs = []
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=2, expiry="2026-06-19",
                          entry_date="2026-06-17", opening_fees=1.30)
    state = BotState(open_positions=[pos], prev_flow_bias="bullish")
    # not-yet-profitable per the triggering mark (10.0 >= credit 3.0); actual close fill is 9.20
    d = _deps(features=S2bFeatures(actual_fill_accounting=True),
              mark_position=lambda p: 10.0, degross_on_flow_flip=True,
              close_spread=lambda p, a: _er(avg_fill=9.20, commissions=1.30, regulatory_fees=0.05),
              trade_log=lambda r: recs.append(r))
    run_flow_degross_cycle(state, d, today="2026-06-17", regime=RegimeState(flow_bias="bearish"))
    fdeg = [r for r in recs if r["event"] == "FLOW_DEGROSS"]
    assert len(fdeg) == 1
    rec = fdeg[0]
    assert rec["gross_pnl"] == -1240.0    # (3.0 - 9.20) * 100 * 2, the ACTUAL fill, not the mark (10.0)
    assert rec["net_pnl"] == round(-1240.0 - 1.30 - 1.30 - 0.05, 2)
    assert rec["pnl"] == rec["net_pnl"]


def test_flow_degross_pnl_off_flag_matches_legacy_mark_based_calc():
    from bot.regime.state import RegimeState
    recs = []
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=2, expiry="2026-06-19",
                          entry_date="2026-06-17")
    state = BotState(open_positions=[pos], prev_flow_bias="bullish")
    d = _deps(mark_position=lambda p: 10.0, degross_on_flow_flip=True,
              close_spread=lambda p, a: _er(avg_fill=9.20, commissions=50.0),
              trade_log=lambda r: recs.append(r))
    assert d.features.actual_fill_accounting is False
    run_flow_degross_cycle(state, d, today="2026-06-17", regime=RegimeState(flow_bias="bearish"))
    fdeg = [r for r in recs if r["event"] == "FLOW_DEGROSS"]
    assert fdeg[0]["pnl"] == round((3.0 - 10.0) * 100 * 2, 2)
    assert "gross_pnl" not in fdeg[0] and "net_pnl" not in fdeg[0]


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

def test_run_s2b_live_disables_actual_fill_accounting_live_broker_bug():
    # actual_fill_accounting DISABLED on the live bot 2026-07-17: it is broken against the real
    # Tradier broker (order-level avg_fill_price is negative for a credit -> sign flip; order-level
    # exec_quantity counts legs not contracts -> qty doubled). Off = record the requested credit/qty.
    from bot.app.run_s2b_live import LIVE_FEATURES
    assert LIVE_FEATURES.actual_fill_accounting is False
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
                                           commission_per_contract_per_leg_per_side=0.65))
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


# ── Task 2 (advisor-mandated PARTIAL-FILL accounting) ────────────────────────────────────────────
# A partial fill does NOT reliably surface as status=="partially_filled": on the Bot C non-ladder
# path a partial that got its remainder cancelled comes back as status "canceled"/"timeout" but with
# filled_quantity > 0. So the gate keys on filled_quantity, NOT the status string. The submit layer
# has ALREADY cancelled the unfilled remainder (both ladder and non-ladder paths), so the
# orchestrator NEVER cancels here -- Task 2 is purely about ACCOUNTING for the filled portion.

# --- 2a: entry partial fill ----------------------------------------------------------------------

def test_entry_partial_records_filled_qty_flags_off_bot_c():
    # Bot C (all flags off): the broker fills only 1 of the 2 the strategy sized, then cancels the
    # rest -> surfaces as status "canceled" with filled_quantity 1. We MUST record the position at
    # the ACTUALLY-filled contract count (1), not the requested/status -- tracking more than the
    # broker filled is the exact bug this fixes (untracked_at_broker -> HALT).
    recs = []
    state = BotState()
    d = _deps(open_spread=lambda payload: _er(status="canceled", requested_qty=5, filled_qty=1,
                                              avg_fill=None),
              trade_log=lambda r: recs.append(r))
    assert d.features.actual_fill_accounting is False
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "canceled"                     # returned status is still the raw broker status
    assert len(state.open_positions) == 1
    pos = state.open_positions[0]
    assert pos.qty == 1                           # the ACTUALLY-filled contracts (order sized 2)
    assert pos.credit == 1.70                     # Bot C pricing convention preserved (order.credit)
    assert pos.opening_fees == 0.0                # fees never touched when the flag is off
    assert state.entries_today == 1               # a partial still consumed the day's entry
    # Fix A: the OPEN row's status cell reads "partial_fill" (self-describing) even on Bot C, and
    # carries no extra key -> visible in the 12-column live CSV without a schema change.
    open_rec = [r for r in recs if r["event"] == "OPEN"][0]
    assert open_rec["status"] == "partial_fill" and "partial" not in open_rec


def test_entry_partial_with_actual_fill_accounting_on():
    recs = []
    state = BotState()
    d = _deps(features=S2bFeatures(actual_fill_accounting=True),
              open_spread=lambda payload: _er(status="canceled", requested_qty=5,
                                              filled_qty=1, avg_fill=1.55, commissions=2.60),
              trade_log=lambda r: recs.append(r))
    state, info = run_entry_cycle(state, d, MONDAY)
    # the RETURN value is still the raw broker status (nothing downstream that keys on it changes) ...
    assert info == "canceled"
    assert len(state.open_positions) == 1
    pos = state.open_positions[0]
    assert pos.qty == 1                            # actual filled_quantity
    assert pos.credit == 1.55                      # actual fill price (flag on)
    assert pos.opening_fees == 2.60                # commissions carried through
    # Fix A: ... but the OPEN row's `status` cell reads the self-describing "partial_fill" (NOT the
    # raw "canceled") and carries NO extra "partial" key -- so it stays inside the 12-column CSV.
    open_rec = [r for r in recs if r["event"] == "OPEN"][0]
    assert open_rec["status"] == "partial_fill"
    assert "partial" not in open_rec
    assert open_rec["qty"] == 1


def test_entry_zero_fill_records_nothing_unchanged():
    # byte-identical to today's else path: nothing recorded, no entry consumed.
    state = BotState()
    d = _deps(open_spread=lambda payload: _er(status="timeout", filled_qty=0))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "timeout"
    assert len(state.open_positions) == 0
    assert state.entries_today == 0


def test_entry_full_fill_records_requested_qty_bot_c():
    # full fill, flag OFF: qty is the REQUESTED order.qty (2), NOT the result's filled_quantity (1)
    # -- the full-fill path is byte-identical to before Task 2.
    recs = []
    state = BotState()
    d = _deps(open_spread=lambda payload: _er(status="filled", filled_qty=1),
              trade_log=lambda r: recs.append(r))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    pos = state.open_positions[0]
    assert pos.qty == 2
    assert pos.credit == 1.70
    assert pos.opening_fees == 0.0
    # Fix A byte-identical guard: the full-fill OPEN row keeps status "filled" and no extra keys.
    open_rec = [r for r in recs if r["event"] == "OPEN"][0]
    assert open_rec["status"] == "filled"
    assert "partial" not in open_rec


# --- 2b: close partial fill ----------------------------------------------------------------------

def test_close_partial_books_pnl_on_filled_qty_and_keeps_remainder():
    # position qty 8; a STOP triggers (mark 3.5 >= credit*(1+stop_mult)=3.0); the broker fills only
    # 3 of the 8 and cancels the rest -> surfaces as failed=True/status "canceled" with
    # filled_quantity 3. We book P&L on the 3 filled, reduce the position to 5, and KEEP it.
    recs = []
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=8, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(features=S2bFeatures(actual_fill_accounting=True),
              mark_position=lambda p: 3.5, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: _er(status="canceled", requested_qty=8, filled_qty=3,
                                            avg_fill=0.40),
              trade_log=lambda r: recs.append(r))
    state, results = run_management_cycle(state, d, today="2026-06-19")
    closes = [r for r in recs if r["event"] == "CLOSE"]
    assert len(closes) == 1
    rec = closes[0]
    assert rec["qty"] == 3                          # booked on the FILLED contracts, not the full 8
    # gross uses the ACTUAL close fill (0.40) on 3 contracts: (1.00 - 0.40) * 100 * 3 = 180.0
    assert rec["gross_pnl"] == 180.0
    assert rec["net_pnl"] == 180.0
    assert rec["pnl"] == 180.0
    assert len(state.open_positions) == 1           # the position SURVIVES ...
    assert state.open_positions[0].qty == 5         # ... at the still-working remainder
    assert state.halted is False                    # a partial that filled SOME is progress, not a halt


def test_close_partial_flag_off_uses_mark_and_keeps_remainder_no_halt():
    # Bot C guard: flag OFF -> P&L uses the triggering MARK (not the fill) on the FILLED qty, the
    # remainder survives at reduced qty, and a partial never trips the "failed close" halt.
    recs = []
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=8, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 3.5, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: _er(status="canceled", requested_qty=8, filled_qty=3,
                                            avg_fill=0.40),
              trade_log=lambda r: recs.append(r))
    assert d.features.actual_fill_accounting is False
    state, results = run_management_cycle(state, d, today="2026-06-19")
    rec = [r for r in recs if r["event"] == "CLOSE"][0]
    assert rec["qty"] == 3
    assert rec["pnl"] == round((1.00 - 3.5) * 100 * 3, 2)     # == -750.0, uses the mark on 3 contracts
    assert "gross_pnl" not in rec and "net_pnl" not in rec
    assert state.open_positions[0].qty == 5
    assert state.halted is False


def test_close_full_fill_books_full_qty_and_removes_bot_c():
    # byte-identical full-close guard (flag OFF): P&L on the FULL qty via the mark, position removed.
    recs = []
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=4, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 3.5, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: _er(status="filled", requested_qty=4, filled_qty=4,
                                            avg_fill=0.40),
              trade_log=lambda r: recs.append(r))
    assert d.features.actual_fill_accounting is False
    state, results = run_management_cycle(state, d, today="2026-06-19")
    rec = [r for r in recs if r["event"] == "CLOSE"][0]
    assert rec["qty"] == 4                                     # full qty
    assert rec["pnl"] == round((1.00 - 3.5) * 100 * 4, 2)      # mark-based, full qty == -1000.0
    assert state.open_positions == []                          # fully closed -> removed
    assert state.halted is False


def test_close_full_failed_zero_fill_still_halts_bot_c():
    # byte-identical failed-close guard: a close that filled NOTHING is a stuck stop -> alert + halt.
    recs = []
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=4, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 3.5, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: _er(status="timeout", requested_qty=4, filled_qty=0),
              trade_log=lambda r: recs.append(r))
    state, results = run_management_cycle(state, d, today="2026-06-19")
    assert results[0].failed is True
    assert state.open_positions == [pos]                      # nothing filled -> kept
    assert state.halted is True and "close" in state.halt_reason
    # log-only byte-identical guard: a ZERO-fill stuck close logs the FULL position qty (4), NOT
    # cfq (0) -- restores the pre-Task-2 CLOSE-row qty for Bot C.
    close_rec = [r for r in recs if r["event"] == "CLOSE"][0]
    assert close_rec["qty"] == 4
    assert "pnl" not in close_rec                             # no P&L booked on a nothing-filled close


def test_close_partial_opening_fees_prorate_and_telescope():
    # Fix 1: net P&L on a partial close must deduct only the PRORATED share of opening_fees, and the
    # remainder must keep the rest -- so across any number of partials plus the final full close the
    # total opening_fees deducted equals the ORIGINAL exactly (no double-count, no under-count).
    recs = []
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=10, expiry="2026-06-19",
                          opening_fees=3.00)
    state = BotState(open_positions=[pos])
    fills = iter([3, 3, 4])   # two genuine partials, then the final remainder (a full close of 4/4)
    d = _deps(features=S2bFeatures(actual_fill_accounting=True),
              mark_position=lambda p: 3.5, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: _er(status="canceled", requested_qty=p.qty,
                                            filled_qty=next(fills), avg_fill=0.40),
              trade_log=lambda r: recs.append(r))
    for _ in range(3):        # three sequential management cycles, one tranche each
        state, _ = run_management_cycle(state, d, today="2026-06-19")
    closes = [r for r in recs if r["event"] == "CLOSE"]
    assert len(closes) == 3
    # close commissions/reg fees are 0 here, so (gross - net) on each close == the opening_fees booked
    fee_deductions = [round(r["gross_pnl"] - r["net_pnl"], 2) for r in closes]
    assert sum(fee_deductions) == 3.00           # telescopes to the ORIGINAL opening_fees, exactly
    assert all(fd > 0 for fd in fee_deductions)  # each close booked a positive prorated share
    assert state.open_positions == []            # final tranche fully closed -> removed


# ── Failed take-profit: WARN + retry, never halt (STOP/TIME_EXIT/ERROR still halt) ───────────────
# A failed TAKE_PROFIT is a winning position we merely didn't capture this tick -- NOT a risk event.
# It must NOT set the sticky "failed close" halt (the root cause of Bot C's recurring $1-wing halt).

def test_failed_take_profit_warns_and_does_not_halt():
    # winning position: mark 0.40 <= credit*(1-tp_pct)=0.50 -> TAKE_PROFIT; close fills NOTHING.
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=4, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 0.40, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: _er(status="canceled", requested_qty=4, filled_qty=0))
    state, results = run_management_cycle(state, d, today="2026-06-19")
    assert results[0].action == ExitAction.TAKE_PROFIT and results[0].failed is True
    assert state.halted is False                      # a failed take-profit must NOT halt
    assert state.open_positions == [pos]              # position kept for retry next tick

def test_failed_take_profit_is_retried_next_cycle():
    # the same TAKE_PROFIT is re-attempted on the next management cycle (close_fn called again).
    calls = {"n": 0}
    def close_fn(p, a):
        calls["n"] += 1
        return _er(status="canceled", requested_qty=4, filled_qty=0)
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=4, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 0.40, dte_of=lambda p, today: 5, close_spread=close_fn)
    state, _ = run_management_cycle(state, d, today="2026-06-19")
    state, _ = run_management_cycle(state, d, today="2026-06-19")
    assert calls["n"] == 2                             # re-attempted, not abandoned
    assert state.halted is False

def test_failed_stop_still_halts_bot_c():
    # losing position: mark 3.5 >= credit*(1+stop_mult)=3.00 -> STOP; close fills nothing -> HALT.
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=4, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 3.5, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: _er(status="timeout", requested_qty=4, filled_qty=0))
    state, results = run_management_cycle(state, d, today="2026-06-19")
    assert results[0].action == ExitAction.STOP
    assert state.halted is True and "close" in state.halt_reason

def test_failed_time_exit_still_halts():
    # dte 0 (<= time_exit_dte 1) with a non-stop, non-tp mark -> TIME_EXIT; close fails -> HALT.
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=4, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 1.00, dte_of=lambda p, today: 0,
              close_spread=lambda p, a: _er(status="canceled", requested_qty=4, filled_qty=0))
    state, results = run_management_cycle(state, d, today="2026-06-19")
    assert results[0].action == ExitAction.TIME_EXIT
    assert state.halted is True

def test_failed_close_that_raised_still_halts():
    # close_fn raises -> monitor_positions records ExitAction.ERROR, failed=True -> HALT (conservative).
    def boom(p, a):
        raise RuntimeError("broker 500")
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=4, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 0.40, dte_of=lambda p, today: 5, close_spread=boom)
    state, results = run_management_cycle(state, d, today="2026-06-19")
    assert results[0].action == ExitAction.ERROR
    assert state.halted is True

# ── Task 6 (advisor nextsteps2 section 1 reaction): unmatched legs halt new entries ──────────────
# normalize_spread_fill already reports the COVERED count (min of the legs) and balanced=False for
# an unequal-leg fill (e.g. 3 shorts vs 2 longs = 2 covered spreads + 1 NAKED short). What's under
# test here is the reaction: stop opening anything NEW until a human reconciles, while the position
# itself stays tracked at the covered count -- a halt must never abandon live exposure.

def test_unbalanced_open_order_normalizes_to_covered_count():
    # Unit-level sanity check (exercises the earlier task's normalize_spread_fill directly, not the
    # orchestrator): 3 shorts vs 2 longs -> 2 covered contracts, balanced False.
    from bot.broker.spread_fill import normalize_spread_fill
    from bot.tests.test_spread_fill import _open_order
    f = normalize_spread_fill(_open_order(short_qty=3, long_qty=2))
    assert f.contracts == 2
    assert f.balanced is False


def test_entry_unbalanced_legs_halts_new_entries_and_records_covered_count_bot_c():
    # Bot C shape: actual_fill_accounting OFF (the real live setting). An unequal-leg fill realistically
    # surfaces as a partial (remainder legs still working/cancelled), so it lands on the SAME
    # partial-fill accounting path Task 2 built -- which already records open_filled_qty
    # unconditionally, regardless of the flag. That is what makes the covered count (2), not the
    # short-leg count (3), get recorded here even though the flag is off.
    from bot.broker.spread_fill import normalize_spread_fill
    from bot.tests.test_spread_fill import _open_order
    normalized = normalize_spread_fill(_open_order(short_qty=3, long_qty=2))
    assert normalized.contracts == 2 and normalized.balanced is False   # sanity on the fixture

    alerts = []
    state = BotState()
    d = _deps(open_spread=lambda payload: _er(status="canceled", requested_qty=3, filled_qty=2,
                                              avg_fill=normalized.net_price,
                                              normalized_fill=normalized, legs_balanced=False),
              alert_sink=lambda a: alerts.extend(a))
    assert d.features.actual_fill_accounting is False   # Bot C's real setting

    state, info = run_entry_cycle(state, d, MONDAY)

    assert state.halted is True
    assert state.halt_reason == "unmatched legs"
    assert len(state.open_positions) == 1
    assert state.open_positions[0].qty == 2              # covered count, NOT the short-leg count (3)
    assert any(a.severity == Severity.CRITICAL for a in alerts)


def test_entry_unbalanced_legs_halts_new_entries_full_fill_status_flag_on():
    # Companion case: the broker reports the order status "filled" outright (both legs' orders
    # closed out, just at unequal quantities) with actual_fill_accounting ON -- the full-fill branch
    # then also uses filled_quantity (the covered count) rather than the requested order.qty.
    from bot.broker.spread_fill import normalize_spread_fill
    from bot.tests.test_spread_fill import _open_order
    # 3 shorts against 1 long -> covered count 1. Deliberately NOT 2: this harness sizes
    # order.qty to 2, so asserting 2 would pass even if the covered count were ignored.
    normalized = normalize_spread_fill(_open_order(short_qty=3, long_qty=1))
    assert normalized.contracts == 1 and normalized.balanced is False

    alerts = []
    state = BotState()
    d = _deps(features=S2bFeatures(actual_fill_accounting=True),
              open_spread=lambda payload: _er(status="filled", requested_qty=3, filled_qty=1,
                                              avg_fill=normalized.net_price,
                                              normalized_fill=normalized, legs_balanced=False),
              alert_sink=lambda a: alerts.extend(a))
    state, info = run_entry_cycle(state, d, MONDAY)

    assert state.halted is True
    assert state.halt_reason == "unmatched legs"
    assert len(state.open_positions) == 1
    assert state.open_positions[0].qty == 1
    assert any(a.severity == Severity.CRITICAL for a in alerts)


def test_entry_unbalanced_legs_full_fill_records_covered_count_with_flag_OFF():
    """Bot C's REAL configuration: actual_fill_accounting=False, broker reports status "filled".

    The full-fill branch normally trusts order.qty when the flag is off, because "filled" has
    always meant the REQUESTED quantity filled. Leg normalization breaks that invariant -- here
    the broker says "filled" but only 2 of 3 spreads are covered by matching legs. Recording 3
    would track a position larger than the broker actually gave us, which is the untracked-at-
    broker fault that halted this account before. The covered count must win regardless of the
    flag, exactly as it already does on the partial-fill branch."""
    from bot.broker.spread_fill import normalize_spread_fill
    from bot.tests.test_spread_fill import _open_order
    # 3 shorts against 1 long -> covered count 1. This harness naturally sizes order.qty to 2, so
    # a covered count of 1 is the only value that DISCRIMINATES: asserting 2 here would pass
    # whether or not the fix exists.
    normalized = normalize_spread_fill(_open_order(short_qty=3, long_qty=1))
    assert normalized.contracts == 1 and normalized.balanced is False

    state = BotState()
    d = _deps(features=S2bFeatures(actual_fill_accounting=False),
              open_spread=lambda payload: _er(status="filled", requested_qty=3, filled_qty=1,
                                              avg_fill=normalized.net_price,
                                              normalized_fill=normalized, legs_balanced=False))
    state, info = run_entry_cycle(state, d, MONDAY)

    assert state.halted is True
    assert len(state.open_positions) == 1
    assert state.open_positions[0].qty == 1, (
        "must record the COVERED count (1), not the sized order.qty (2) -- tracking more than "
        "the broker filled is the untracked_at_broker fault")


def test_entry_unbalanced_legs_with_ZERO_covered_records_no_position():
    """Branch review finding 2. The covered-count override read `if covered_qty:`, and 0 is falsy,
    so a fill covering NOTHING fell through to the sized order.qty and booked a complete spread
    that does not exist -- in the one branch whose entire purpose is 'never invent a spread'.

    Reachable: one short filled, the long leg unfilled, broker still reporting status "filled".
    That is a NAKED SHORT PUT. Recording it as a covered spread would understate the position's
    max loss by the entire width of the wing, and every downstream stop and risk budget would be
    computed against collateral the account does not have."""
    from bot.broker.spread_fill import normalize_spread_fill
    from bot.tests.test_spread_fill import _open_order
    normalized = normalize_spread_fill(_open_order(short_qty=1, long_qty=0))
    assert normalized.contracts == 0 and normalized.balanced is False

    state = BotState()
    d = _deps(features=S2bFeatures(actual_fill_accounting=False),
              open_spread=lambda payload: _er(status="filled", requested_qty=1, filled_qty=0,
                                              avg_fill=normalized.net_price,
                                              normalized_fill=normalized, legs_balanced=False))
    state, info = run_entry_cycle(state, d, MONDAY)

    assert state.open_positions == [], (
        "zero covered contracts must record NO position -- booking order.qty here invents a "
        "covered spread on top of a naked short")
    assert state.halted is True and state.halt_reason == "unmatched legs"


def test_entry_balanced_fill_never_halts_sandbox_regression():
    # Regression guard: normalize_spread_fill returns None for sandbox payloads (Bot B), which
    # wiring.py maps to legs_balanced=True by default -- confirm a normal balanced fill never trips
    # the new halt.
    state = BotState()
    d = _deps(open_spread=lambda payload: _er(status="filled", filled_qty=2, legs_balanced=True))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert state.halted is False
    assert state.halt_reason == ""


def test_entry_zero_fill_balanced_never_halts():
    # A fully unfilled order normalizes to contracts=0, balanced=True -- must not halt.
    state = BotState()
    d = _deps(open_spread=lambda payload: _er(status="timeout", filled_qty=0, legs_balanced=True))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert state.halted is False


def test_reconcile_does_not_auto_clear_unmatched_legs_halt():
    # "unmatched legs" is deliberately NOT one of the reasons run_reconcile_cycle auto-clears
    # ("failed close", "reconcile drift"): an unmatched leg needs a human to reconcile the broker
    # book against ours, not a later clean reconcile. This is the test that stops someone later
    # adding our reason to that auto-clear tuple without noticing.
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=2, expiry="2026-06-19")
    state = BotState(open_positions=[pos], halted=True, halt_reason="unmatched legs")
    d = _deps(broker_positions=lambda: [pos])   # even a CLEAN reconcile...
    state, _ = run_reconcile_cycle(state, d)
    assert state.halted is True and state.halt_reason == "unmatched legs"   # ...must not clear it


def test_successful_take_profit_still_removes_position_no_halt():
    # byte-identical happy path: TAKE_PROFIT fills fully -> position removed, no halt.
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=4, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 0.40, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: _er(status="filled", requested_qty=4, filled_qty=4))
    state, results = run_management_cycle(state, d, today="2026-06-19")
    assert state.open_positions == []
    assert state.halted is False
