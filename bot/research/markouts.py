"""Entry markouts for research (partner review v2 §14). Records signal follow-ups; never trades.

For EVERY evaluated candidate signal -- filled OR rejected -- `MarkoutTracker` schedules a
follow-up: SPY movement + spread (debit-to-close) movement at 1/5/15/30/60 minutes after the
signal, short-delta/IV change (when available), MFE/MAE (max favorable/adverse excursion, in
credit units per contract -- i.e. entry_credit - current_debit_to_close, NOT scaled by qty/100),
and an end-of-day result. Rejected signals are retained and written exactly like fills (no
survivorship bias). Filled signals additionally get time-to-take-profit / time-to-stop and the
MFE/MAE captured at the moment each threshold was crossed ("MFE before stop" / "MAE before
profit").

LOG ONLY: nothing here reads or influences an order, a size, or a trading decision. The tracker
is stateful (pending markouts resolve 1-60 min after the signal) but keeps NO wall-clock or I/O
dependency of its own -- the caller injects `now`/`spy_now` and a `spread_value_fn`/`delta_iv_fn`,
so this module is fully unit-testable with a fake clock. `to_state()`/`from_state()` round-trip
the pending list through BotState so pending markouts survive a bot restart.

Design note on time-to-TP/time-to-stop (spec §14, "for FILLED trades also"): rather than hooking
`run_management_cycle`'s close (which would couple this research module to the live exit path),
the tracker independently WATCHES the spread value crossing the same tp/stop thresholds during
`resolve_due` (thresholds are captured once, at `record_signal` time, from the position's own
credit + the bot's ManageConfig). This is the simpler, more robust choice: it needs no wiring
into the management cycle at all, and a markout-tracking bug can never touch a real close.
"""
from datetime import datetime

from bot.strategy.s2b import _occ

HORIZONS_MIN = (1, 5, 15, 30, 60)


def _elapsed_minutes(entry_ts, now_ts):
    return (now_ts - entry_ts).total_seconds() / 60.0


def _spread_key(item):
    """Identity of a pending item's underlying spread (ticker, expiry, short, long) -- the cache
    key that lets many pending items sharing one spread reuse a single batched mark."""
    return (item.get("ticker") or "SPY", item.get("expiry"),
            item.get("short_strike"), item.get("long_strike"))


class MarkoutTracker:
    def __init__(self, write_fn, pending=None):
        self._write = write_fn
        self._pending = pending if pending is not None else []

    # ── recording ────────────────────────────────────────────────────────────────────────────

    def record_signal(self, ts, signal):
        """Add a pending markout for one evaluated candidate (filled or rejected).

        `signal` keys: signal_id, ticker, short_strike, long_strike, expiry, filled (bool),
        entry_spy, entry_spread_value, credit, qty, entry_delta (optional), entry_iv (optional),
        tp_value/stop_value (optional -- filled-only spread-value thresholds used to detect
        time-to-TP / time-to-stop)."""
        self._pending.append({
            "signal_id": signal.get("signal_id"),
            "ts": ts,
            "ticker": signal.get("ticker", "SPY"),
            "short_strike": signal.get("short_strike"),
            "long_strike": signal.get("long_strike"),
            "expiry": signal.get("expiry"),
            "filled": bool(signal.get("filled", False)),
            "entry_spy": signal.get("entry_spy"),
            "entry_spread_value": signal.get("entry_spread_value"),
            "credit": signal.get("credit"),
            "qty": signal.get("qty"),
            "entry_delta": signal.get("entry_delta"),
            "entry_iv": signal.get("entry_iv"),
            "tp_value": signal.get("tp_value"),
            "stop_value": signal.get("stop_value"),
            "horizons": {h: None for h in HORIZONS_MIN},
            "mfe": None,
            "mae": None,
            "time_to_tp": None,
            "time_to_stop": None,
            "mfe_before_stop": None,
            "mae_before_tp": None,
            "horizons_written": False,
        })

    # ── resolution ───────────────────────────────────────────────────────────────────────────

    def resolve_due(self, now_ts, spy_now, spread_value_fn, delta_iv_fn=None):
        """Advance every pending markout: update MFE/MAE off the current spread value, fill any
        horizon (1/5/15/30/60 min) whose elapsed time has arrived, track time-to-TP/time-to-stop
        for filled positions, and write ONE checkpoint row once all five horizons are in. This
        intraday checkpoint is best-effort -- `finalize_eod` is the authoritative once-per-day
        write and fires for every still-pending signal regardless of whether horizons finished.

        `spread_value_fn(item)` -> current debit-to-close for the pending item's strikes (must
        work for a spread that was never opened). `delta_iv_fn(item)` (optional) -> (delta, iv)."""
        for item in self._pending:
            try:
                value_now = spread_value_fn(item)
            except Exception:
                continue

            if item["entry_spread_value"] is not None and value_now is not None:
                favorable = round(item["entry_spread_value"] - value_now, 4)
                item["mfe"] = favorable if item["mfe"] is None else max(item["mfe"], favorable)
                item["mae"] = favorable if item["mae"] is None else min(item["mae"], favorable)

            delta_now, iv_now = (None, None)
            if delta_iv_fn is not None:
                try:
                    delta_now, iv_now = delta_iv_fn(item)
                except Exception:
                    pass

            elapsed = _elapsed_minutes(item["ts"], now_ts)
            for h in HORIZONS_MIN:
                if item["horizons"][h] is not None or elapsed < h:
                    continue
                spy_move = (None if spy_now is None or item["entry_spy"] is None
                            else round(spy_now - item["entry_spy"], 4))
                spread_move = (None if value_now is None or item["entry_spread_value"] is None
                               else round(value_now - item["entry_spread_value"], 4))
                delta_change = (None if delta_now is None or item["entry_delta"] is None
                                else round(delta_now - item["entry_delta"], 4))
                iv_change = (None if iv_now is None or item["entry_iv"] is None
                             else round(iv_now - item["entry_iv"], 4))
                item["horizons"][h] = {"spy_move": spy_move, "spread_move": spread_move,
                                       "delta_change": delta_change, "iv_change": iv_change}

            if item["filled"] and value_now is not None:
                if (item["time_to_tp"] is None and item["tp_value"] is not None
                        and value_now <= item["tp_value"]):
                    item["time_to_tp"] = round(elapsed, 4)
                    item["mae_before_tp"] = item["mae"]
                if (item["time_to_stop"] is None and item["stop_value"] is not None
                        and value_now >= item["stop_value"]):
                    item["time_to_stop"] = round(elapsed, 4)
                    item["mfe_before_stop"] = item["mfe"]

            if not item["horizons_written"] and all(item["horizons"][h] is not None for h in HORIZONS_MIN):
                self._write(self._row(item, event="MARKOUT"))
                item["horizons_written"] = True

    def resolve_due_batched(self, now_ts, spy_now, batch_quote_fn, delta_iv_fn=None):
        """Batched variant of resolve_due for the critical tick() path (Priority-0 fix item 4).

        Instead of ONE broker quote call per pending item, collect the set of unique OCC option
        leg symbols across every pending item whose horizon is DUE (elapsed >= the first horizon),
        issue exactly ONE `batch_quote_fn(sorted(symbols))` call ({symbol: mid_price}), cache each
        unique spread's debit-to-close mark (short_leg_mid - long_leg_mid -- same sign convention
        as strategy.manage.spread_value_mid / wiring.mark_position), then run the EXISTING per-item
        resolve_due logic reading each item's mark from that cache. No per-item quote calls.

        Best-effort: a symbol missing from the batch -> that spread's mark is None (handled by
        resolve_due's None-guards); never raises. Returns a coverage dict
        {"quotes_requested": <#unique legs requested>, "quotes_missing": <#legs absent from batch>}
        so a caller can see how many legs the batch failed to cover.

        NOTE (differs from plain resolve_due): to avoid quoting a spread we can't yet act on, an item
        younger than the first horizon (<1 min) is skipped entirely this cycle, so its MFE/MAE (and
        any filled TP/stop crossing) is deferred until the 1-min horizon is due. resolve_due updates
        every pending item every call; the deferral is immaterial at minute-scale horizons vs
        second-scale ticks, and by the 1-min mark the item is quoted like any other."""
        # 1. unique due spreads -> their two OCC leg symbols (deduped across all pending items)
        due_legs = {}          # _spread_key -> (short_sym, long_sym)
        symbols = set()
        for item in self._pending:
            if _elapsed_minutes(item["ts"], now_ts) < HORIZONS_MIN[0]:
                continue        # not yet due for any horizon -> no quote needed this cycle
            key = _spread_key(item)
            if key in due_legs:
                continue
            try:
                short_sym = _occ(item.get("ticker") or "SPY", item["expiry"], "P", item["short_strike"])
                long_sym = _occ(item.get("ticker") or "SPY", item["expiry"], "P", item["long_strike"])
            except Exception:
                continue         # unbuildable symbol (missing expiry/strike) -> skip, mark stays None
            due_legs[key] = (short_sym, long_sym)
            symbols.add(short_sym)
            symbols.add(long_sym)

        # 2. ONE batched quote call for every unique leg symbol
        quotes = {}
        if symbols:
            try:
                quotes = batch_quote_fn(sorted(symbols)) or {}
            except Exception:
                quotes = {}
        quotes_missing = sum(1 for s in symbols if quotes.get(s) is None)

        # 3. per-unique-spread mark cache: debit-to-close = short_mid - long_mid
        mark_cache = {}
        for key, (short_sym, long_sym) in due_legs.items():
            sm = quotes.get(short_sym)
            lm = quotes.get(long_sym)
            mark_cache[key] = (round(sm - lm, 2) if sm is not None and lm is not None else None)

        # 4. reuse resolve_due (no per-item quote calls) reading marks from the cache
        self.resolve_due(now_ts, spy_now, lambda item: mark_cache.get(_spread_key(item)),
                         delta_iv_fn=delta_iv_fn)
        return {"quotes_requested": len(symbols), "quotes_missing": quotes_missing}

    def finalize_eod(self, spy_close, spread_value_fn):
        """Write the authoritative EOD row for every still-pending signal (filled or rejected --
        no survivorship bias), then clear pending (the trading day for these signals is over).
        Best-effort: a spread_value_fn failure for one item must not block the others."""
        for item in list(self._pending):
            try:
                value_now = spread_value_fn(item)
            except Exception:
                value_now = None
            row = self._row(item, event="EOD")
            row["eod_spy_move"] = (None if spy_close is None or item["entry_spy"] is None
                                    else round(spy_close - item["entry_spy"], 4))
            row["eod_spread_value"] = value_now
            row["eod_spread_move"] = (None if value_now is None or item["entry_spread_value"] is None
                                       else round(value_now - item["entry_spread_value"], 4))
            self._write(row)
        self._pending = []

    def _row(self, item, event):
        row = {
            "event": event, "signal_id": item["signal_id"], "ticker": item["ticker"],
            "short_strike": item["short_strike"], "long_strike": item["long_strike"],
            "expiry": item["expiry"], "filled": item["filled"],
            "entry_spy": item["entry_spy"], "entry_spread_value": item["entry_spread_value"],
            "credit": item["credit"], "qty": item["qty"],
            "entry_delta": item["entry_delta"], "entry_iv": item["entry_iv"],
            "mfe": item["mfe"], "mae": item["mae"],
            "time_to_tp": item["time_to_tp"], "time_to_stop": item["time_to_stop"],
            "mfe_before_stop": item["mfe_before_stop"], "mae_before_tp": item["mae_before_tp"],
        }
        for h in HORIZONS_MIN:
            hd = item["horizons"][h] or {}
            row[f"spy_move_{h}m"] = hd.get("spy_move")
            row[f"spread_move_{h}m"] = hd.get("spread_move")
            row[f"delta_change_{h}m"] = hd.get("delta_change")
            row[f"iv_change_{h}m"] = hd.get("iv_change")
        return row

    # ── persistence ──────────────────────────────────────────────────────────────────────────

    def to_state(self):
        """JSON-serializable snapshot of pending markouts (`ts` as an ISO string). Round-trips
        via `from_state`."""
        out = []
        for item in self._pending:
            d = dict(item)
            d["ts"] = item["ts"].isoformat()
            out.append(d)
        return out

    @classmethod
    def from_state(cls, state, write_fn):
        """Rebuild a tracker from `to_state()` output (parses `ts` back to a datetime; horizon
        keys back to int -- JSON round-tripping stringifies dict keys)."""
        pending = []
        for d in (state or []):
            item = dict(d)
            ts = item.get("ts")
            if isinstance(ts, str):
                item["ts"] = datetime.fromisoformat(ts)
            item["horizons"] = {int(k): v for k, v in item.get("horizons", {}).items()}
            pending.append(item)
        return cls(write_fn, pending=pending)
