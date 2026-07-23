#!/usr/bin/env python3
"""S2b dashboard builder. Reads the Codex report files and writes a self-contained dashboard.html.

Runs on the bot droplet (hooked into the gen_report cron). Pure Python stdlib; no external
requests, no external URLs in the output. Two bots only: Bot B (sandbox) and Bot C (LIVE).
"""
import os
import json
import html
from datetime import datetime

# Bot B history before this date is sandbox-corrupted (negative-credit / leg-count corruption;
# see gen_report footer + memory sandbox-does-send-leg-arrays). Excluded from clean series.
BOT_B_CLEAN_FROM = "2026-07-20"

_REC_FIELDS = ["date", "recommendation", "reason", "confidence", "status", "implemented", "outcome"]


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def load_reports(reports_dir, advisor_memory_path):
    out = {"market": _load_json(os.path.join(reports_dir, "market_context.json"))}
    for k in ("b", "c"):
        out[k] = {"perf": _load_json(os.path.join(reports_dir, "bot_%s_performance.json" % k)),
                  "broker": _load_json(os.path.join(reports_dir, "broker_snapshot_%s.json" % k))}
    try:
        with open(advisor_memory_path, encoding="utf-8") as fh:
            out["recommendations"] = parse_recommendations(fh.read())
    except Exception:
        out["recommendations"] = []
    return out


def daily_pnl_series(perf, bot_key):
    by_day = ((perf or {}).get("history") or {}).get("by_day") or {}
    items = sorted(by_day.items())
    if bot_key == "b":
        items = [(d, v) for d, v in items if d >= BOT_B_CLEAN_FROM]
    out, cum = [], 0.0
    for d, v in items:
        cum += float(v)
        out.append({"date": d, "realized": round(float(v), 2), "cumulative": round(cum, 2)})
    return out


def _top_gate(perf):
    gb = (perf or {}).get("gate_breakdown") or {}
    blocks = {k: v for k, v in gb.items() if k not in ("filled", "off_hours")}
    return max(blocks, key=blocks.get) if blocks else None


def health_verdict(perf, bot_key):
    series = daily_pnl_series(perf, bot_key)
    hist = (perf or {}).get("history") or {}
    wr = hist.get("win_rate_pct")
    synth = (perf or {}).get("pnl_source") == "synthetic_mark"
    if not series:
        return {"status": "Insufficient data",
                "narrative": "Not enough clean trading days yet to judge the strategy."
                             + (" P&L is synthetic (live-fill accounting off)." if synth else "")}
    recent = series[-5:]
    recent_sum = round(sum(r["realized"] for r in recent), 2)
    top = _top_gate(perf)
    if (perf.get("entries_today") == 0 and not (perf.get("positions") or [])
            and top and top.startswith("risk_budget")):
        status = "Stalled"
        why = ("Not opening trades — the risk budget (%s) is capping every candidate." % top)
    elif recent_sum > 0 and (wr or 0) >= 60:
        status = "Improving"
        why = ("Up over the last %d trading days (%+.0f) with a %.0f%% win rate." % (len(recent), recent_sum, wr or 0))
    elif recent_sum < 0 or (wr is not None and wr < 45):
        status = "Under pressure"
        why = ("Down over the last %d trading days (%+.0f); win rate %.0f%%." % (len(recent), recent_sum, wr or 0))
    else:
        status = "Steady"
        why = ("Roughly flat over the last %d trading days (%+.0f)." % (len(recent), recent_sum))
    caveat = (" Edge note: Monday-only is the validated schedule; all-days is diluted."
              " Small sample — treat as directional.")
    if synth:
        caveat += " Bot C P&L is synthetic until live-fill accounting is validated."
    return {"status": status, "narrative": why + caveat}


def parse_recommendations(md_text):
    rows = []
    for line in (md_text or "").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 7:
            continue
        if (cells[0].lower() in ("date", ":---", "---")
                or cells[0].startswith("_example_")
                or set(cells[0]) <= {"-", ":"}):
            continue
        rows.append(dict(zip(_REC_FIELDS, cells[:7])))
    return rows


def why_line(perf):
    opens = len((perf or {}).get("opens_today") or [])
    closes = len((perf or {}).get("closes_today") or [])
    top = _top_gate(perf)
    if opens or closes:
        return "Traded today: opened %d, closed %d." % (opens, closes) + (
            " Main limiter on new entries: %s." % top if top else "")
    if top and top.startswith("risk_budget"):
        return "No new trades — risk budget (%s) blocked every candidate." % top
    if top:
        return "No new trades — top blocker was %s." % top
    return "No new trades today."


# ---------------------------------------------------------------------------
# Rendering — one self-contained HTML page (inline CSS + inline JS, no external refs).
# ---------------------------------------------------------------------------

esc = html.escape

_CSS = """
:root{--bg:#0f141b;--panel:#171f2b;--panel2:#1e2836;--edge:#2a3646;--txt:#e6ecf3;
--muted:#93a1b3;--pos:#3fd07a;--neg:#ff6b6b;--accent:#5aa9ff;--warn:#ffcc66;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
.wrap{max-width:1040px;margin:0 auto;padding:16px;}
h1{font-size:20px;margin:0 0 2px;}
h2{font-size:16px;margin:18px 0 8px;}
h3{font-size:14px;margin:14px 0 6px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;}
.sub{color:var(--muted);font-size:12px;margin:0 0 14px;}
.strip{display:flex;flex-wrap:wrap;gap:14px;background:var(--panel);border:1px solid var(--edge);
border-radius:10px;padding:12px 14px;margin-bottom:14px;}
.strip .m{font-size:13px;}
.strip .m b{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;font-weight:600;}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px;}
.tabs button{background:var(--panel);color:var(--muted);border:1px solid var(--edge);
border-radius:8px;padding:8px 14px;font-size:14px;cursor:pointer;}
.tabs button.active{background:var(--accent);color:#04121f;border-color:var(--accent);font-weight:600;}
.tab{display:none;}
.tab.active{display:block;}
.cards{display:grid;grid-template-columns:1fr;gap:14px;}
@media(min-width:760px){.cards{grid-template-columns:1fr 1fr;}}
.card{background:var(--panel);border:1px solid var(--edge);border-radius:10px;padding:14px;}
.card h2{margin-top:0;}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;
border:1px solid var(--edge);}
.b-improving{background:rgba(63,208,122,.15);color:var(--pos);border-color:var(--pos);}
.b-steady{background:rgba(90,169,255,.15);color:var(--accent);border-color:var(--accent);}
.b-pressure{background:rgba(255,107,107,.15);color:var(--neg);border-color:var(--neg);}
.b-stalled{background:rgba(255,204,102,.15);color:var(--warn);border-color:var(--warn);}
.b-info{background:var(--panel2);color:var(--muted);}
.b-synth{background:rgba(255,204,102,.12);color:var(--warn);border-color:var(--warn);}
.b-actual{background:rgba(63,208,122,.12);color:var(--pos);border-color:var(--pos);}
.narr{font-size:13px;color:var(--txt);margin:8px 0;}
.why{font-size:13px;color:var(--muted);margin:6px 0 10px;font-style:italic;}
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 0;}
.kpi{background:var(--panel2);border-radius:8px;padding:8px 10px;}
.kpi b{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;font-weight:600;}
.kpi span{font-size:16px;font-weight:600;}
.pos{color:var(--pos);}.neg{color:var(--neg);}
table{width:100%;border-collapse:collapse;font-size:13px;margin:6px 0 12px;}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--edge);}
th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.03em;}
td.num{text-align:right;font-variant-numeric:tabular-nums;}
.spark{display:block;margin:4px 0;}
.note{color:var(--muted);font-size:12px;}
.foot{color:var(--muted);font-size:11px;margin-top:20px;border-top:1px solid var(--edge);padding-top:10px;}
"""

_JS = """
function showTab(id,btn){
  var t=document.getElementsByClassName('tab');
  for(var i=0;i<t.length;i++){t[i].className='tab';}
  var b=document.querySelectorAll('.tabs button');
  for(var j=0;j<b.length;j++){b[j].className='';}
  document.getElementById(id).className='tab active';
  btn.className='active';
}
"""

_STATUS_CLASS = {
    "Improving": "b-improving", "Steady": "b-steady", "Under pressure": "b-pressure",
    "Stalled": "b-stalled", "Insufficient data": "b-info",
}


def _money(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return '<span class="note">-</span>'
    cls = "pos" if v > 0 else ("neg" if v < 0 else "")
    return '<span class="%s">%s$%s</span>' % (cls, "+" if v > 0 else "", "{:,.0f}".format(v))


def _num(v, fmt="{:,.2f}"):
    try:
        return fmt.format(float(v))
    except (TypeError, ValueError):
        return "-"


def _spark(series, key="cumulative", width=260, height=44):
    pts = [r[key] for r in series]
    if len(pts) < 2:
        return '<span class="note">not enough data</span>'
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    n = len(pts)
    coords = []
    for i, y in enumerate(pts):
        x = (i / (n - 1)) * (width - 4) + 2
        yy = height - 2 - ((y - lo) / span) * (height - 4)
        coords.append("%.1f,%.1f" % (x, yy))
    last_up = pts[-1] >= pts[0]
    color = "#3fd07a" if last_up else "#ff6b6b"
    return ('<svg class="spark" width="%d" height="%d" viewBox="0 0 %d %d">'
            '<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/></svg>'
            % (width, height, width, height, color, " ".join(coords)))


def _bot_card(key, bot):
    perf = bot.get("perf") or {}
    name = perf.get("bot") or ("Bot %s" % key.upper())
    hv = health_verdict(perf, key)
    scls = _STATUS_CLASS.get(hv["status"], "b-info")
    synth = perf.get("pnl_source") == "synthetic_mark"
    src_badge = ('<span class="badge b-synth">synthetic P&amp;L</span>' if synth
                 else '<span class="badge b-actual">actual fill</span>')
    hist = perf.get("history") or {}
    series = daily_pnl_series(perf, key)
    kpis = [
        ("Equity", "$" + _num(perf.get("equity"), "{:,.0f}")),
        ("Today realized" + (" (synthetic)" if synth else ""), _money(perf.get("realized_report_date"))),
        ("Lifetime realized", _money(perf.get("lifetime_realized"))),
        ("Win rate", (_num(hist.get("win_rate_pct"), "{:.1f}") + "%") if hist.get("win_rate_pct") is not None else "-"),
        ("Open positions", str(len(perf.get("positions") or []))),
        ("Opened / Closed today",
         "%d / %d" % (len(perf.get("opens_today") or []), len(perf.get("closes_today") or []))),
    ]
    kh = "".join('<div class="kpi"><b>%s</b><span>%s</span></div>' % (esc(t), v) for t, v in kpis)
    return (
        '<div class="card">'
        '<h2>%s <span class="badge %s">%s</span> %s</h2>'
        '<div class="narr">%s</div>'
        '<div class="why">%s</div>'
        '<div class="kpis">%s</div>'
        '<h3>Daily P&amp;L (cumulative, clean days)</h3>%s'
        '</div>'
        % (esc(name), scls, esc(hv["status"]), src_badge,
           esc(hv["narrative"]), esc(why_line(perf)), kh, _spark(series))
    )


def _pnl_table(series):
    if not series:
        return '<p class="note">No clean trading days yet.</p>'
    rows = "".join(
        '<tr><td>%s</td><td class="num">%s</td><td class="num">%s</td></tr>'
        % (esc(r["date"]), _money(r["realized"]), _money(r["cumulative"]))
        for r in series)
    return ('<table><tr><th>Date</th><th>Realized</th><th>Cumulative</th></tr>%s</table>' % rows)


def _kv_table(d, kh="Key", vh="Value"):
    if not d:
        return '<p class="note">none</p>'
    rows = "".join('<tr><td>%s</td><td class="num">%s</td></tr>' % (esc(str(k)), esc(str(v)))
                   for k, v in d.items())
    return '<table><tr><th>%s</th><th>%s</th></tr>%s</table>' % (esc(kh), esc(vh), rows)


def _bot_perf_section(key, bot):
    perf = bot.get("perf") or {}
    broker = bot.get("broker") or {}
    name = perf.get("bot") or ("Bot %s" % key.upper())
    hist = perf.get("history") or {}
    series = daily_pnl_series(perf, key)
    funnel = perf.get("funnel") or {}
    gates = perf.get("gate_breakdown") or {}
    recon = broker.get("reconciliation") or {}
    stats = {
        "Closed trades": hist.get("closed_trades", "-"),
        "Wins / Losses": "%s / %s" % (hist.get("wins", "-"), hist.get("losses", "-")),
        "Win rate %": hist.get("win_rate_pct", "-"),
        "Profit factor": hist.get("profit_factor", "-"),
        "Best day": hist.get("best", "-"),
        "Worst day": hist.get("worst", "-"),
    }
    recon_d = {
        "Owned legs": recon.get("owned_legs", "-"),
        "Foreign legs": recon.get("foreign_legs", "-"),
    }
    return (
        '<h2>%s</h2>'
        '<h3>Historical daily P&amp;L</h3>%s%s'
        '<h3>Win / loss</h3>%s'
        '<h3>Entry funnel</h3>%s'
        '<h3>Gate breakdown (why blocked / filled)</h3>%s'
        '<h3>Risk-config snapshot</h3>%s'
        '<h3>Broker reconciliation</h3>%s'
        % (esc(name), _pnl_table(series), _spark(series, width=520, height=90),
           _kv_table(stats), _kv_table(funnel, "Stage", "Count"),
           _kv_table(gates, "Gate", "Count"), _kv_table(perf.get("config") or {}, "Setting", "Value"),
           _kv_table(recon_d))
    )


def _recs_table(rows):
    if not rows:
        return '<p class="note">none</p>'
    body = "".join(
        '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
        % (esc(r.get("date", "")), esc(r.get("recommendation", "")), esc(r.get("reason", "")),
           esc(r.get("confidence", "")), esc(r.get("implemented", "")), esc(r.get("outcome", "")))
        for r in rows)
    return ('<table><tr><th>Date</th><th>Recommendation</th><th>Reason</th>'
            '<th>Confidence</th><th>Implemented</th><th>Outcome</th></tr>%s</table>' % body)


def render_html(data, generated_at):
    market = data.get("market") or {}
    daily = market.get("daily") or {}
    close = daily.get("close")
    op = daily.get("open")
    pct = ""
    try:
        if op:
            pct = " (%+.2f%%)" % ((float(close) - float(op)) / float(op) * 100.0)
    except (TypeError, ValueError, ZeroDivisionError):
        pct = ""
    pvw = market.get("price_vs_vwap_pts")
    try:
        regime = "quiet" if float(market.get("vix") or 0) < 20 else "elevated"
    except (TypeError, ValueError):
        regime = "unknown"
    try:
        regime += ", closed below VWAP" if float(pvw) < 0 else ", closed above VWAP"
    except (TypeError, ValueError):
        pass

    strip = (
        '<div class="strip">'
        '<div class="m"><b>SPY close</b>%s%s</div>'
        '<div class="m"><b>VIX</b>%s</div>'
        '<div class="m"><b>Session VWAP</b>%s</div>'
        '<div class="m"><b>Price vs VWAP</b>%s pts</div>'
        '<div class="m"><b>Regime</b>%s</div>'
        '</div>'
        % (esc(_num(close, "{:,.2f}")), esc(pct), esc(_num(market.get("vix"), "{:.2f}")),
           esc(_num(market.get("session_vwap"), "{:,.2f}")), esc(_num(pvw, "{:+.2f}")), esc(regime))
    )

    overview = ('<div id="overview" class="tab active">%s<div class="cards">%s%s</div></div>'
                % (strip, _bot_card("b", data.get("b") or {}), _bot_card("c", data.get("c") or {})))

    perf_tab = ('<div id="performance" class="tab">%s%s</div>'
                % (_bot_perf_section("b", data.get("b") or {}),
                   _bot_perf_section("c", data.get("c") or {})))

    recs = data.get("recommendations") or []
    pending = [r for r in recs if r.get("status", "").lower() != "implemented"]
    implemented = [r for r in recs if r.get("status", "").lower() == "implemented"]
    recs_tab = ('<div id="recs" class="tab">'
                '<h2>Pending</h2>%s<h2>Implemented</h2>%s</div>'
                % (_recs_table(pending), _recs_table(implemented)))

    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>S2b Dashboard — Bot B &amp; Bot C</title>'
        "<style>%s</style></head><body><div class=\"wrap\">"
        "<h1>S2b Dashboard</h1>"
        '<p class="sub">Bot B (sandbox) &amp; Bot C (LIVE) — generated %s ET. Read-only.</p>'
        '<div class="tabs">'
        '<button class="active" onclick="showTab(\'overview\',this)">Overview</button>'
        '<button onclick="showTab(\'performance\',this)">Performance</button>'
        '<button onclick="showTab(\'recs\',this)">Recommendations</button>'
        '</div>'
        "%s%s%s"
        '<div class="foot">Self-contained static page. P&amp;L marked synthetic for Bot C is '
        'un-audited; Bot B history before %s is excluded as sandbox-corrupted. '
        'Edge caveat: Monday-only is the validated schedule; all-days is diluted.</div>'
        "<script>%s</script></div></body></html>"
        % (_CSS, esc(generated_at), overview, perf_tab, recs_tab, BOT_B_CLEAN_FROM, _JS)
    )


def build(reports_dir, advisor_memory_path, out_path):
    data = load_reports(reports_dir, advisor_memory_path)
    # Prefer the report's own ET timestamp (gen_report writes proper ET via zoneinfo); the droplet
    # runs in UTC, so datetime.now() would mislabel the hour as ET. Fall back to local time.
    gen_et = ((data.get("market") or {}).get("generated_et") or "")[:16].replace("T", " ")
    generated_at = gen_et or datetime.now().strftime("%Y-%m-%d %H:%M")
    doc = render_html(data, generated_at=generated_at)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(doc)
    os.replace(tmp, out_path)


if __name__ == "__main__":
    BASE = "/root/s2b-bot"
    build(BASE + "/reports/latest", BASE + "/reports/advisor_memory.md",
          BASE + "/reports/latest/dashboard.html")
    print("wrote dashboard.html")
