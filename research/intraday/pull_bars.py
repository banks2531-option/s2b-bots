"""Stage 1 bar puller — per-ticker full-range 1-min equity bars from Polygon.

Universe = top-80 liquid-core tickers by S1 (aggressive sweep, prem>=500k) + S2
(floor/dark-pool) candidate-event count. Free-tier rate limit = 5 req/min, so we
sleep 13s between calls and back off on 429. Paginated via next_url. Cached per
ticker to research/intraday/bars/<TICKER>.parquet so re-runs are free.

Pre-registered in research/2026-06-14-intraday-flow-edge-prereg.md.
"""
import os, time, json, sys
import requests
import pandas as pd

KEY = os.environ.get("POLYGON_API_KEY", "")   # free-tier Polygon (read-only market data)
if not KEY:
    raise SystemExit("Set POLYGON_API_KEY env var before running (see C8: no hardcoded credentials).")
HERE = os.path.dirname(os.path.abspath(__file__))
BARS_DIR = os.path.join(HERE, "bars")
os.makedirs(BARS_DIR, exist_ok=True)
ALERTS = os.path.join(HERE, "..", "alerts_panel.parquet")
PRICES = os.path.join(HERE, "..", "prices.parquet")
FROM, TO = "2026-03-04", "2026-06-11"
SLEEP = 13.0          # 5 req/min -> 12s; 13s for safety margin
N_CORE = 80

def liquid_core():
    df = pd.read_parquet(ALERTS)
    liquid = set(pd.read_parquet(PRICES).columns)
    base = df[(df.is_index == False) & (df.dir != 0) & (df.ticker.isin(liquid))]
    s1 = base[(base.has_sweep == True) & (base.side == "ask") & (base.total_premium >= 500000)]
    s2 = base[base.has_floor == True]
    counts = pd.concat([s1, s2]).ticker.value_counts()
    core = list(counts.head(N_CORE).index)
    json.dump(core, open(os.path.join(HERE, "liquid_core.json"), "w"), indent=0)
    return core

def get(url, params=None):
    """GET with 429 backoff."""
    for attempt in range(6):
        r = requests.get(url, params=params, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            print(f"    429, backoff 60s (attempt {attempt+1})", flush=True)
            time.sleep(60)
            continue
        print(f"    HTTP {r.status_code}: {r.text[:120]}", flush=True)
        return None
    return None

def pull_ticker(tk):
    out = os.path.join(BARS_DIR, f"{tk}.parquet")
    if os.path.exists(out):
        print(f"  {tk}: cached, skip", flush=True)
        return "cached"
    url = f"https://api.polygon.io/v2/aggs/ticker/{tk}/range/1/minute/{FROM}/{TO}"
    params = {"apiKey": KEY, "limit": 50000, "sort": "asc", "adjusted": "true"}
    rows, page = [], 0
    while True:
        page += 1
        j = get(url, params)
        time.sleep(SLEEP)
        if not j:
            print(f"  {tk}: FAILED on page {page}", flush=True)
            return "failed"
        rows.extend(j.get("results", []))
        nxt = j.get("next_url")
        if not nxt:
            break
        url, params = nxt + f"&apiKey={KEY}", None
    if not rows:
        print(f"  {tk}: 0 bars", flush=True)
        return "empty"
    df = pd.DataFrame(rows)[["t", "o", "h", "l", "c", "v"]]
    df["ts_et"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("America/New_York")
    df = df.drop_duplicates("t").sort_values("t").reset_index(drop=True)
    df.to_parquet(out)
    print(f"  {tk}: {len(df)} bars, {page} page(s) -> {out}", flush=True)
    return "ok"

def main():
    core = liquid_core()
    print(f"liquid core ({len(core)}): {core}", flush=True)
    t0 = time.time()
    stats = {}
    for i, tk in enumerate(core, 1):
        print(f"[{i}/{len(core)}] {tk}  (elapsed {(time.time()-t0)/60:.1f}m)", flush=True)
        try:
            res = pull_ticker(tk)
        except Exception as e:
            res = "exc"
            print(f"  {tk}: EXC {e}", flush=True)
        stats[res] = stats.get(res, 0) + 1
    print(f"DONE in {(time.time()-t0)/60:.1f}m. stats={stats}. bars dir: {BARS_DIR}", flush=True)

if __name__ == "__main__":
    main()
