"""Futures-leg validation harness — MES simulated on ES.c.0 1-min bars.

Data: futures_validation/es_1m.parquet (Databento GLBX ES.c.0 continuous,
already cached locally by the legacy project; ES price == MES price, MES
economics applied: $5/pt, $1.25/tick).

Execution model (declared up front, no post-hoc tuning):
- Signals on completed 5-min bars; fills simulated on 1-min bars.
- Market/stop fills: 1-tick base slippage; stop-loss fills 1 extra tick
  (gap-through realism). TP is a limit: fills at limit only if price trades
  THROUGH it (strictly beyond); no slippage credit.
- If SL and TP both touch within the same 1-min bar -> SL first (pessimistic).
- Commission $1.24/side -> $2.48 round trip per contract.
- Lunch skip: no new entries 11:30-13:29 ET (ORB stop-entry suspended too).
- No new entries at/after 15:30 ET; EOD flatten at the 15:45 bar open.
- Daily realized loss cap $150 -> no new entries rest of day.
- One position at a time per strategy; 1 contract MES.
- Sessions used: full RTH days only (>=370 of 390 bars); 12 chunk-truncated
  days and 3 half-days are excluded from trading entirely.

Strategies:
F1 midpoint_reclaim family (legacy semantics, regime-aligned):
   session running high/low midpoint on 5-min closes; long when prev 2 closes
   < mid and current > mid (regime != DOWN); short mirror (regime != UP).
   SL 8 / TP 12. Cooldown 1 bar after any exit. Max 4 entries/day.
F2 ORB long-only in UP regime: 15-min opening range (09:30-09:44), resting
   stop-entry at OR-high + 1 tick from 09:45; SL 12 / TP 16. Re-arm only
   after a 5-min close back below OR-high. Chop-day damage cap: 2 stop-outs
   -> done for day. Max 3 entries/day.
F3 trend-day follower: if the 10:25-10:29 5-min close (first-hour close) is
   beyond the prior FULL session's RTH range, enter with trend at 10:30;
   initial stop = trail; trailing stop TRAIL_PTS behind best 1-min close
   since entry; no TP; EOD flatten. Skips roll Mondays and days whose prior
   session was truncated. Trail reported for {8,12,16} pts (family
   robustness, no post-hoc selection).
"""
import json
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

OUTDIR = r"C:\Users\FixUser123\Documents\fable options\futures_validation"
PT = 5.0          # $ per point, MES
TICK = 0.25
COMM_RT = 2.48
DAY_LOSS_CAP = -150.0
H1_END = "2025-08-28"
ROLL_DAYS = {"2025-03-24", "2025-06-23", "2025-09-22", "2025-12-22"}

LUNCH_START, LUNCH_END = 11 * 60 + 30, 13 * 60 + 30   # minutes from 00:00 ET
LAST_ENTRY = 15 * 60 + 30
FLATTEN = 15 * 60 + 45


def t2m(s):  # "09:30" -> minutes
    return int(s[:2]) * 60 + int(s[3:])


def load_sessions():
    bars = pd.read_parquet(f"{OUTDIR}\\es_1m.parquet")
    rth = bars[(bars["et_time"] >= "09:30") & (bars["et_time"] <= "15:59")].copy()
    rth["minute"] = rth["et_time"].str[:2].astype(int) * 60 + rth["et_time"].str[3:].astype(int)
    counts = rth.groupby("et_date").size()
    full_days = sorted(counts[counts >= 370].index)
    all_days = sorted(counts.index)
    sessions = {}
    for d, g in rth.groupby("et_date"):
        g = g.sort_values("minute")
        sessions[d] = {
            "minute": g["minute"].to_numpy(),
            "open": g["open"].to_numpy(),
            "high": g["high"].to_numpy(),
            "low": g["low"].to_numpy(),
            "close": g["close"].to_numpy(),
        }
    return sessions, full_days, all_days


def load_regime():
    df = pd.read_csv(f"{OUTDIR}\\regime_daily.csv", index_col=0)
    return df["regime"].to_dict()


class DayCtx:
    """Per-day execution engine: pending orders, bracket management, equity path."""

    def __init__(self, day, sess, slip_mult, contracts=1):
        self.day = day
        self.s = sess
        self.base_slip = TICK * 1 * slip_mult       # market/stop-entry fills
        self.stop_slip = TICK * 2 * slip_mult       # stop-loss / trail fills
        self.contracts = contracts
        self.pos = None          # dict(dir, entry, sl, tp, trail_pts, best, setup, entry_min)
        self.realized = 0.0
        self.trades = []
        self.equity_path = []    # (minute, realized + open MTM) in $
        self.stopouts = defaultdict(int)
        self.entries = defaultdict(int)
        self.cooldown_until = -1  # minute; block signal entries before this

    def can_enter(self, minute):
        if self.pos is not None:
            return False
        if self.realized <= DAY_LOSS_CAP:
            return False
        if LUNCH_START <= minute < LUNCH_END:
            return False
        if minute >= LAST_ENTRY:
            return False
        if minute < self.cooldown_until:
            return False
        return True

    def open_pos(self, direction, fill, minute, setup, sl_pts=None, tp_pts=None, trail_pts=None):
        sl = fill - sl_pts if (sl_pts and direction == 1) else (fill + sl_pts if sl_pts else None)
        tp = fill + tp_pts if (tp_pts and direction == 1) else (fill - tp_pts if tp_pts else None)
        self.pos = {"dir": direction, "entry": fill, "sl": sl, "tp": tp,
                    "trail_pts": trail_pts, "best": fill, "setup": setup,
                    "entry_min": minute}
        self.entries[setup] += 1

    def close_pos(self, price, minute, reason):
        p = self.pos
        pnl = (price - p["entry"]) * p["dir"] * PT * self.contracts - COMM_RT * self.contracts
        self.realized += pnl
        self.trades.append({
            "date": self.day, "setup": p["setup"], "dir": "long" if p["dir"] == 1 else "short",
            "entry_min": p["entry_min"], "exit_min": minute,
            "entry": p["entry"], "exit": price, "reason": reason, "pnl": round(pnl, 2),
        })
        if reason == "stop":
            self.stopouts[p["setup"]] += 1
        self.pos = None
        self.cooldown_until = minute + 5

    def manage_bar(self, i):
        """Bracket/trail/EOD management on 1-min bar i. Returns True if closed."""
        m = self.s["minute"][i]
        o, h, l = self.s["open"][i], self.s["high"][i], self.s["low"][i]
        c = self.s["close"][i]
        p = self.pos
        if p is None:
            return False
        if m >= FLATTEN:
            self.close_pos(o - p["dir"] * self.base_slip, m, "eod_flatten")
            return True
        d = p["dir"]
        # trailing stop update uses prior bars' closes; check exit first vs current trail
        if p["trail_pts"] is not None:
            trail = p["best"] - d * p["trail_pts"]
            if (d == 1 and l <= trail) or (d == -1 and h >= trail):
                fill = min(o, trail) if d == 1 else max(o, trail)
                self.close_pos(fill - d * self.stop_slip, m, "stop")
                return True
            # update best AFTER exit check (trail can't move intra-bar in our model)
            p["best"] = max(p["best"], c) if d == 1 else min(p["best"], c)
            return False
        # fixed bracket: SL first (pessimistic), then TP
        if p["sl"] is not None and ((d == 1 and l <= p["sl"]) or (d == -1 and h >= p["sl"])):
            fill = min(o, p["sl"]) if d == 1 else max(o, p["sl"])
            self.close_pos(fill - d * self.stop_slip, m, "stop")
            return True
        if p["tp"] is not None and ((d == 1 and h > p["tp"]) or (d == -1 and l < p["tp"])):
            fill = max(o, p["tp"]) if d == 1 else min(o, p["tp"])  # gap-through opens fill at open
            self.close_pos(fill, m, "take_profit")
            return True
        return False

    def record_equity(self, i):
        m = self.s["minute"][i]
        mtm = 0.0
        if self.pos is not None:
            p = self.pos
            mtm = (self.s["close"][i] - p["entry"]) * p["dir"] * PT * self.contracts
        self.equity_path.append((int(m), self.realized + mtm))


def agg5(sess):
    """5-min aggregates: returns list of (end_minute_exclusive, o,h,l,c) completed bars."""
    out = []
    mins = sess["minute"]
    start = 9 * 60 + 30
    for b0 in range(start, 16 * 60, 5):
        mask = (mins >= b0) & (mins < b0 + 5)
        if not mask.any():
            continue
        out.append({
            "end": b0 + 5,
            "open": sess["open"][mask][0], "high": sess["high"][mask].max(),
            "low": sess["low"][mask].min(), "close": sess["close"][mask][-1],
        })
    return out


# ---------------------------------------------------------------- strategies

def run_f1(day, sess, regime, slip_mult):
    ctx = DayCtx(day, sess, slip_mult)
    bars5 = agg5(sess)
    closes = []
    sess_hi, sess_lo = -np.inf, np.inf
    b5 = {b["end"]: b for b in bars5}
    pending = None  # (dir, setup) to fill at next 1-min open
    for i in range(len(sess["minute"])):
        m = sess["minute"][i]
        if pending is not None and ctx.can_enter(m):
            d, setup = pending
            fill = sess["open"][i] + d * ctx.base_slip
            ctx.open_pos(d, fill, m, setup, sl_pts=8.0, tp_pts=12.0)
        pending = None
        ctx.manage_bar(i)
        # update running session range with this 1-min bar
        sess_hi = max(sess_hi, sess["high"][i])
        sess_lo = min(sess_lo, sess["low"][i])
        # 5-min bar completing at m+1?
        if (m + 1) in b5 or (m + 1) % 5 == 0:
            end = m + 1
            if end in b5:
                closes.append(b5[end]["close"])
                if len(closes) >= 3 and ctx.pos is None:
                    mid = (sess_hi + sess_lo) / 2.0
                    cur, prev = closes[-1], closes[-3:-1]
                    if all(c < mid for c in prev) and cur > mid and regime != "DOWN" \
                            and ctx.entries["midpoint_reclaim"] < 4:
                        pending = (1, "midpoint_reclaim")
                    elif all(c > mid for c in prev) and cur < mid and regime != "UP" \
                            and ctx.entries["midpoint_rejection"] < 4:
                        pending = (-1, "midpoint_rejection")
        ctx.record_equity(i)
    return ctx


def run_f2(day, sess, regime, slip_mult):
    ctx = DayCtx(day, sess, slip_mult)
    if regime != "UP":
        for i in range(len(sess["minute"])):
            ctx.record_equity(i)
        return ctx
    mins = sess["minute"]
    or_mask = (mins >= t2m("09:30")) & (mins <= t2m("09:44"))
    or_high = sess["high"][or_mask].max()
    trigger = or_high + TICK
    armed = True
    for i in range(len(mins)):
        m = mins[i]
        ctx.manage_bar(i)
        # re-arm check on completed 5-min closes: need a close back below or_high
        if not armed and ctx.pos is None and (m % 5 == 0) and m > t2m("09:45"):
            # close of the just-completed 5-min bar
            prev_mask = (mins >= m - 5) & (mins < m)
            if prev_mask.any() and sess["close"][prev_mask][-1] < or_high:
                armed = True
        if armed and m >= t2m("09:45") and ctx.can_enter(m) \
                and ctx.stopouts["orb_break"] < 2 and ctx.entries["orb_break"] < 3:
            if sess["high"][i] >= trigger:
                fill = max(sess["open"][i], trigger) + ctx.base_slip
                ctx.open_pos(1, fill, m, "orb_break", sl_pts=12.0, tp_pts=16.0)
                armed = False
                ctx.manage_bar(i)  # same-bar bracket check after entry
        ctx.record_equity(i)
    return ctx


def run_f3(day, sess, prior_range, slip_mult, trail_pts):
    ctx = DayCtx(day, sess, slip_mult)
    if prior_range is None or day in ROLL_DAYS:
        for i in range(len(sess["minute"])):
            ctx.record_equity(i)
        return ctx
    ph, pl = prior_range
    mins = sess["minute"]
    pending = None
    for i in range(len(mins)):
        m = mins[i]
        if pending is not None and ctx.can_enter(m):
            d = pending
            fill = sess["open"][i] + d * ctx.base_slip
            ctx.open_pos(d, fill, m, "trend_day", trail_pts=trail_pts)
            pending = None
        ctx.manage_bar(i)
        if m + 1 == t2m("10:30") and ctx.entries["trend_day"] == 0:
            fh_mask = (mins >= t2m("10:25")) & (mins <= t2m("10:29"))
            if fh_mask.any():
                c = sess["close"][fh_mask][-1]
                if c > ph:
                    pending = 1
                elif c < pl:
                    pending = -1
        ctx.record_equity(i)
    return ctx


# ---------------------------------------------------------------- runner

def summarize(trades, label):
    if not trades:
        return {"label": label, "n": 0}
    df = pd.DataFrame(trades)
    gp = df.loc[df.pnl > 0, "pnl"].sum()
    gl = -df.loc[df.pnl < 0, "pnl"].sum()
    eq = df.pnl.cumsum()
    dd = (eq.cummax() - eq).max()
    return {
        "label": label, "n": len(df), "wr": round(100 * (df.pnl > 0).mean(), 1),
        "pf": round(gp / gl, 3) if gl > 0 else float("inf"),
        "pnl": round(df.pnl.sum(), 2), "avg_win": round(df.loc[df.pnl > 0, "pnl"].mean(), 2) if gp > 0 else 0,
        "avg_loss": round(df.loc[df.pnl < 0, "pnl"].mean(), 2) if gl > 0 else 0,
        "max_dd": round(dd, 2),
    }


def main():
    sessions, full_days, all_days = load_sessions()
    regime = load_regime()
    half_or_trunc = set(all_days) - set(full_days)
    print(f"sessions: {len(all_days)} total, {len(full_days)} full (trading), "
          f"{len(half_or_trunc)} excluded")

    # prior FULL session range lookup
    prior_range = {}
    prev_full = None
    for d in all_days:
        if prev_full is not None:
            s = sessions[prev_full]
            prior_range[d] = (s["high"].max(), s["low"].min())
        else:
            prior_range[d] = None
        if d in full_days:
            prev_full = d
        else:
            prev_full = None   # truncated/half day -> next day has no valid prior range

    runs = {}
    for slip_mult, suffix in ((1, ""), (2, "_slip2x")):
        for name, fn in [
            ("f1_midpoint", lambda d, s: run_f1(d, s, regime.get(d, "CHOP"), slip_mult)),
            ("f2_orb_long_up", lambda d, s: run_f2(d, s, regime.get(d, "CHOP"), slip_mult)),
            ("f3_trend_t8", lambda d, s: run_f3(d, s, prior_range[d], slip_mult, 8.0)),
            ("f3_trend_t12", lambda d, s: run_f3(d, s, prior_range[d], slip_mult, 12.0)),
            ("f3_trend_t16", lambda d, s: run_f3(d, s, prior_range[d], slip_mult, 16.0)),
        ]:
            all_trades, eq_paths = [], {}
            for d in full_days:
                ctx = fn(d, sessions[d])
                all_trades.extend(ctx.trades)
                eq_paths[d] = ctx.equity_path
            runs[name + suffix] = (all_trades, eq_paths)
            df = pd.DataFrame(all_trades)
            df.to_csv(f"{OUTDIR}\\trades_{name}{suffix}.csv", index=False)

    # summaries: full / H1 / H2 per run; per-setup attribution; monthly pnl
    report = {}
    for key, (trades, eq_paths) in runs.items():
        df = pd.DataFrame(trades)
        rep = {"full": summarize(trades, "full")}
        if len(df):
            rep["h1"] = summarize(df[df.date <= H1_END].to_dict("records"), "h1")
            rep["h2"] = summarize(df[df.date > H1_END].to_dict("records"), "h2")
            rep["by_setup"] = {s: summarize(g.to_dict("records"), s)
                               for s, g in df.groupby("setup")}
            rep["by_dir"] = {s: summarize(g.to_dict("records"), s)
                             for s, g in df.groupby("dir")}
            df["month"] = df.date.str[:7]
            rep["monthly_pnl"] = {m: round(g.pnl.sum(), 2) for m, g in df.groupby("month")}
            rep["exit_reasons"] = df.reason.value_counts().to_dict()
        report[key] = rep
        f = rep["full"]
        h1 = rep.get("h1", {})
        h2 = rep.get("h2", {})
        print(f"{key:28s} n={f.get('n',0):4d} PF={f.get('pf',0):>6} pnl={f.get('pnl',0):>9} "
              f"| H1 PF={h1.get('pf','-'):>6} pnl={h1.get('pnl','-'):>8} "
              f"| H2 PF={h2.get('pf','-'):>6} pnl={h2.get('pnl','-'):>8} "
              f"| maxDD={f.get('max_dd','-')}")

    with open(f"{OUTDIR}\\summary_futures.json", "w") as fh:
        json.dump(report, fh, indent=1)

    # equity paths (baseline only) for the eval overlay
    eq_out = {}
    for key, (trades, eq_paths) in runs.items():
        if key.endswith("_slip2x"):
            continue
        eq_out[key] = {d: path for d, path in eq_paths.items()}
    np.save(f"{OUTDIR}\\equity_paths.npy", eq_out, allow_pickle=True)
    print("\nwrote summary_futures.json, trades_*.csv, equity_paths.npy")


if __name__ == "__main__":
    main()
