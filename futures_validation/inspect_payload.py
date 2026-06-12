"""Read-only: enumerate futures key families, date ranges, and payload format."""
import sqlite3, json
from collections import defaultdict

DB = r"E:\BanksBackup\trading-bot\backtests\cache\api_cache.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
cur = con.cursor()

keys = [k for (k,) in cur.execute("SELECT key FROM cache WHERE key LIKE 'futures:%'")]
fams = defaultdict(list)
for k in keys:
    body = k.split(":", 1)[1]
    parts = body.rsplit("_", 1)
    fams[parts[0]].append(parts[1] if len(parts) > 1 else "")
print("FAMILIES:")
for f, dates in sorted(fams.items()):
    ds = sorted(dates)
    print(f"  {f}: n={len(ds)} range {ds[0]} .. {ds[-1]}")

# Inspect one payload per family
for f, dates in sorted(fams.items()):
    k = f"futures:{f}_{sorted(dates)[5] if len(dates)>5 else sorted(dates)[0]}"
    v = cur.execute("SELECT value FROM cache WHERE key=?", (k,)).fetchone()[0]
    print(f"\n== {k}  payload_len={len(v)}")
    try:
        d = json.loads(v)
        if isinstance(d, list):
            print("  list len", len(d), "first:", json.dumps(d[0])[:300])
            print("  last:", json.dumps(d[-1])[:300])
        elif isinstance(d, dict):
            print("  dict keys:", list(d.keys())[:20])
            for kk in list(d.keys())[:2]:
                print("  ", kk, "->", str(d[kk])[:300])
    except Exception as e:
        print("  not json:", v[:200], e)

# Also check whether NQ/MES exist under other prefixes
for pat in ("%nq%", "%mes%"):
    rows = cur.execute("SELECT key FROM cache WHERE key LIKE ? AND key NOT LIKE 'polygon%' AND key NOT LIKE 'tradier%' LIMIT 20", (pat,)).fetchall()
    print(f"\nnon-polygon keys matching {pat}:", [r[0][:80] for r in rows])
