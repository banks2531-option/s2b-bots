"""Read-only inspection of the legacy api_cache.db for futures bar data."""
import sqlite3, json, sys

DB = r"E:\BanksBackup\trading-bot\backtests\cache\api_cache.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
cur = con.cursor()

tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("TABLES:", tables)
for t in tables:
    cols = cur.execute(f"PRAGMA table_info({t})").fetchall()
    n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"\n== {t} rows={n}")
    print("   cols:", [(c[1], c[2]) for c in cols])
