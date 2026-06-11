"""Two-tier disk cache shim (drop-in replacement for backtests/disk_cache.py).

READS: first the original 1.5GB cache on E: (opened READ-ONLY via sqlite URI),
then a local overlay DB in this directory.
WRITES: local overlay only — the E: archive is never modified.
"""
import os
import json
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DB = r'E:\BanksBackup\trading-bot\backtests\cache\api_cache.db'
LOCAL_DB = os.path.join(HERE, os.environ.get('OVERLAY_DB', 'api_cache_overlay.db'))


class DiskCache:
    def __init__(self, db_path=None):
        self.db_path = LOCAL_DB
        # read-only connection to the archived cache
        self.src = sqlite3.connect(
            'file:' + SOURCE_DB.replace('\\', '/') + '?mode=ro', uri=True)
        self.conn = sqlite3.connect(LOCAL_DB)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.conn.commit()
        self._hits = 0
        self._misses = 0
        self._src_hits = 0

    def get(self, key):
        row = self.src.execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
        if row:
            self._hits += 1
            self._src_hits += 1
            return json.loads(row[0])
        row = self.conn.execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
        if row:
            self._hits += 1
            return json.loads(row[0])
        self._misses += 1
        return None

    def put(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO cache (key, value) VALUES (?, ?)",
            (key, json.dumps(value)))
        self.conn.commit()

    def stats(self):
        total = self._hits + self._misses
        rate = (self._hits / total * 100) if total > 0 else 0
        return (f"{self._hits:,} hits ({self._src_hits:,} from archive), "
                f"{self._misses:,} misses ({rate:.0f}% hit rate)")

    def size(self):
        try:
            return os.path.getsize(self.db_path) / (1024 * 1024)
        except OSError:
            return 0

    def close(self):
        self.src.close()
        self.conn.close()


_disk_cache = None


def get_disk_cache():
    global _disk_cache
    if _disk_cache is None:
        _disk_cache = DiskCache()
    return _disk_cache
