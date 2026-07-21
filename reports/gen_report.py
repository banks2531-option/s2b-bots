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
import os, sys, json, csv, html, hashlib, glob, urllib.request, urllib.parse, collections, traceback
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

# The 5 advisor review slots, in ET minutes-since-midnight. The last one is the EOD full session.
SLOTS = {600: "10:00-open", 720: "12:00-midday", 840: "14:00-afternoon",
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
            "equity": f(broker.get("equity")), "realized_today": f(st.get("realized_today")),
            "risk_day": st.get("risk_day"), "halted": bool(st.get("halted")),
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
        plist.append({"short": s, "long": l, "qty": q, "credit": cr, "mark": mark, "unrealized": pnl,
                      "dist_to_short_pts": dist, "dist_to_short_atr": distatr, "stop_at": round(cr * 3, 2),
                      "expiry": exp, "at_the_money": bool(distatr is not None and distatr <= 0.10)})
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
    written = {}

    def dump_json(name, obj):
        path = os.path.join(REPORTS, name)
        txt = json.dumps(obj, indent=1, default=str)
        with open(path, "w") as fh:
            fh.write(txt)
        written[name] = hashlib.sha256(txt.encode()).hexdigest()[:16]

    def dump_csv(name, header, rows):
        path = os.path.join(REPORTS, name)
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        txt = buf.getvalue()
        with open(path, "w", newline="") as fh:
            fh.write(txt)
        written[name] = hashlib.sha256(txt.encode()).hexdigest()[:16]

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
        cols = ["date", "decision", "limiting_gate", "expected_executable_credit", "credit_ratio",
                "credit_threshold", "agg_gap_stress_1_5", "agg_remaining_stop", "agg_structural",
                "risk_budget_proposed_qty", "risk_budget_permitted_qty", "risk_budget_headroom"]
        dump_csv("bot_%s_decisions.csv" % k, cols, [[r.get(c, "") for c in cols] for r in dec])

    with open(os.path.join(REPORTS, "deployed_commit.txt"), "w") as fh:
        fh.write(deployed_commit() + "\n")
    written["deployed_commit.txt"] = deployed_commit()[:16]

    manifest = {"generated_et": NOW_ET.isoformat(), "generated_utc": NOW_UTC.isoformat(),
                "review_slot": mkt.get("review_slot"), "market": mkt.get("market"),
                "deployed_commit": deployed_commit(), "files": written,
                "note": "sha16 per file lets a reviewer skip files unchanged since the previous run."}
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
        P.append("<table class='kv'><tr><th>Equity</th><td>%s</td><th>Realized today</th><td>%s</td></tr>"
                 "<tr><th>Unrealized</th><td>%s</td><th>Halted</th><td>%s</td></tr></table>"
                 % (("${:,.2f}".format(d["equity"]) if d.get("equity") is not None else "&mdash;"),
                    money(d["realized_today"]), money(d["unrealized"]),
                    ("<span class='neg'>YES: %s</span>" % esc(d["halt_reason"]) if d["halted"] else "no")))
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
    P.append("<footer>Data: live Tradier balances/quotes + on-droplet logs. P&amp;L is SYNTHETIC "
             "(actual_fill_accounting OFF both bots) = (credit&minus;mark)&times;100&times;qty. Bot B history "
             "before 2026-07-20 carries the sandbox leg-corruption. Mark-out window is 60 min. Stop = 3&times; credit. "
             "Standardized machine-readable copies in reports/latest/ for automated review.</footer></body></html>")
    doc = "\n".join(P)
    tmp = OUT_HTML + ".tmp"
    open(tmp, "w").write(doc)
    os.replace(tmp, OUT_HTML)


def build():
    mkt = collect_market()
    brokers = {b["key"]: collect_broker(b) for b in BOTS}
    bots_data = {b["key"]: collect_bot(b, mkt, brokers[b["key"]]) for b in BOTS}
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
    if not FORCE and not should_generate(NOW_ET):
        sys.exit(0)
    try:
        slot, man = build()
        print("wrote HTML + %d Codex files (slot %s) at %s ET" % (len(man["files"]), slot, NOW_ET.strftime("%H:%M")))
    except Exception:
        traceback.print_exc()
        sys.exit(1)
