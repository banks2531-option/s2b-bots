#!/usr/bin/env python3
"""S2b A/B dashboard generator.

Self-contained generator (modeled on the legacy trading-bot dashboard) that SSHes the droplet,
pulls live state + trade logs + Tradier sandbox marks for BOTH S2b bots (Bot A Monday-only and
Bot B all-days), and writes a single auto-refreshing dashboard.html.

  python build_dashboard.py --once                 # generate once and exit
  python build_dashboard.py --watch                # regenerate every 60s
  python build_dashboard.py --watch --interval 30  # custom interval

Then open dashboard/dashboard.html in a browser (it self-refreshes every `interval` seconds).

Data sources (on the droplet, /root/s2b-bot):
  state_monday.json / state_alldays.json   - per-bot persisted state (open positions, halt, entries)
  trades_monday.csv / trades_alldays.csv   - per-bot trade/P&L log (the A/B record)
  s2b.env                                  - Tradier token + sandbox base url (read on droplet only)
  systemctl is-active s2b-{monday,alldays} - process health
Tradier sandbox is queried ON the droplet for live equity + option marks (keys never leave it).
"""
import argparse
import html
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SSH_KEY = str(Path.home() / ".ssh" / "id_ed25519_do")
DROPLET = "root@159.89.45.162"
OUT_HTML = SCRIPT_DIR / "dashboard.html"

# ── Droplet-side data collection: read both bots' files, query Tradier, emit one JSON blob ──
DROPLET_QUERY = r'''
import json, csv, os, subprocess, urllib.request
from datetime import datetime, timezone

APP = "/root/s2b-bot"
# each bot: state, trade log, service, and the env file holding ITS creds (sandbox vs production)
BOTS = {"MONDAY":  ("state_monday.json",  "trades_monday.csv",  "s2b-monday.service",  "s2b.env"),
        "ALLDAYS": ("state_alldays.json", "trades_alldays.csv", "s2b-alldays.service", "s2b.env"),
        "LIVE":    ("state_live.json",    "trades_live.csv",    "s2b-live.service",    "s2b-live.env")}

def load_env(fn):
    e, p = {}, os.path.join(APP, fn)
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1); e[k] = v
    return e

_ctx = {}
def ctx(env_file):
    """Per-env Tradier context (sandbox bots share s2b.env; LIVE uses s2b-live.env / production)."""
    if env_file in _ctx:
        return _ctx[env_file]
    e = load_env(env_file)
    tok = e.get("TRADIER_TOKEN", ""); base = e.get("TRADIER_BASE_URL", "https://sandbox.tradier.com/v1")
    acct = e.get("TRADIER_ACCOUNT_ID", "")
    def api(path):
        req = urllib.request.Request(base + path,
            headers={"Authorization": "Bearer " + tok, "Accept": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=15))
    def quotes(symbols):
        if not symbols:
            return {}
        try:
            d = api("/markets/quotes?symbols=" + ",".join(symbols))["quotes"]["quote"]
            d = d if isinstance(d, list) else [d]
            return {x["symbol"]: x for x in d}
        except Exception:
            return {}
    try:
        equity = float(api("/accounts/%s/balances" % acct)["balances"]["total_equity"])
    except Exception:
        equity = None
    c = {"api": api, "quotes": quotes, "equity": equity, "live": "sandbox" not in base}
    _ctx[env_file] = c
    return c

def occ(strike, expiry):
    return "SPY%sP%08d" % (expiry[2:].replace("-", ""), int(strike * 1000))

def is_active(svc):
    try:
        return subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True
                              ).stdout.strip() == "active"
    except Exception:
        return False

today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
out = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), "bots": {}}

# market context (SPY/VIX) from the sandbox feed
mq = ctx("s2b.env")["quotes"](["SPY", "VIX"])
spy = mq.get("SPY", {}); vix = mq.get("VIX", {})
out["market"] = {
    "SPY": {"last": spy.get("last"), "chg": spy.get("change_percentage"),
            "low": spy.get("low"), "high": spy.get("high")},
    "VIX": {"last": vix.get("last"), "chg": vix.get("change_percentage")},
}

for tag, (sf, cf, svc, envf) in BOTS.items():
    c = ctx(envf)
    sp = os.path.join(APP, sf)
    st = json.load(open(sp)) if os.path.exists(sp) else {"open_positions": []}
    legs = []
    for pos in st.get("open_positions", []):
        legs.append(occ(pos["short_strike"], pos["expiry"])); legs.append(occ(pos["long_strike"], pos["expiry"]))
    qmap = c["quotes"](sorted(set(legs)))
    def mid(sym, _qmap=qmap):
        o = _qmap.get(sym)
        if not o or o.get("bid") is None or o.get("ask") is None:
            return None
        return (o["bid"] + o["ask"]) / 2.0
    open_live = []
    unreal = 0.0
    for pos in st.get("open_positions", []):
        smid, lmid = mid(occ(pos["short_strike"], pos["expiry"])), mid(occ(pos["long_strike"], pos["expiry"]))
        val = (smid - lmid) if (smid is not None and lmid is not None) else None
        pnl = None
        if val is not None:
            pnl = round((pos["credit"] - val) * 100 * pos["qty"], 2)
            unreal += pnl
        cushion = None
        if spy.get("last") is not None:
            cushion = round(spy["last"] - pos["short_strike"], 1)
        open_live.append({"short": pos["short_strike"], "long": pos["long_strike"],
                          "credit": pos["credit"], "qty": pos["qty"], "expiry": pos["expiry"],
                          "mid": round(val, 2) if val is not None else None,
                          "unreal": pnl, "cushion": cushion})
    # trade log: realized closes (filled only) + today's activity + rejected count
    cf_path = os.path.join(APP, cf)
    closed, today_rows, realized_today, realized_all, rejected = [], [], 0.0, 0.0, 0
    if os.path.exists(cf_path):
        rows = list(csv.DictReader(open(cf_path)))
        for r in rows:
            status = (r.get("status") or "").lower()
            if r.get("event") == "CLOSE" and status not in ("filled",):
                rejected += 1
            if r.get("date") == today:
                today_rows.append(r)
            if r.get("event") == "CLOSE" and status == "filled":
                pnl = float(r["pnl"]) if r.get("pnl") not in (None, "") else 0.0
                realized_all += pnl
                if r.get("date") == today:
                    realized_today += pnl
                closed.append(r)
    out["bots"][tag] = {
        "running": is_active(svc), "live": c["live"],
        "halted": st.get("halted", False), "halt_reason": st.get("halt_reason", ""),
        "entries_today": st.get("entries_today"), "last_entry": st.get("last_entry_date"),
        "equity": c["equity"], "open_live": open_live, "unrealized": round(unreal, 2),
        "realized_today": round(realized_today, 2), "realized_all": round(realized_all, 2),
        "closed": closed, "today_rows": today_rows, "rejected": rejected,
    }

print(json.dumps(out))
'''


def ssh_run(payload):
    """Run the droplet-side python payload over SSH and parse its JSON stdout."""
    cmd = ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
           DROPLET, "python3 - <<'PYEOF'\n" + payload + "\nPYEOF"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError("ssh failed: " + (r.stderr or "")[-500:])
    return json.loads(r.stdout)


# ───────────────────────────── rendering ─────────────────────────────
CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#c9d1d9;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
a{color:#58a6ff}
.wrap{max-width:1100px;margin:0 auto;padding:18px}
h1{font-size:20px;margin:0 0 2px}
.sub{color:#8b949e;font-size:12px;margin-bottom:14px}
.tabs{display:flex;gap:8px;margin:14px 0}
.tab{padding:7px 14px;border:1px solid #30363d;border-radius:6px;cursor:pointer;color:#c9d1d9;background:#161b22}
.tab.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
.panel{display:none}.panel.active{display:block}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin-bottom:14px}
.card h2{font-size:15px;margin:0 0 10px;display:flex;align-items:center;gap:10px}
.kpis{display:flex;flex-wrap:wrap;gap:14px;margin:6px 0 10px}
.kpi{min-width:120px}.kpi .v{font-size:18px;font-weight:600}.kpi .l{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:.4px}
.pill{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600}
.pill-up{background:#1a3326;color:#3fb950;border:1px solid #2ea04326}
.pill-down{background:#3a1a1a;color:#f85149;border:1px solid #f8514926}
.pill-warn{background:#3a2f17;color:#d29922;border:1px solid #d2992226}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid #21262d}
th:first-child,td:first-child{text-align:left}
th{color:#8b949e;font-weight:600;font-size:11px;text-transform:uppercase;cursor:pointer;user-select:none}
.pos{color:#3fb950}.neg{color:#f85149}.flat{color:#8b949e}
.market{display:flex;gap:18px;flex-wrap:wrap;font-size:13px}
.market b{color:#fff}
.muted{color:#8b949e;font-size:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:760px){.grid2{grid-template-columns:1fr}}
"""

SORT_JS = """
function cell(td){var v=td.getAttribute('data-v');if(v!==null)return v;return td.textContent.trim();}
function sortTable(t,i,type,asc){
 var rows=Array.prototype.slice.call(t.tBodies[0].rows);
 rows.sort(function(a,b){var x=cell(a.cells[i]),y=cell(b.cells[i]);
  if(type==='num'){x=parseFloat(x)||0;y=parseFloat(y)||0;return asc?x-y:y-x;}
  return asc?(''+x).localeCompare(y):(''+y).localeCompare(x);});
 rows.forEach(function(r){t.tBodies[0].appendChild(r);});
}
document.querySelectorAll('table.sortable thead th').forEach(function(th){
 th.addEventListener('click',function(){
  var t=th.closest('table'),i=Array.prototype.indexOf.call(th.parentNode.children,th);
  var type=th.getAttribute('data-type')||'text';var asc=th.getAttribute('data-asc')!=='1';
  th.setAttribute('data-asc',asc?'1':'0');sortTable(t,i,type,asc);});
});
function showTab(id){
 document.querySelectorAll('.tab').forEach(function(t){t.classList.remove('active');});
 document.querySelectorAll('.panel').forEach(function(p){p.classList.remove('active');});
 document.getElementById('tab-'+id).classList.add('active');
 document.getElementById('panel-'+id).classList.add('active');
}
"""


def money(v, plus=False):
    if v is None:
        return "<span class='flat'>—</span>"
    cls = "pos" if v > 0 else ("neg" if v < 0 else "flat")
    s = ("%+.0f" if plus else "%.0f") % v
    return "<span class='%s'>$%s</span>" % (cls, s)


def pill(ok, up_txt, down_txt):
    return ("<span class='pill pill-up'>%s</span>" % up_txt) if ok else \
           ("<span class='pill pill-down'>%s</span>" % down_txt)


def render_market(m):
    spy, vix = m.get("SPY", {}), m.get("VIX", {})
    def fmt(d, sym):
        last = d.get("last")
        chg = d.get("chg")
        if last is None:
            return "%s <span class='muted'>n/a</span>" % sym
        c = "pos" if (chg or 0) > 0 else ("neg" if (chg or 0) < 0 else "flat")
        return "<b>%s</b> $%.2f <span class='%s'>%+.2f%%</span>" % (sym, last, c, chg or 0)
    rng = ""
    if spy.get("low") is not None:
        rng = " <span class='muted'>range $%.2f–$%.2f</span>" % (spy["low"], spy["high"])
    return "<div class='market'>%s%s<span>%s</span></div>" % (fmt(spy, "SPY"), rng, fmt(vix, "VIX"))


BOT_LABEL = {"MONDAY": "Bot A · Monday-only (sandbox)",
             "ALLDAYS": "Bot B · all-days (sandbox)",
             "LIVE": "Bot C · LIVE $1-wing all-days (REAL MONEY)"}


def render_bot_overview(tag, b):
    status = pill(b["running"] and not b["halted"], "RUNNING", "HALTED" if b["halted"] else "STOPPED")
    if b.get("live"):
        status += " <span class='pill pill-down'>● REAL MONEY</span>"
    if b["halted"]:
        status += " <span class='pill pill-warn'>halt: %s</span>" % html.escape(b.get("halt_reason") or "")
    net = (b.get("realized_today") or 0) + (b.get("unrealized") or 0)
    kpis = "".join("<div class='kpi'><div class='v'>%s</div><div class='l'>%s</div></div>" % (v, l)
        for v, l in [
            (money(b.get("equity")), "Equity"),
            (money(b.get("realized_today"), True), "Realized today"),
            (money(b.get("unrealized"), True), "Unrealized"),
            (money(net, True), "Net day"),
            (money(b.get("realized_all"), True), "Realized all-time"),
            ("%s/%s" % (b.get("entries_today"), len(b.get("open_live") or [])), "Entries today / open"),
        ])
    # open positions
    if b["open_live"]:
        body = "".join(
            "<tr><td>%d/%d</td><td>%s</td><td data-v='%.2f'>%.2f</td><td data-v='%.2f'>%.2f</td>"
            "<td data-v='%s'>%s</td><td data-v='%s'>%s</td></tr>" % (
                p["short"], p["long"], html.escape(p["expiry"]), p["credit"], p["credit"],
                (p["mid"] or 0), (p["mid"] if p["mid"] is not None else 0),
                (p["unreal"] if p["unreal"] is not None else 0), money(p["unreal"], True),
                (p["cushion"] if p["cushion"] is not None else 0),
                ("%.1f pt" % p["cushion"]) if p["cushion"] is not None else "—")
            for p in b["open_live"])
        postbl = ("<table class='sortable'><thead><tr><th>Spread</th><th>Exp</th>"
                  "<th data-type='num'>Credit</th><th data-type='num'>Mid</th>"
                  "<th data-type='num'>Unreal</th><th data-type='num'>Cushion</th></tr></thead>"
                  "<tbody>%s</tbody></table>" % body)
    else:
        postbl = "<div class='muted'>flat — no open positions</div>"
    # today's activity
    tt = ""
    if b["today_rows"]:
        trows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s/%s</td><td>%s</td><td data-v='%s'>%s</td></tr>" % (
                html.escape(r.get("event", "")), html.escape(r.get("action") or "—"),
                r.get("short", ""), r.get("long", ""), html.escape(r.get("status") or ""),
                (r.get("pnl") or 0), money(float(r["pnl"]) if r.get("pnl") not in (None, "") else None, True))
            for r in b["today_rows"] if (r.get("status") or "").lower() in ("filled",))
        if trows:
            tt = ("<div class='muted' style='margin-top:8px'>Today</div>"
                  "<table><thead><tr><th>Event</th><th>Action</th><th>Spread</th>"
                  "<th>Status</th><th>P&L</th></tr></thead><tbody>%s</tbody></table>" % trows)
    rej = ""
    if b["rejected"]:
        rej = "<div class='muted' style='margin-top:6px'>⚠ %d rejected/errored close attempt(s) in log (historical)</div>" % b["rejected"]
    return ("<div class='card'><h2>%s %s</h2>%s<div class='kpis'>%s</div>%s%s%s</div>" % (
        BOT_LABEL[tag], status, "", kpis, postbl, tt, rej))


def render_history(tag, b):
    closed = b["closed"]
    n = len(closed)
    wins = sum(1 for r in closed if float(r.get("pnl") or 0) > 0)
    net = sum(float(r.get("pnl") or 0) for r in closed)
    wr = (wins / n * 100) if n else 0
    avg = (net / n) if n else 0
    kpis = "".join("<div class='kpi'><div class='v'>%s</div><div class='l'>%s</div></div>" % (v, l)
        for v, l in [
            (str(n), "Closed trades"),
            ("%.0f%%" % wr, "Win rate"),
            (money(net, True), "Net realized"),
            (money(avg, True), "Avg / trade"),
        ])
    rows = ""
    for r in sorted(closed, key=lambda x: (x.get("date", ""), x.get("short", "")), reverse=True):
        pnl = float(r.get("pnl") or 0)
        rows += ("<tr><td>%s</td><td>%s/%s</td><td>%s</td><td>%s</td>"
                 "<td data-v='%s'>%s</td><td data-v='%s'>%s</td></tr>" % (
            html.escape(r.get("date", "")), r.get("short", ""), r.get("long", ""),
            html.escape(r.get("expiry", "")), html.escape(r.get("action") or ""),
            r.get("credit", ""), r.get("credit", ""), pnl, money(pnl, True)))
    if not rows:
        rows = "<tr><td colspan='6' class='muted'>no closed trades yet</td></tr>"
    tbl = ("<table class='sortable'><thead><tr><th>Close date</th><th>Spread</th><th>Exp</th>"
           "<th>Exit</th><th data-type='num'>Credit</th><th data-type='num'>P&L</th></tr></thead>"
           "<tbody>%s</tbody></table>" % rows)
    return "<div class='card'><h2>%s</h2><div class='kpis'>%s</div>%s</div>" % (
        BOT_LABEL[tag], kpis, tbl)


def render_html(data, refresh):
    bots = data["bots"]
    order = ("LIVE", "ALLDAYS", "MONDAY")   # show the real-money bot first
    overview = "".join(render_bot_overview(t, bots[t]) for t in order if t in bots)
    history = "".join(render_history(t, bots[t]) for t in order if t in bots)
    return """<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<meta http-equiv='refresh' content='%d'>
<title>S2b A/B Dashboard</title><style>%s</style></head><body><div class='wrap'>
<h1>S2b A/B Dashboard</h1>
<div class='sub'>Bot A (Monday-only) vs Bot B (all-days) · SPY bull put spreads · Tradier sandbox ·
generated %s · auto-refresh %ds</div>
<div class='card' style='padding:10px 14px'>%s</div>
<div class='tabs'>
 <div class='tab active' id='tab-ov' onclick="showTab('ov')">Overview</div>
 <div class='tab' id='tab-hist' onclick="showTab('hist')">Trade History</div>
</div>
<div class='panel active' id='panel-ov'>%s</div>
<div class='panel' id='panel-hist'>%s</div>
</div><script>%s</script></body></html>""" % (
        refresh, CSS, html.escape(data.get("generated", "")), refresh,
        render_market(data.get("market", {})), overview, history, SORT_JS)


def generate_once(refresh):
    data = ssh_run(DROPLET_QUERY)
    OUT_HTML.write_text(render_html(data, refresh), encoding="utf-8")
    b = data["bots"]
    print("[%s] wrote %s | Bot A net $%.0f | Bot B net $%.0f" % (
        data.get("generated"), OUT_HTML,
        (b.get("MONDAY", {}).get("realized_all") or 0) + (b.get("MONDAY", {}).get("unrealized") or 0),
        (b.get("ALLDAYS", {}).get("realized_all") or 0) + (b.get("ALLDAYS", {}).get("unrealized") or 0)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the S2b A/B dashboard.")
    ap.add_argument("--once", action="store_true", help="generate once and exit")
    ap.add_argument("--watch", action="store_true", help="regenerate on a loop")
    ap.add_argument("--interval", type=int, default=300, help="seconds between regenerations (watch); default 5 min")
    args = ap.parse_args(argv)
    if args.watch:
        print("watching — regenerating every %ds (Ctrl-C to stop)" % args.interval)
        while True:
            try:
                generate_once(args.interval)
            except Exception as exc:
                print("error: %s" % exc, file=sys.stderr)
            time.sleep(args.interval)
    else:
        generate_once(args.interval)


if __name__ == "__main__":
    main()
