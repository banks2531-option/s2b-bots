"""S2b bot entrypoint — assembles live feeds and runs the tick loop. SANDBOX-FIRST.

Thin, mostly-untestable glue (the testable parsers live in bot.app.feeds and are unit-tested).
Credentials come only from the environment (C8):
  TRADIER_TOKEN          (required)  — Tradier access token
  TRADIER_BASE_URL       (default sandbox: https://sandbox.tradier.com/v1)
  TRADIER_ACCOUNT_ID     (required)  — the account to trade
  I_UNDERSTAND_THIS_IS_LIVE=yes      — REQUIRED to run against api.tradier.com (live)

Run a single dry cycle against sandbox:
  TRADIER_TOKEN=... TRADIER_ACCOUNT_ID=VA... python -m bot.app.run_s2b --ticks 1

The market-data feeds (spot, ATR, VIX regime) and the broker actions are wired through
bot.app.wiring.build_deps; the orchestrator's tick reconciles broker truth, fires verified
stops, and enters one cushioned Monday SPY put spread, halt-gated.
"""
import os
from datetime import datetime, timedelta

from bot.broker.tradier import make_http_from_env
from bot.app import feeds
from bot.app.wiring import build_deps, runner
from bot.app.orchestrator import BotState


def fetch_spot(http, symbol):
    """Last trade price of the underlying from Tradier quotes."""
    q = http("GET", "/markets/quotes", params={"symbols": symbol})["quotes"]["quote"]
    return float(q["last"])


def fetch_atr(http, symbol, today, lookback_days=40, n=14):
    """ATR(n) of the underlying from Tradier daily history."""
    start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    resp = http("GET", "/markets/history",
                params={"symbol": symbol, "interval": "daily", "start": start, "end": today})
    return feeds.compute_atr(feeds.parse_history(resp), n=n)


def fetch_vix_regime(lookback=120):
    """(pct_rank, 1-day change) of VIX from a recent yfinance series (the size-cut input)."""
    import yfinance as yf
    c = yf.download("^VIX", period="6mo", progress=False, auto_adjust=False)["Close"]
    if hasattr(c, "columns"):
        c = c.iloc[:, 0]
    series = [float(x) for x in c.dropna().values][-lookback:]
    return feeds.vix_regime(series)


def parse_args(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Run the S2b bot (sandbox-first).")
    ap.add_argument("--ticks", type=int, default=1, help="number of tick cycles to run")
    ap.add_argument("--poll-seconds", type=int, default=300, help="seconds between ticks")
    return ap.parse_args(argv)


def build_and_run(ticks, poll_seconds, entry_days=frozenset({0}), max_open=1, label="MONDAY"):
    """Shared bot core. The A/B variable is (entry_days, max_open) — Monday-only vs all-days."""
    import time
    from zoneinfo import ZoneInfo

    base = os.environ.get("TRADIER_BASE_URL", "https://sandbox.tradier.com/v1")
    account_id = os.environ.get("TRADIER_ACCOUNT_ID")
    if not account_id:
        raise SystemExit("TRADIER_ACCOUNT_ID not set (C8: config from environment only)")
    is_live = "sandbox" not in base
    if is_live and os.environ.get("I_UNDERSTAND_THIS_IS_LIVE") != "yes":
        raise SystemExit("Refusing LIVE mode without I_UNDERSTAND_THIS_IS_LIVE=yes (C8 safety guard).")

    http = make_http_from_env()
    mode = "LIVE" if is_live else "SANDBOX"
    print(f"=== S2b bot [{label}] | {mode} | account ...{account_id[-4:]} | {base} | "
          f"entry_days={sorted(entry_days)} max_open={max_open} ticks={ticks} ===", flush=True)

    et_now = lambda: datetime.now(ZoneInfo("America/New_York"))
    deps = build_deps(
        http, account_id,
        get_spot=lambda s: fetch_spot(http, s),
        get_atr=lambda s: fetch_atr(http, s, et_now().strftime("%Y-%m-%d")),
        get_vix_regime=fetch_vix_regime,
        entry_days=entry_days, max_open=max_open,
    )
    state = runner(BotState(), deps, now_fn=et_now, sleep_fn=time.sleep,
                   poll_s=poll_seconds, ticks=ticks)
    print(f"=== done [{label}] | open={len(state.open_positions)} | halted={state.halted} "
          f"({state.halt_reason}) ===", flush=True)
    return state


def main(argv=None):
    args = parse_args(argv)
    # BOT A: the validated Monday-only S2b.
    return build_and_run(args.ticks, args.poll_seconds,
                         entry_days=frozenset({0}), max_open=1, label="MONDAY")


if __name__ == "__main__":
    main()
