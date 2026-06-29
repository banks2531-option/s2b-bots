"""Bound a callable that can HANG (not just error). yfinance has no network timeout of its own, so
a TCP stall there would otherwise block the single-threaded tick loop indefinitely. Running it in a
daemon thread and joining with a timeout lets us abandon a stuck call and return a default instead."""
import threading


def run_with_timeout(fn, timeout_s, default):
    """Run fn() in a daemon thread; return its result, or `default` if it exceeds timeout_s or raises.
    A timed-out thread is abandoned (daemon -> dies at process exit); callers keep this rare via caching."""
    box = {"v": default}

    def run():
        try:
            box["v"] = fn()
        except Exception:
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout_s)
    return box["v"]
