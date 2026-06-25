"""Production wiring: build Deps from Tradier and the runner (spec §6,§7,§8)."""
import csv as _csv
import os as _os

from bot.app import feeds
from bot.app.orchestrator import Deps, tick
from bot.app.state_store import save_state
from bot.strategy.s2b import _occ

_LOG_FIELDS = ["event", "date", "ticker", "short", "long", "expiry", "qty",
               "credit", "action", "exit_value", "pnl", "status"]


def make_trade_logger(path):
    """Return a callable that appends a trade-record dict to a CSV (header written once).
    Used per-bot so a shared-account A/B can be measured from separate files."""
    def log(record):
        exists = _os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=_LOG_FIELDS, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow(record)
    return log


def runner(state, deps, now_fn, sleep_fn, poll_s, ticks, tick_fn=tick, state_path=None):
    """Call tick_fn every poll_s up to `ticks` times; stop early if the bot halts.
    now_fn/sleep_fn are injected so this is testable; production passes an ET clock + time.sleep.
    If state_path is given, persist state after every tick so a restart resumes (not orphans)."""
    for i in range(ticks):
        state = tick_fn(state, deps, now_fn())
        if state_path:
            save_state(state, state_path)
        if state.halted:
            break
        if i < ticks - 1:
            sleep_fn(poll_s)
    return state


from bot.broker.tradier import TradierClient
from bot.broker.submit import submit_and_verify
from bot.strategy.manage import spread_value_mid, dte_from_expiry, build_close_payload
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
import time


def build_deps(http, account_id, get_spot, get_atr, get_vix_regime,
               entry_days=frozenset({0}), max_open=1, max_entries_per_day=1, shared_account=False,
               trade_log=(lambda record: None),
               poll_s=2, timeout_s=30, base_risk_pct=0.10):
    """Assemble a production Deps from a Tradier http callable + injected market-data feeds."""
    client = TradierClient(account_id=account_id, http=http)

    def get_chain(symbol, expiry):
        resp = http("GET", "/markets/options/chains",
                    params={"symbol": symbol, "expiration": expiry, "greeks": "true"})
        return feeds.parse_chain(resp)

    def _quote(symbol):
        q = http("GET", "/markets/quotes", params={"symbols": symbol})["quotes"]["quote"]
        bid = q.get("bid")
        ask = q.get("ask")
        if bid is None or ask is None:
            raise ValueError(f"no market for {symbol}")
        return OptionQuote(0.0, 0.0, float(bid), float(ask))

    def _leg_quotes(pos):
        short = _quote(_occ(pos.ticker, pos.expiry, "P", pos.short_strike))
        long = _quote(_occ(pos.ticker, pos.expiry, "P", pos.long_strike))
        return short, long

    def mark_position(pos):
        short, long = _leg_quotes(pos)
        return spread_value_mid(short, long)

    def broker_legs():
        return feeds.parse_position_legs(http("GET", f"/accounts/{account_id}/positions"))

    def broker_equity():
        return feeds.parse_equity(http("GET", f"/accounts/{account_id}/balances"))

    def open_spread(payload):
        state, _ = submit_and_verify(client, payload, poll_s, timeout_s, time.time, time.sleep)
        return state.value

    def close_spread(pos, action):
        short, long = _leg_quotes(pos)
        limit = round(short.ask - long.bid, 2)   # marketable cost-to-close -> fills under stress
        payload = build_close_payload(pos, limit_price=limit)
        state, _ = submit_and_verify(client, payload, poll_s, timeout_s, time.time, time.sleep)
        return state.value

    def account_state(today, concurrent):
        eq = broker_equity()
        return AccountState(eq, eq, 0.0, concurrent, 0.0, {}, today)

    def pick_expiry(today):
        # broker-aware: pick from the actual listed expirations so market holidays are handled
        resp = http("GET", "/markets/options/expirations", params={"symbol": "SPY"})
        return feeds.pick_expiry_from_list(feeds.parse_expirations(resp), today)

    return Deps(
        get_spot=get_spot, get_atr=get_atr, get_chain=get_chain,
        pick_expiry=pick_expiry,
        get_vix_regime=get_vix_regime, account_state=account_state,
        mark_position=mark_position, dte_of=lambda p, today: dte_from_expiry(p.expiry, today),
        open_spread=open_spread, close_spread=close_spread,
        broker_positions=lambda: feeds.reconstruct_spreads(broker_legs()),
        broker_equity=broker_equity, bot_equity=broker_equity,
        alert_sink=lambda alerts: [print(f"[ALERT] {a.severity.value}: {a.message}") for a in alerts],
        base_risk_pct=base_risk_pct,
        entry_days=entry_days, max_open=max_open, max_entries_per_day=max_entries_per_day,
        shared_account=shared_account, trade_log=trade_log,
    )
