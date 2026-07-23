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
    plist, unreal, priced = [], 0.0, False
    for p in (st.get("open_positions") or []):
        s, l, q, cr, exp = (p.get("short_strike"), p.get("long_strike"), p.get("qty"),
                            p.get("credit"), p.get("expiry"))
        mark = None
        try:
            qq = _get(env, "/markets/quotes", symbols=occ(exp, s) + "," + occ(exp, l)) \
                .get("quotes", {}).get("quote", [])
            qq = [qq] if isinstance(qq, dict) else qq
            mm = {x["symbol"]: ((x.get("bid") or 0) + (x.get("ask") or 0)) / 2 for x in qq}
            mark = round(mm.get(occ(exp, s), 0) - mm.get(occ(exp, l), 0), 2)
        except Exception:
            mark = None
        pnl = round((cr - mark) * 100 * q, 2) if mark is not None else None
        if pnl is not None:
            unreal += pnl
            priced = True
        plist.append({"short": s, "long": l, "qty": q, "credit": cr, "mark": mark,
                      "unrealized": pnl, "expiry": exp})
    out["positions"] = plist
    out["unrealized"] = round(unreal, 2) if priced or not plist else None
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
