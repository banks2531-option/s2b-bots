import time
from bot.regime.timeoututil import run_with_timeout


def test_returns_value_when_fast():
    assert run_with_timeout(lambda: 42, timeout_s=1.0, default=-1) == 42


def test_returns_default_on_hang_without_waiting():
    def slow():
        time.sleep(2.0)
        return 99
    t0 = time.time()
    assert run_with_timeout(slow, timeout_s=0.1, default=-1) == -1
    assert time.time() - t0 < 1.0          # returned promptly, did not block on the hang


def test_returns_default_on_error():
    def boom():
        raise ValueError("x")
    assert run_with_timeout(boom, timeout_s=1.0, default="dflt") == "dflt"
