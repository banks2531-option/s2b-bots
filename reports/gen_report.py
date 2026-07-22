#!/usr/bin/env python3
"""S2b performance + Codex-review data generator (Bot B sandbox + Bot C live).

Runs ON the droplet via cron at the 5 daily review slots (10:00, 12:00, 14:00, 15:45, 16:15 ET,
Mon-Fri). One data-collection pass produces:

  * performance_report.html  -- human/advisor report (self-overwriting)
  * reports/latest/*         -- standardized files a Codex reviewer reads (JSON + CSV), per the
                                advisor's spec, plus manifest.json with per-file sha256 so Codex
                                can ignore unchanged files.

READ-ONLY. This never touches broker orders, bot state, config, or the running services. Every
section is wrapped so a data/API hiccup degrades that section rather than crashing the run.
"""
import os, sys, json, csv, html, hashlib, glob, math, urllib.request, urllib.parse, collections, traceback
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

BASE = "/root/s2b-bot"
OUT_HTML = BASE + "/performance_report.html"
OUT_JSON = BASE + "/performance_report_status.json"
REPORTS = BASE + "/reports/latest"
ET = ZoneInfo("America/New_York")
NOW_UTC = datetime.now(timezone.utc)
NOW_ET = NOW_UTC.astimezone(ET)
TODAY = NOW_ET.strftime("%Y-%m-%d")
FORCE = "--force" in sys.argv

BASE12 = ["event", "date", "ticker", "short", "long", "expiry", "qty", "credit",
          "action", "exit_value", "pnl", "status"]

# Standardized decision-telemetry columns. The orchestrator populates the ACTIVE quantity-cap fields
# below; the legacy risk_budget_proposed_qty / permitted_qty / headroom columns it superseded are
# left BLANK in the trade log, so exporting them (as this report used to) yielded 0/N usable rows.
# Candidate identity/timestamp/strikes are NOT yet emitted on rejected decision rows (bot-side gap;
# see the follow-up recommendation) -- obs_candidate_key is carried so it flows through once logged.
DECISION_COLS = [
    "date", "decision", "limiting_gate",
    "requested_qty", "quality_adjusted_qty", "final_qty",
    "risk_current_exposure", "risk_limit", "risk_remaining_capacity", "risk_incremental_per_contract",
    "expected_executable_credit", "credit_ratio", "credit_threshold",
    "agg_structural", "agg_remaining_stop",
    "obs_candidate_key",
]

# The 5 advisor review slots, in ET minutes-since-midnight. The last one is the EOD full session.
SLOTS = {580: "09:40-postopen", 600: "10:00-open", 720: "12:00-midday", 840: "14:00-afternoon",
         945: "15:45-preclose", 975: "16:15-EOD"}

BOTS = [
    {"key": "c", "label": "Bot C", "kind": "LIVE (real money)", "env": "s2b-live.env",
     "state": "state_live.json", "trades": "trades_live.csv", "markouts": "markouts_live.csv", "wing": 5},
    {"key": "b", "label": "Bot B", "kind": "SANDBOX (paper)", "env": "s2b.env",
     "state": "state_alldays.json", "trades": "trades_alldays.csv", "markouts": "markouts_alldays.csv", "wing": 10},
]


def market_open(dt):
    return dt.weekday() < 5 and 570 <= dt.hour * 60 + dt.minute <= 960


def should_generate(dt):
    """Write on weekdays 09:30..16:20 ET (the wide edge lets a slightly-late cron capture 16:15)."""
    return dt.weekday() < 5 and 570 <= dt.hour * 60 + dt.minute <= 980


def review_slot(dt):
    """Nearest scheduled slot within +/-8 min, else 'adhoc'."""
    mins = dt.hour * 60 + dt.minute
    for m, name in SLOTS.items():
        if abs(mins - m) <= 8:
            return name
    return "adhoc"


def due_now(dt, tol=2):
    """True when dt (ET) is inside the generate window AND within tol minutes of a defined slot.

    Cron polls this every 5 minutes over a wide UTC window; the script self-selects the exact ET
    slots here. That makes the schedule DST-safe via Python's zoneinfo -- necessary because this
    droplet's Debian cron (3.0pl1) does NOT honor CRON_TZ (empirically verified 2026-07-21: an ET
    test job under CRON_TZ never fired). Every slot minute is a multiple of 5, so a */5 poll lands
    on each slot exactly once; tol=2 admits that tick and excludes the adjacent (5-min-away) ticks."""
    if not should_generate(dt):
        return False
    mins = dt.hour * 60 + dt.minute
    return any(abs(mins - m) <= tol for m in SLOTS)


def load_env(path):
    e = {}
    try:
        for ln in open(path):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                e[k] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return e


def api(env):
    b, t, a = env.get("TRADIER_BASE_URL"), env.get("TRADIER_TOKEN"), env.get("TRADIER_ACCOUNT_ID")

    def get(path, **kw):
        u = b + path + ("?" + urllib.parse.urlencode(kw) if kw else "")
        r = urllib.request.Request(u, headers={"Authorization": "Bearer " + t, "Accept": "application/json"})
        return json.load(urllib.request.urlopen(r, timeout=25))
    return get, a, b


def occ(exp, k):
    y, m, d = exp.split("-")
    return "SPY%s%s%sP%08d" % (y[2:], m, d, int(float(k) * 1000))


def f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def sha16(data):
    """SHA-256, first 16 hex chars, of raw bytes. The single hashing path for EVERY manifest file."""
    return hashlib.sha256(data).hexdigest()[:16]


def manifest_of_dir(reports_dir, names):
    """Per-file sha16 of each file's ACTUAL on-disk bytes -- identical treatment for every payload,
    including deployed_commit.txt (previously special-cased to the commit-string prefix, so its
    manifest value was not a byte hash like the others)."""
    out = {}
    for n in names:
        with open(os.path.join(reports_dir, n), "rb") as fh:
            out[n] = sha16(fh.read())
    return out


def realized_fields(state, report_date, closes_today, lifetime=None):
    """Honest realized-P&L fields so a prior risk-day's P&L is never presented as today's.

    A bot's realized_today counter resets on the bot's OWN risk-day rollover, which can lag the
    report's calendar date (e.g. Bot C idle since its last trade shows -14.6 from risk_day
    2026-07-17 on a 2026-07-21 report). Presenting that counter as "today" is the bug. Fields:
      report_date          -- the report's ET calendar date (authoritative "today")
      risk_day             -- the bot's internal risk-day (may lag report_date)
      realized_risk_day    -- the bot's realized_today counter, valid for risk_day, NOT today
      realized_report_date -- P&L actually booked on report_date, summed from today's CLOSE rows
      lifetime_realized    -- total realized across all closed trades
      realized_is_stale    -- True when risk_day != report_date (the counter predates today)
    """
    risk_day = state.get("risk_day")
    realized_report_date = round(sum(f(t.get("pnl"), 0.0) for t in (closes_today or [])), 2)
    return {"report_date": report_date, "risk_day": risk_day,
            "realized_risk_day": f(state.get("realized_today")),
            "realized_report_date": realized_report_date,
            "lifetime_realized": (round(lifetime, 2) if lifetime is not None else None),
            "realized_is_stale": bool(risk_day and risk_day != report_date)}


def pnl_accounting_fields(config):
    """Replace the ambiguous actual_fill_accounting boolean with explicit provenance a reviewer can
    trust. ON  -> P&L is booked from actual broker fills (Bot B sandbox; unaudited vs a statement).
             OFF -> P&L is SYNTHETIC (credit-mark); live-broker fill accounting is disabled (Bot C:
                    negative-credit sign + leg-count qty bug, pending the 5-execution ledger)."""
    on = bool((config or {}).get("actual_fill_accounting"))
    return {"pnl_source": "actual_fill" if on else "synthetic_mark",
            "pnl_validation_status": "sandbox_unaudited" if on else "live_fill_accounting_disabled"}


def label_broker_legs(legs, own_positions):
    """Tag each shared-account broker leg owned/foreign. Bot B runs --shared-account, so the broker
    positions endpoint returns co-occupants' legs too; those are EXPECTED, not reconciliation drift.
    A leg is 'owned' when its OCC put symbol AND side match a leg the bot's own book implies (short
    strike -> qty<0, long strike -> qty>0 at the position's expiry). Returns (labeled_legs, summary).
    Heuristic: if a co-occupant holds the same strike/side the counts merge; the split still tells a
    reviewer the extra legs are shared-account, not missing from the book."""
    own = {}
    for p in (own_positions or []):
        exp = p.get("expiry")
        s, l = p.get("short_strike"), p.get("long_strike")
        if exp and s is not None:
            own[occ(exp, s)] = -1
        if exp and l is not None:
            own[occ(exp, l)] = 1
    labeled, owned, foreign = [], 0, 0
    for L in (legs or []):
        q = L.get("quantity")
        side = -1 if (q is not None and q < 0) else 1
        is_own = own.get(L.get("symbol")) == side
        owned += int(is_own)
        foreign += int(not is_own)
        d = dict(L)
        d["ownership"] = "owned" if is_own else "foreign"
        labeled.append(d)
    summary = {"own_book_positions": len(own_positions or []), "owned_legs": owned,
               "foreign_legs": foreign,
               "note": "foreign legs belong to co-occupants of the shared sandbox account "
                       "(Bot B runs --shared-account); they are expected and are NOT drift."}
    return labeled, summary


def vwap_fields(bars):
    """Robust intraday VWAP. Tradier's per-bar `vwap` field is unreliable -- values fall outside the
    bar's own [low,high] (it appears to return a cumulative SESSION vwap, which drifts below price on
    an up day), so using bars[-1]['vwap'] directly for vwap_now/price_vs_vwap_pts is wrong. Instead
    self-compute the session VWAP from typical-price*volume, and report how many raw per-bar vwaps
    were out of range so a reviewer can gauge feed quality. Returns {} when volume is unavailable."""
    out_of_range = num = den = 0
    for b in (bars or []):
        h, l, c, v, w = b.get("high"), b.get("low"), b.get("close"), b.get("volume"), b.get("vwap")
        if None not in (h, l, w) and not (l <= w <= h):
            out_of_range += 1
        if None not in (h, l, c) and v:
            num += ((h + l + c) / 3.0) * v
            den += v
    res = {"vwap_bars_out_of_range": out_of_range}
    if den:
        session_vwap = round(num / den, 2)
        res["session_vwap"] = res["vwap_now"] = session_vwap
        lc = bars[-1].get("close")
        if lc is not None:
            res["price_vs_vwap_pts"] = round(lc - session_vwap, 2)
    return res


def read_trades(bot):
    rows, seen = [], set()
    files = [BASE + "/" + bot["trades"]] + sorted(glob.glob(BASE + "/" + bot["trades"] + ".superseded-*"))
    for path in files:
        try:
            for r in csv.reader(open(path, newline="")):
                if len(r) < 12 or r[0] not in ("OPEN", "CLOSE"):
                    continue
                dd = dict(zip(BASE12, r[:12]))
                key = (dd["event"], dd["date"], dd["short"], dd["long"], dd["expiry"], dd["qty"], dd["pnl"], dd["action"])
                if key in seen:
                    continue
                seen.add(key)
                rows.append(dd)
        except Exception:
            pass
    return rows


def read_today_decisions(bot):
    try:
        rows = list(csv.DictReader(open(BASE + "/" + bot["trades"])))
    except Exception:
        return []
    return [r for r in rows if r.get("date") == TODAY and r.get("event") == "DECISION"]



def markout_index(bot):
    """Map (short,long,expiry) -> the markout row, for MFE/MAE on open positions. Zero API cost."""
    idx = {}
    try:
        for r in csv.DictReader(open(BASE + "/" + bot["markouts"])):
            key = (f(r.get("short_strike")), f(r.get("long_strike")), r.get("expiry"))
            idx[key] = r        # last one wins (most recent tracking of that spread)
    except Exception:
        pass
    return idx


# ---------------- structured collection (one pass) ----------------
def collect_market():
    m = {"generated_et": NOW_ET.isoformat(), "generated_utc": NOW_UTC.isoformat(),
         "review_slot": review_slot(NOW_ET), "market": "OPEN" if market_open(NOW_ET) else "CLOSED",
         "symbol": "SPY"}
    try:
        get, _, _ = api(load_env(BASE + "/s2b-live.env"))
    except Exception:
        return m
    try:
        start = (NOW_ET - timedelta(days=30)).strftime("%Y-%m-%d")
        h = get("/markets/history", symbol="SPY", interval="daily", start=start, end=TODAY).get("history", {}).get("day", [])
        if isinstance(h, dict):
            h = [h]
        trs = [max(h[i]["high"] - h[i]["low"], abs(h[i]["high"] - h[i-1]["close"]), abs(h[i]["low"] - h[i-1]["close"]))
               for i in range(1, len(h))]
        m["atr14"] = round(sum(trs[-14:]) / max(1, min(14, len(trs))), 2)
        if h:
            t = h[-1]
            m["daily"] = {k: t.get(k) for k in ("open", "high", "low", "close", "volume")}
            m["spot"] = t.get("close")
    except Exception as e:
        m["daily_error"] = str(e)
    try:
        q = get("/markets/quotes", symbols="VIX").get("quotes", {}).get("quote", {})
        m["vix"] = q.get("last")
    except Exception:
        pass
    try:
        ts = get("/markets/timesales", symbol="SPY", interval="5min",
                 start=TODAY + " 09:30", end=TODAY + " 16:00").get("series", {})
        data = ts.get("data") if isinstance(ts, dict) else None
        if isinstance(data, dict):
            data = [data]
        m["bars_5min"] = [{"time": str(b.get("time"))[11:16], "open": b.get("open"), "high": b.get("high"),
                           "low": b.get("low"), "close": b.get("close"), "vwap": b.get("vwap"),
                           "volume": b.get("volume")} for b in (data or [])]
    except Exception as e:
        m["bars_error"] = str(e)
    # ── quant context (advisor Q5): realized vol, expected move, VWAP deviation, opening range ──
    try:
        closes = [d["close"] for d in h[-21:]]
        rets = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        mu = sum(rets) / len(rets)
        var = sum((r - mu) ** 2 for r in rets) / max(1, len(rets) - 1)
        m["realized_vol_annual_pct"] = round(math.sqrt(var * 252) * 100, 2)
    except Exception:
        pass
    try:
        vix = float(m.get("vix"))
        m["expected_move_1d_pct"] = round(vix / (252 ** 0.5), 2)          # VIX is annualized %
        if m.get("spot"):
            m["expected_move_1d_pts"] = round(m["spot"] * (vix / 100) / (252 ** 0.5), 2)
    except Exception:
        pass
    try:
        bars = m.get("bars_5min") or []
        if bars:
            last = bars[-1]
            m.update(vwap_fields(bars))     # self-computed session VWAP; Tradier's per-bar vwap is unreliable
            orb = bars[:6]                                                # first 30 min = 6 x 5-min
            if orb:
                hi = max(b["high"] for b in orb if b.get("high") is not None)
                lo = min(b["low"] for b in orb if b.get("low") is not None)
                c = last.get("close")
                m["opening_range"] = {"high": hi, "low": lo,
                                      "broken_up": bool(c is not None and c > hi),
                                      "broken_down": bool(c is not None and c < lo)}
    except Exception:
        pass
    return m


def collect_broker(bot):
    snap = {"bot": bot["label"], "account_type": "sandbox" if bot["key"] == "b" else "live",
            "generated_et": NOW_ET.isoformat()}
    try:
        get, acct, base = api(load_env(BASE + "/" + bot["env"]))
    except Exception as e:
        snap["error"] = "no api: %s" % e
        return snap
    snap["account"] = (acct[:4] + "****") if acct else None
    try:
        bal = (get("/accounts/%s/balances" % acct) or {}).get("balances") or {}
        snap["equity"] = bal.get("total_equity")
        snap["option_buying_power"] = (bal.get("margin") or bal.get("cash") or {}).get("option_buying_power")
    except Exception as e:
        snap["balance_error"] = str(e)
    try:
        p = get("/accounts/%s/positions" % acct).get("positions")
        legs = [] if p in (None, "null") else (p.get("position") if isinstance(p, dict) else p)
        legs = [legs] if isinstance(legs, dict) else (legs or [])
        snap["legs"] = [{"symbol": L.get("symbol"), "quantity": L.get("quantity"), "cost_basis": L.get("cost_basis")} for L in legs]
    except Exception as e:
        snap["positions_error"] = str(e)
    try:
        o = get("/accounts/%s/orders" % acct).get("orders")
        lst = [] if o in (None, "null") else (o.get("order") if isinstance(o, dict) else o)
        lst = [lst] if isinstance(lst, dict) else (lst or [])
        pend = [{"id": x.get("id"), "status": x.get("status"), "class": x.get("class")}
                for x in lst if x.get("status") not in ("filled", "canceled", "rejected", "expired")]
        snap["pending_orders"] = pend
    except Exception as e:
        snap["orders_error"] = str(e)
    return snap


def collect_bot(bot, mkt, broker):
    try:
        st = json.load(open(BASE + "/" + bot["state"]))
    except Exception:
        st = {}
    spot, atr = mkt.get("spot"), (mkt.get("atr14") or 0)
    data = {"bot": bot["label"], "kind": bot["kind"], "generated_et": NOW_ET.isoformat(),
            "review_slot": mkt.get("review_slot"), "deployed_commit": deployed_commit(),
            "equity": f(broker.get("equity")), "halted": bool(st.get("halted")),
            "halt_reason": st.get("halt_reason") or None,
            "entries_today": st.get("entries_today"),
            "funnel": {k: st.get(k) for k in ("entry_cycles_started", "raw_candidate_evaluations",
                                              "unique_candidate_opportunities") if k in st}}
    # positions with live marks
    positions = st.get("open_positions") or []
    marks = {}
    try:
        get, acct, _ = api(load_env(BASE + "/" + bot["env"]))
    except Exception:
        get = None
    unreal = 0.0
    plist = []
    mo_idx = markout_index(bot)
    for p in positions:
        s, l, q, cr, exp = p.get("short_strike"), p.get("long_strike"), p.get("qty"), p.get("credit"), p.get("expiry")
        mark = None
        if get:
            try:
                qq = get("/markets/quotes", symbols=occ(exp, s) + "," + occ(exp, l)).get("quotes", {}).get("quote", [])
                qq = [qq] if isinstance(qq, dict) else qq
                mm = {x["symbol"]: ((x.get("bid") or 0) + (x.get("ask") or 0)) / 2 for x in qq}
                mark = round(mm.get(occ(exp, s), 0) - mm.get(occ(exp, l), 0), 2)
            except Exception:
                mark = None
        pnl = round((cr - mark) * 100 * q, 2) if mark is not None else None
        if pnl is not None:
            unreal += pnl
        dist = round(spot - s, 2) if spot else None
        distatr = round(dist / atr, 2) if (dist is not None and atr) else None
        mo = mo_idx.get((f(s), f(l), exp)) or {}
        plist.append({"short": s, "long": l, "qty": q, "credit": cr, "mark": mark, "unrealized": pnl,
                      "dist_to_short_pts": dist, "dist_to_short_atr": distatr, "stop_at": round(cr * 3, 2),
                      "expiry": exp, "at_the_money": bool(distatr is not None and distatr <= 0.10),
                      "mfe": f(mo.get("mfe")), "mae": f(mo.get("mae")),
                      "entry_delta": f(mo.get("entry_delta")), "entry_iv": f(mo.get("entry_iv"))})
    data["positions"] = plist
    data["unrealized"] = round(unreal, 2)
    # today's captured trades
    trades = read_trades(bot)
    tt = [t for t in trades if t["date"] == TODAY]
    data["opens_today"] = [{k: t[k] for k in ("short", "long", "qty", "credit", "expiry", "status")} for t in tt if t["event"] == "OPEN"]
    data["closes_today"] = [{k: t[k] for k in ("short", "long", "qty", "credit", "action", "exit_value", "pnl", "status")} for t in tt if t["event"] == "CLOSE"]
    # decision funnel today
    dec = read_today_decisions(bot)
    gates = collections.Counter(r.get("decision") for r in dec)
    data["decisions_today"] = len(dec)
    data["gate_breakdown"] = dict(gates.most_common())
    # missed-opportunity (rejected markouts, 60m forward)
    try:
        mk = [r for r in csv.DictReader(open(BASE + "/" + bot["markouts"])) if str(r.get("signal_id", "")).startswith(TODAY)]
        rej = [m for m in mk if str(m.get("filled")) == "False"]
        moves = [f(m.get("spread_move_60m")) for m in rej if f(m.get("spread_move_60m")) is not None]
        data["missed_opportunities"] = {"rejected_candidates": len(rej), "with_60m_markout": len(moves),
                                        "favorable_to_seller": sum(1 for x in moves if x < 0),
                                        "mean_spread_move_60m": round(sum(moves) / len(moves), 4) if moves else None}
    except Exception:
        data["missed_opportunities"] = {}
    # config snapshot (risk parameters the advisor cares about)
    data["config"] = read_config(bot)
    # history summary
    data["history"] = history_summary(bot)
    # honest realized-P&L fields (report_date vs the bot's lagging risk_day counter) + P&L provenance
    data.update(realized_fields(st, TODAY, data["closes_today"],
                                lifetime=(data["history"] or {}).get("total_realized")))
    data["realized_today"] = data["realized_report_date"]   # exec column now shows P&L booked TODAY
    data.update(pnl_accounting_fields(data["config"]))
    return data


def read_config(bot):
    """Pull the deployed risk parameters straight from the entrypoint's feature set."""
    try:
        import importlib
        mod = "bot.app.run_s2b_live" if bot["key"] == "c" else "bot.app.run_s2b_alldays"
        m = importlib.import_module(mod)
        F = getattr(m, "LIVE_FEATURES" if bot["key"] == "c" else "ALLDAYS_FEATURES")
        keys = ["max_gap_stress_loss_pct", "max_total_structural_risk_pct", "max_total_stop_risk_pct",
                "max_entry_stop_risk_pct", "daily_pnl_halt_pct", "min_equity_to_open", "max_entry_qty",
                "risk_classification", "actual_fill_accounting", "gap_stress_enforcement_model",
                "enable_alternate_expirations", "enable_low_credit_08_to_10", "aggregate_risk_budget"]
        return {k: getattr(F, k, None) for k in keys}
    except Exception as e:
        return {"error": str(e)}


def history_summary(bot):
    closes = [t for t in read_trades(bot) if t["event"] == "CLOSE" and f(t["pnl"]) is not None]
    if not closes:
        return {}
    by_day = collections.OrderedDict()
    for c in sorted(closes, key=lambda x: x["date"]):
        by_day.setdefault(c["date"], 0.0)
        by_day[c["date"]] += f(c["pnl"])
    pnls = [f(c["pnl"]) for c in closes]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gp, gl = sum(wins), -sum(losses)
    return {"total_realized": round(sum(pnls), 2), "closed_trades": len(closes),
            "wins": len(wins), "losses": len(losses),
            "win_rate_pct": round(100 * len(wins) / len(pnls), 1) if pnls else None,
            "profit_factor": (round(gp / gl, 2) if gl else None),
            "best": max(pnls), "worst": min(pnls),
            "by_day": {d: round(v, 2) for d, v in by_day.items()}}


def deployed_commit():
    try:
        return open(BASE + "/DEPLOYED_COMMIT").read().strip()
    except Exception:
        return "unknown"


# ---------------- Codex standardized files ----------------
def write_codex_files(mkt, bots_data, brokers):
    os.makedirs(REPORTS, exist_ok=True)
    names = []

    def dump_json(name, obj):
        with open(os.path.join(REPORTS, name), "w") as fh:
            fh.write(json.dumps(obj, indent=1, default=str))
        names.append(name)

    def dump_csv(name, header, rows):
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        with open(os.path.join(REPORTS, name), "w", newline="") as fh:
            fh.write(buf.getvalue())
        names.append(name)

    dump_json("market_context.json", mkt)
    for b in BOTS:
        k = b["key"]
        dump_json("bot_%s_performance.json" % k, bots_data[k])
        dump_json("broker_snapshot_%s.json" % k, brokers[k])
        # trades: full OPEN/CLOSE history, normalized
        tr = read_trades(b)
        dump_csv("bot_%s_trades.csv" % k, BASE12, [[t[c] for c in BASE12] for t in tr])
        # decisions: today's DECISION rows, key telemetry columns
        dec = read_today_decisions(b)
        dump_csv("bot_%s_decisions.csv" % k, DECISION_COLS,
                 [[r.get(c, "") for c in DECISION_COLS] for r in dec])

    with open(os.path.join(REPORTS, "deployed_commit.txt"), "w") as fh:
        fh.write(deployed_commit() + "\n")
    names.append("deployed_commit.txt")

    # Uniform byte-sha16 for EVERY payload -- deployed_commit.txt is hashed from its bytes exactly
    # like the JSON/CSV files, no special-casing (previously it recorded the commit-string prefix).
    written = manifest_of_dir(REPORTS, names)
    manifest = {"generated_et": NOW_ET.isoformat(), "generated_utc": NOW_UTC.isoformat(),
                "review_slot": mkt.get("review_slot"), "market": mkt.get("market"),
                "deployed_commit": deployed_commit(), "files": written,
                "note": "sha16 = first16(sha256(file bytes)) per file; lets a reviewer skip files "
                        "unchanged since the previous run."}
    with open(os.path.join(REPORTS, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    return manifest


# ---------------- HTML (human/advisor) ----------------
def esc(x):
    return html.escape(str(x))


def money(x, dp=2):
    v = f(x)
    if v is None:
        return "&mdash;"
    cls = "pos" if v > 0 else ("neg" if v < 0 else "zero")
    return '<span class="{c}">${v:+,.{dp}f}</span>'.format(c=cls, v=v, dp=dp)


CSS = """body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;padding:24px;background:#f6f7f9;color:#1a1a1a;line-height:1.45}
h1{margin:0 0 4px}h2{margin:22px 0 8px;border-bottom:2px solid #d0d5dd;padding-bottom:4px}h3{margin:16px 0 6px;color:#344054}
table{border-collapse:collapse;margin:6px 0 14px;font-size:13px;background:#fff}th,td{border:1px solid #e4e7ec;padding:5px 9px;text-align:right}
th{background:#f2f4f7;text-align:center}td:first-child,th:first-child{text-align:left}
table.kv th{background:#f2f4f7;width:170px}table.kv td{text-align:left}
.pos{color:#067647;font-weight:600}.neg{color:#b42318;font-weight:600}.zero{color:#667085}
.warn{color:#b54708;background:#fffaeb;padding:6px 10px;border-radius:6px}.muted{color:#667085;font-size:12px}
.botcard{background:#fff;border:1px solid #e4e7ec;border-radius:10px;padding:14px 18px;margin:14px 0}
tr.atm{background:#fef3f2}.scroll{overflow-x:auto;max-width:100%}
.banner{background:#eef4ff;border:1px solid #b2ccff;border-radius:8px;padding:10px 14px;margin:10px 0}
footer{margin-top:24px;color:#667085;font-size:12px;border-top:1px solid #e4e7ec;padding-top:10px}"""


def render_html(mkt, bots_data):
    P = []
    P.append("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>")
    P.append("<title>S2b Performance %s ET</title><style>%s</style></head><body>" % (NOW_ET.strftime("%m/%d %H:%M"), CSS))
    P.append("<h1>S2b Bot Performance Report</h1>")
    P.append("<div class='banner'><b>Generated:</b> %s ET &nbsp;|&nbsp; <b>Slot:</b> %s &nbsp;|&nbsp; <b>Market:</b> %s &nbsp;|&nbsp; <b>Deployed:</b> %s</div>"
             % (NOW_ET.strftime("%Y-%m-%d %H:%M:%S"), mkt.get("review_slot"), mkt.get("market"), deployed_commit()[:12]))
    # exec summary
    P.append("<h2>Executive summary</h2><table><tr><th>Bot</th><th>Type</th><th>Equity</th><th>Realized today</th><th>Unrealized</th><th>Open</th><th>Opens</th><th>Closes</th><th>Halted</th><th>Total P&amp;L</th></tr>")
    for b in BOTS:
        d = bots_data[b["key"]]
        h = d.get("history", {})
        P.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%d</td><td>%d</td><td>%d</td><td>%s</td><td>%s</td></tr>"
                 % (d["bot"], d["kind"], ("${:,.0f}".format(d["equity"]) if d.get("equity") is not None else "&mdash;"),
                    money(d["realized_today"]), money(d["unrealized"]), len(d["positions"]),
                    len(d["opens_today"]), len(d["closes_today"]), ("YES" if d["halted"] else "no"),
                    money(h.get("total_realized"))))
    P.append("</table>")
    # market
    P.append("<h2>Market &mdash; SPY</h2>")
    dl = mkt.get("daily", {})
    if dl:
        P.append("<table><tr><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>ATR14</th><th>VIX</th></tr>"
                 "<tr><td>%s</td><td>%s</td><td>%s</td><td><b>%s</b></td><td>%s</td><td>%s</td></tr></table>"
                 % (dl.get("open"), dl.get("high"), dl.get("low"), dl.get("close"), mkt.get("atr14"), mkt.get("vix")))
    bars = mkt.get("bars_5min") or []
    if bars:
        P.append("<h3>SPY 5-minute bars (%d)</h3><div class='scroll'><table><tr><th>Time</th><th>O</th><th>H</th><th>L</th><th>C</th><th>VWAP</th><th>Vol</th></tr>" % len(bars))
        for b in bars:
            P.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                     % (b["time"], b["open"], b["high"], b["low"], b["close"], b["vwap"], b["volume"]))
        P.append("</table></div>")
    # per bot
    P.append("<h2>Bot detail</h2>")
    for b in BOTS:
        d = bots_data[b["key"]]
        P.append("<div class='botcard'><h2>%s &mdash; %s</h2>" % (esc(d["bot"]), esc(d["kind"])))
        stale = (" <span class='neg'>(STALE &mdash; counter from %s)</span>" % esc(d.get("risk_day"))
                 if d.get("realized_is_stale") else "")
        P.append("<table class='kv'><tr><th>Equity</th><td>%s</td><th>Realized today</th><td>%s</td></tr>"
                 "<tr><th>Unrealized</th><td>%s</td><th>Halted</th><td>%s</td></tr>"
                 "<tr><th>Report date</th><td>%s</td><th>Risk day</th><td>%s%s</td></tr>"
                 "<tr><th>Realized (risk-day counter)</th><td>%s</td><th>Lifetime</th><td>%s</td></tr>"
                 "<tr><th>P&amp;L source</th><td>%s</td><th>Validation</th><td>%s</td></tr></table>"
                 % (("${:,.2f}".format(d["equity"]) if d.get("equity") is not None else "&mdash;"),
                    money(d["realized_today"]), money(d["unrealized"]),
                    ("<span class='neg'>YES: %s</span>" % esc(d["halt_reason"]) if d["halted"] else "no"),
                    esc(d.get("report_date")), esc(d.get("risk_day")), stale,
                    money(d.get("realized_risk_day")), money(d.get("lifetime_realized")),
                    esc(d.get("pnl_source")), esc(d.get("pnl_validation_status"))))
        P.append("<h3>Open positions</h3>")
        if d["positions"]:
            P.append("<table><tr><th>Spread</th><th>Qty</th><th>Credit</th><th>Mark</th><th>Unreal</th><th>Dist→short</th><th>ATR</th><th>Stop@</th><th>Exp</th></tr>")
            for p in d["positions"]:
                cls = " class='atm'" if p["at_the_money"] else ""
                P.append("<tr%s><td>%s/%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                         % (cls, p["short"], p["long"], (" &#9888;ATM" if p["at_the_money"] else ""), p["qty"], p["credit"],
                            p["mark"] if p["mark"] is not None else "&mdash;", money(p["unrealized"]),
                            ("%+.2f" % p["dist_to_short_pts"]) if p["dist_to_short_pts"] is not None else "&mdash;",
                            ("%+.2f" % p["dist_to_short_atr"]) if p["dist_to_short_atr"] is not None else "&mdash;",
                            p["stop_at"], p["expiry"]))
            P.append("</table>")
        else:
            P.append("<p>No open positions.</p>")
        P.append("<h3>Captured today &mdash; %d opens, %d closes</h3>" % (len(d["opens_today"]), len(d["closes_today"])))
        rows = [("OPEN", t) for t in d["opens_today"]] + [("CLOSE", t) for t in d["closes_today"]]
        if rows:
            P.append("<table><tr><th>Event</th><th>Spread</th><th>Qty</th><th>Credit</th><th>Exit</th><th>P&amp;L</th><th>Status</th></tr>")
            for ev, t in rows:
                P.append("<tr><td>%s</td><td>%s/%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                         % (ev, t["short"], t["long"], t["qty"], t["credit"], t.get("exit_value", "&mdash;"),
                            money(t.get("pnl")) if t.get("pnl") else "&mdash;", t["status"]))
            P.append("</table>")
        P.append("<h3>Blocked / missed (%d decisions)</h3>" % d["decisions_today"])
        if d["gate_breakdown"]:
            P.append("<table><tr><th>Gate</th><th>Count</th></tr>")
            for k, v in d["gate_breakdown"].items():
                P.append("<tr><td>%s</td><td>%s</td></tr>" % (esc(k), v))
            P.append("</table>")
        mo = d.get("missed_opportunities") or {}
        if mo.get("with_60m_markout"):
            P.append("<p class='muted'>%d rejected candidates; of %d with a 60-min mark-out, %d favored the seller (mean move %s).</p>"
                     % (mo.get("rejected_candidates", 0), mo["with_60m_markout"], mo.get("favorable_to_seller", 0), mo.get("mean_spread_move_60m")))
        P.append("</div>")
    # history
    P.append("<h2>Historical P&amp;L</h2>")
    for b in BOTS:
        h = bots_data[b["key"]].get("history", {})
        P.append("<h3>%s</h3>" % esc(bots_data[b["key"]]["bot"]))
        if not h:
            P.append("<p>No closed trades.</p>")
            continue
        cum = 0.0
        P.append("<div class='scroll'><table><tr><th>Date</th><th>Realized</th><th>Cumulative</th></tr>")
        for d0, v in h.get("by_day", {}).items():
            cum += v
            P.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (d0, money(v), money(cum)))
        P.append("</table></div>")
        P.append("<table class='kv'><tr><th>Total realized</th><td>%s</td><th>Closed</th><td>%d</td></tr>"
                 "<tr><th>Win/Loss</th><td>%d/%d (%s%%)</td><th>Profit factor</th><td>%s</td></tr>"
                 "<tr><th>Best/Worst</th><td>%s / %s</td><td></td><td></td></tr></table>"
                 % (money(h["total_realized"]), h["closed_trades"], h["wins"], h["losses"],
                    h.get("win_rate_pct"), h.get("profit_factor"), money(h["best"]), money(h["worst"])))
    prov = "; ".join("%s: %s / %s" % (esc(bots_data[b["key"]]["bot"]), esc(bots_data[b["key"]].get("pnl_source")),
                                      esc(bots_data[b["key"]].get("pnl_validation_status"))) for b in BOTS)
    P.append("<footer>Data: live Tradier balances/quotes + on-droplet logs. P&amp;L provenance &mdash; %s. "
             "Synthetic = (credit&minus;mark)&times;100&times;qty. Bot B history before 2026-07-20 carries the "
             "sandbox leg-corruption. Mark-out window is 60 min. Stop = 3&times; credit. Broker snapshots tag each "
             "leg owned/foreign (Bot B shares its sandbox account). Machine-readable copies in "
             "reports/latest/ for automated review.</footer></body></html>" % prov)
    doc = "\n".join(P)
    tmp = OUT_HTML + ".tmp"
    open(tmp, "w").write(doc)
    os.replace(tmp, OUT_HTML)


def build():
    mkt = collect_market()
    brokers = {b["key"]: collect_broker(b) for b in BOTS}
    bots_data = {b["key"]: collect_bot(b, mkt, brokers[b["key"]]) for b in BOTS}
    # Tag shared-account broker legs owned/foreign so a reviewer sees the reconciliation is clean
    # (Bot B runs --shared-account; co-occupant legs are expected, not drift). Uses each bot's book.
    for b in BOTS:
        k = b["key"]
        own = [{"short_strike": p["short"], "long_strike": p["long"], "expiry": p["expiry"]}
               for p in bots_data[k].get("positions", [])]
        if isinstance(brokers[k].get("legs"), list):
            brokers[k]["legs"], brokers[k]["reconciliation"] = label_broker_legs(brokers[k]["legs"], own)
    render_html(mkt, bots_data)
    manifest = write_codex_files(mkt, bots_data, brokers)
    json.dump({"generated_utc": NOW_UTC.isoformat(), "generated_et": NOW_ET.isoformat(),
               "review_slot": mkt.get("review_slot"), "market": mkt.get("market"),
               "deployed_commit": deployed_commit(),
               "bots": {k: {"equity": bots_data[k].get("equity"), "realized_today": bots_data[k].get("realized_today"),
                            "unrealized": bots_data[k].get("unrealized"), "positions": len(bots_data[k]["positions"]),
                            "halted": bots_data[k]["halted"]} for k in ("b", "c")}},
              open(OUT_JSON, "w"), indent=1)
    return mkt.get("review_slot"), manifest


if __name__ == "__main__":
    if not FORCE and not due_now(NOW_ET):
        sys.exit(0)
    try:
        slot, man = build()
        print("wrote HTML + %d Codex files (slot %s) at %s ET" % (len(man["files"]), slot, NOW_ET.strftime("%H:%M")))
    except Exception:
        traceback.print_exc()
        sys.exit(1)
