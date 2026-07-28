#!/usr/bin/env python3
"""Live 30-second dashboard refresh.

Every ~30s, for each bot, pulls CURRENT equity (Tradier balances) + marks for the bot's own open
positions (Tradier quotes) and computes unrealized P&L, writing reports/latest/live_{b,c}.json.
Then rebuilds the dashboard and copies it to the served dir so the page (which auto-refreshes every
30s) shows moving numbers between the 6x/day review-pipeline passes.

READ-ONLY against the broker (balances + quotes only — never orders). Runs on the bot droplet as a
loop daemon under systemd. Uses each bot's OWN state book (state_*.json), so Bot B's shared-account
co-occupants are excluded from its unrealized.
"""
import os
import json
import time
import traceback
import urllib.request
import urllib.parse
import subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE = "/root/s2b-bot"
REPORTS = BASE + "/reports/latest"
SERVED = "/var/www/s2b/index.html"
ET = ZoneInfo("America/New_York")
INTERVAL = 30

BOTS = [
    {"key": "b", "env": "s2b.env", "state": "state_alldays.json"},
    {"key": "c", "env": "s2b-live.env", "state": "state_live.json"},
]


def load_env(path):
    e = {}
    for ln in open(path):
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            e[k] = v.strip().strip('"').strip("'")
    return e


def occ(exp, k):
    y, m, d = exp.split("-")
    return "SPY%s%s%sP%08d" % (y[2:], m, d, int(float(k) * 1000))


def _get(env, path, **kw):
    url = env["TRADIER_BASE_URL"] + path + ("?" + urllib.parse.urlencode(kw) if kw else "")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + env["TRADIER_TOKEN"],
                                               "Accept": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=15))


def pull_bot(bot):
    env = load_env(BASE + "/" + bot["env"])
    acct = env["TRADIER_ACCOUNT_ID"]
    out = {"generated_et": datetime.now(timezone.utc).astimezone(ET).isoformat()}
    bal = (_get(env, "/accounts/%s/balances" % acct) or {}).get("balances") or {}
    out["equity"] = bal.get("total_equity")
    try:
        st = json.load(open(BASE + "/" + bot["state"]))
    except Exception:
        st = {}
    # Surface the halt state in the 30s overlay so the dashboard's halt alert is near-real-time (the
    # 6x/day perf JSON would otherwise be up to ~5 min stale). Read straight from the bot's state file.
    out["halted"] = bool(st.get("halted"))
    out["halt_reason"] = st.get("halt_reason") or ""
    own = st.get("open_positions") or []
    # Own leg OCC symbols. Bot B runs --shared-account, so the broker returns co-occupants' legs
    # too; restrict the P&L calc to legs the bot's own book implies.
    own_syms = set()
    for p in own:
        exp = p.get("expiry")
        for k in (p.get("short_strike"), p.get("long_strike")):
            if exp and k is not None:
                own_syms.add(occ(exp, k))
    # REAL unrealized straight from the broker: sum(mark*100*qty - cost_basis) over the owned legs.
    # This matches the broker app exactly (avoids the bot's recorded-credit vs actual-fill mismatch).
    unreal = None
    cbmap = {}   # OCC symbol -> broker cost_basis, for the actual-fill entry credit below
    try:
        pr = _get(env, "/accounts/%s/positions" % acct).get("positions")
        blegs = [] if pr in (None, "null") else (pr.get("position") if isinstance(pr, dict) else pr)
        blegs = [blegs] if isinstance(blegs, dict) else (blegs or [])
        legs = [L for L in blegs if L.get("symbol") in own_syms]
        cbmap = {L.get("symbol"): L.get("cost_basis") for L in legs}
        if legs:
            syms = ",".join(L["symbol"] for L in legs)
            qq = _get(env, "/markets/quotes", symbols=syms).get("quotes", {}).get("quote", [])
            qq = [qq] if isinstance(qq, dict) else qq
            mark = {x["symbol"]: ((x.get("bid") or 0) + (x.get("ask") or 0)) / 2 for x in qq}
            u = 0.0
            for L in legs:
                m = mark.get(L["symbol"])
                if m is None:
                    raise ValueError("missing quote for " + str(L.get("symbol")))
                u += m * 100 * float(L["quantity"]) - float(L.get("cost_basis") or 0)
            unreal = round(u, 2)
        elif not own:
            unreal = 0.0
    except Exception:
        traceback.print_exc()
        unreal = None
    out["unrealized"] = unreal
    # positions kept from the bot's own state book (clean spread structure + count for the dashboard).
    # broker_credit = actual net entry fill from the leg cost bases; it reconciles with mark+unrealized,
    # unlike the bot's RECORDED credit when live-fill accounting is off (Bot C).
    plist = []
    for p in own:
        exp, s, l, q = p.get("expiry"), p.get("short_strike"), p.get("long_strike"), p.get("qty")
        bc = None
        if exp is not None and s is not None and l is not None and q:
            cbs, cbl = cbmap.get(occ(exp, s)), cbmap.get(occ(exp, l))
            if cbs is not None and cbl is not None:
                bc = round(-(float(cbs) + float(cbl)) / 100.0 / q, 2)
        cr = p.get("credit")
        plist.append({"short": s, "long": l, "qty": q, "credit": cr, "broker_credit": bc,
                      "credit_mismatch": bool(bc is not None and cr is not None and abs(bc - cr) >= 0.05),
                      "expiry": exp})
    out["positions"] = plist
    out["realized_today"] = st.get("realized_today")
    return out


def run_once():
    for bot in BOTS:
        try:
            data = pull_bot(bot)
            tmp = REPORTS + "/live_%s.json.tmp" % bot["key"]
            with open(tmp, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, REPORTS + "/live_%s.json" % bot["key"])
        except Exception:
            traceback.print_exc()
    # rebuild the dashboard (picks up the live_*.json overlay) and refresh the served copy
    subprocess.run([BASE + "/venv/bin/python", BASE + "/dashboard/build_dashboard.py"], check=False)
    if os.path.isdir(os.path.dirname(SERVED)):
        subprocess.run(["cp", REPORTS + "/dashboard.html", SERVED], check=False)


if __name__ == "__main__":
    while True:
        try:
            run_once()
        except Exception:
            traceback.print_exc()
        time.sleep(INTERVAL)
