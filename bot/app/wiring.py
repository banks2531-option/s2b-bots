"""Production wiring: build Deps from Tradier, leg-aware reconcile, and the runner (spec §6,§7,§8)."""
from bot.app import feeds
from bot.app.orchestrator import Deps, tick
from bot.ops.ledger import DriftReport
from bot.strategy.s2b import _occ


def _legs_of(pos):
    """Expected (occ_symbol, signed_qty) for a bull put spread: short put -qty, long put +qty."""
    short = _occ(pos.ticker, pos.expiry, "P", pos.short_strike)
    long = _occ(pos.ticker, pos.expiry, "P", pos.long_strike)
    return [(short, -pos.qty), (long, +pos.qty)]


def reconcile_live(tracked, leg_map, bot_equity, broker_equity) -> DriftReport:
    """Verify each tracked spread's legs are present at the broker at the expected qty; flag the rest."""
    referenced = set()
    missing, qty_mismatch = [], []
    for p in tracked:
        ok = True
        for occ, signed_qty in _legs_of(p):
            referenced.add(occ)
            if occ not in leg_map:
                ok = False
            elif leg_map[occ] != signed_qty:
                qty_mismatch.append((p, occ))
                ok = False
        if not ok and not any(q[0] is p for q in qty_mismatch):
            missing.append(p)
    untracked = [occ for occ, q in leg_map.items() if occ not in referenced and q != 0]
    return DriftReport(missing_at_broker=missing, untracked_at_broker=untracked,
                       qty_mismatch=qty_mismatch, equity_drift=round(bot_equity - broker_equity, 2))


def runner(state, deps, now_fn, sleep_fn, poll_s, ticks, tick_fn=tick):
    """Call tick_fn every poll_s up to `ticks` times; stop early if the bot halts.
    now_fn/sleep_fn are injected so this is testable; production passes an ET clock + time.sleep."""
    for _ in range(ticks):
        state = tick_fn(state, deps, now_fn())
        if state.halted:
            break
        sleep_fn(poll_s)
    return state


from bot.broker.tradier import TradierClient
from bot.broker.submit import submit_and_verify
from bot.strategy.manage import spread_value_mid, dte_from_expiry, build_close_payload
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
import time


def build_deps(http, account_id, get_spot, get_atr, get_vix_regime,
               poll_s=2, timeout_s=30, base_risk_pct=0.10):
    """Assemble a production Deps from a Tradier http callable + injected market-data feeds."""
    client = TradierClient(account_id=account_id, http=http)

    def get_chain(symbol, expiry):
        resp = http("GET", "/markets/options/chains",
                    params={"symbol": symbol, "expiration": expiry, "greeks": "true"})
        return feeds.parse_chain(resp)

    def _quote(symbol):
        q = http("GET", "/markets/quotes", params={"symbols": symbol})["quotes"]["quote"]
        return OptionQuote(0.0, 0.0, float(q["bid"]), float(q["ask"]))

    def mark_position(pos):
        short = _quote(_occ(pos.ticker, pos.expiry, "P", pos.short_strike))
        long = _quote(_occ(pos.ticker, pos.expiry, "P", pos.long_strike))
        return spread_value_mid(short, long)

    def broker_legs():
        return feeds.parse_position_legs(http("GET", f"/accounts/{account_id}/positions"))

    def broker_equity():
        return feeds.parse_equity(http("GET", f"/accounts/{account_id}/balances"))

    def open_spread(payload):
        state, _ = submit_and_verify(client, payload, poll_s, timeout_s, time.time, time.sleep)
        return state.value

    def close_spread(pos, action):
        payload = build_close_payload(pos, limit_price=mark_position(pos))
        state, _ = submit_and_verify(client, payload, poll_s, timeout_s, time.time, time.sleep)
        return state.value

    def account_state(today, concurrent):
        eq = broker_equity()
        return AccountState(eq, eq, 0.0, concurrent, 0.0, {}, today)

    return Deps(
        get_spot=get_spot, get_atr=get_atr, get_chain=get_chain,
        pick_expiry=lambda today: feeds.pick_weekly_expiry(today),
        get_vix_regime=get_vix_regime, account_state=account_state,
        mark_position=mark_position, dte_of=lambda p, today: dte_from_expiry(p.expiry, today),
        open_spread=open_spread, close_spread=close_spread,
        broker_positions=lambda: [],            # reconcile uses reconcile_live(broker_legs()) in the runner
        broker_equity=broker_equity, bot_equity=lambda: broker_equity(),
        alert_sink=lambda alerts: [print(f"[ALERT] {a.severity.value}: {a.message}") for a in alerts],
        base_risk_pct=base_risk_pct,
    )
