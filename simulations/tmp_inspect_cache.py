import sqlite3, json
src = sqlite3.connect('file:E:/BanksBackup/trading-bot/backtests/cache/api_cache.db?mode=ro', uri=True)
row = src.execute("SELECT key, value FROM cache WHERE key LIKE 'polygon:bars_SPY%_5_minute' LIMIT 1").fetchone()
print(row[0])
v = json.loads(row[1])
print(len(v), 'bars')
print(json.dumps(v[:3], indent=1))
# also a QQQ one and count of per-day keys
n = src.execute("SELECT COUNT(*) FROM cache WHERE key LIKE 'polygon:bars_%_5_minute'").fetchone()[0]
print('archive 5min bar keys:', n)
# sample one from overlay batch db
ov = sqlite3.connect(r'C:\Users\FixUser123\Documents\fable options\simulations\api_cache_overlay_batch.db')
row2 = ov.execute("SELECT key, value FROM cache WHERE key LIKE 'polygon:bars_SPY%_5_minute' ORDER BY key DESC LIMIT 1").fetchone()
print(row2[0])
v2 = json.loads(row2[1])
print(len(v2), 'bars'); print(json.dumps(v2[:2], indent=1))
print('overlay 5min option keys:', ov.execute("SELECT COUNT(*) FROM cache WHERE key LIKE 'polygon:bars_%_5_minute'").fetchone()[0])
