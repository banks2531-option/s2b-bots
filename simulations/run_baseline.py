"""Restart wrapper for the legacy baseline replay.

Fixes vs the bare CLI run that died ~2025-04-14:
- bounds the PolygonClient/TradierClient in-memory caches (the disk overlay
  cache already persists everything; the in-process dict grew unboundedly and
  the original run died silently around 1.5GB+ RSS)
- paces Polygon at 0.30s/call (the bare run's 0.15s tripped constant 12s
  429 penalties)
"""
import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


class BoundedCache(dict):
    def __init__(self, maxn=20000):
        super().__init__()
        self._order = collections.deque()
        self._maxn = maxn

    def __setitem__(self, key, value):
        if key not in self:
            self._order.append(key)
        super().__setitem__(key, value)
        while len(self._order) > self._maxn:
            super().pop(self._order.popleft(), None)


import engine_v345 as eng

_poly_init = eng.PolygonClient.__init__
_trad_init = eng.TradierClient.__init__


def poly_init(self, *a, **k):
    _poly_init(self, *a, **k)
    self.cache = BoundedCache()
    self.min_interval = 0.30


def trad_init(self, *a, **k):
    _trad_init(self, *a, **k)
    self.cache = BoundedCache()


eng.PolygonClient.__init__ = poly_init
eng.TradierClient.__init__ = trad_init

sys.argv = ['engine_v345.py', '-d',
            os.path.join(HERE, 'whalestream_flow_2025-03_2026-02.csv'),
            '--commissions']
eng.main()
