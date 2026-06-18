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
