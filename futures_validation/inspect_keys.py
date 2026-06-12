"""Read-only: profile cache key namespaces and find futures-related entries."""
import sqlite3, json
from collections import Counter

DB = r"E:\BanksBackup\trading-bot\backtests\cache\api_cache.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
cur = con.cursor()

# Key prefix profile
c = Counter()
for (k,) in cur.execute("SELECT key FROM cache"):
    c[k.split(":")[0] if ":" in k else k.split("|")[0][:40]] += 1
print("TOP PREFIXES:")
for p, n in c.most_common(40):
    print(f"  {n:8d}  {p}")

# Futures-related keys
print("\nFUTURES-LIKE KEYS (sample):")
q = """SELECT key FROM cache WHERE key LIKE '%MES%' OR key LIKE '%databento%'
       OR key LIKE '%futures%' OR key LIKE '%/ES%' OR key LIKE '%NQ%'
       OR key LIKE '%dbn%' OR key LIKE '%GLBX%' LIMIT 200"""
rows = cur.execute(q).fetchall()
print(f"  matched (first 200): {len(rows)}")
for (k,) in rows[:60]:
    print("  ", k[:160])
n_all = cur.execute("""SELECT COUNT(*) FROM cache WHERE key LIKE '%MES%' OR key LIKE '%databento%'
       OR key LIKE '%futures%' OR key LIKE '%/ES%' OR key LIKE '%NQ%' OR key LIKE '%GLBX%'""").fetchone()[0]
print("  total matched:", n_all)
