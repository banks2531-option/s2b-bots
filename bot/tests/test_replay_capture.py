from datetime import datetime

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.app.replay_capture import snapshot_chain, build_replay_record
from bot.strategy.s2b import OptionQuote, S2bConfig
from bot.strategy.manage import ManagedPosition
from bot.features import S2bFeatures
from bot.risk_gate import AccountState


# ---------------- pure helpers ----------------

def test_snapshot_chain_captures_leg_fields_sorted():
    chain = [OptionQuote(568.0, 0.36, 3.40, 3.50), OptionQuote(560.0, 0.22, 2.00, 2.10)]
    snap = snapshot_chain(chain)
    assert [r["strike"] for r in snap] == [560.0, 568.0]           # sorted by strike
    assert snap[1] == {"strike": 568.0, "delta": 0.36, "bid": 3.40, "ask": 3.50,
                       "iv": None, "exch_ts": None, "recv_ts": None}


def test_snapshot_chain_empty_is_empty_list():
    assert snapshot_chain(None) == []
    assert snapshot_chain([]) == []


def test_build_replay_record_omits_none_and_includes_chain():
    rec = build_replay_record(ts_et=datetime(2026, 7, 23, 10, 5), bot="c", decision="filled",
                              spot=738.0, expiry="2026-07-31",
                              recorded_credit=0.74, actual_fill_credit=1.21,
                              chain_snapshot=[{"strike": 729.0}])
    assert rec["event"] == "REPLAY" and rec["bot"] == "c" and rec["decision"] == "filled"
    assert rec["ts_et"] == "2026-07-23T10:05:00" and rec["spot"] == 738.0
    assert rec["recorded_credit"] == 0.74 and rec["actual_fill_credit"] == 1.21
    assert rec["chain"] == [{"strike": 729.0}]
    assert "atr" not in rec and "regime" not in rec           # None fields omitted


# ---------------- orchestrator integration ----------------

def _chain():
    return [OptionQuote(572.0, 0.45, 4.50, 4.60), OptionQuote(568.0, 0.36, 3.40, 3.50),
            OptionQuote(565.0, 0.30, 2.80, 2.90), OptionQuote(560.0, 0.22, 2.00, 2.10),
            OptionQuote(558.0, 0.18, 1.60, 1.70)]


def _acct(today, conc):
    return AccountState(20_000.0, 20_000.0, 0.0, conc, 0.0, {}, today)


def _deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _chain(), pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=_acct,
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 20_000.0,
        bot_equity=lambda: 20_000.0, alert_sink=lambda alerts: None,
        s2b_cfg=S2bConfig(),
    )
    base.update(over)
    return Deps(**base)


def test_capture_off_by_default_never_calls_replay_log():
    calls = []
    d = _deps(replay_log=lambda rec: calls.append(rec))   # flag default off
    run_entry_cycle(BotState(), d, datetime(2026, 6, 15, 10, 5))   # Monday RTH
    assert calls == []


def test_capture_on_writes_a_record_with_chain():
    calls = []
    d = _deps(features=S2bFeatures(replay_capture=True), replay_log=lambda rec: calls.append(rec))
    run_entry_cycle(BotState(), d, datetime(2026, 6, 15, 10, 5))
    assert calls, "expected at least one replay record"
    rec = calls[-1]
    assert rec["event"] == "REPLAY" and rec["spot"] == 575.0 and rec["expiry"] == "2026-06-19"
    assert rec["chain"] and rec["chain"][0]["strike"] == 558.0     # full chain captured, sorted


def test_capture_never_raises_into_the_trading_loop():
    def boom(rec):
        raise RuntimeError("sink down")
    d = _deps(features=S2bFeatures(replay_capture=True), replay_log=boom)
    # a failing sink must not propagate -- the entry cycle still returns normally
    state, info = run_entry_cycle(BotState(), d, datetime(2026, 6, 15, 10, 5))
    assert isinstance(state, BotState)


def test_chain_captured_once_per_expiry_bucket_then_omitted():
    calls = []
    d = _deps(features=S2bFeatures(replay_capture=True), replay_log=lambda rec: calls.append(rec))
    state = BotState()
    now = datetime(2026, 6, 15, 10, 5)
    state, _ = run_entry_cycle(state, d, now)            # first look in this 15-min bucket
    state.last_entry_date = ""                            # allow another entry cycle same day
    state.entries_today = 0
    state, _ = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, 7))   # same bucket, 2 min later
    with_chain = [c for c in calls if c.get("chain")]
    without_chain = [c for c in calls if "chain" not in c]
    assert len(with_chain) == 1, "chain snapshot should be captured once per (expiry,15-min bucket)"
    assert without_chain, "later same-bucket decisions should record without re-snapshotting the chain"


# ---------------- multi-expiry capture (2026-07-28) ----------------

def test_book_chains_captures_held_position_expiries():
    """The open book holds a DIFFERENT expiry than the candidate -> the capture must snapshot that
    held expiry's chain too (book_chains), fetched via get_chain, so the stateful replay can price it."""
    fetched = []
    def get_chain(sym, exp):
        fetched.append(exp)
        if exp == "2026-07-10":
            return [OptionQuote(700.0, 0.30, 2.0, 2.1, iv=0.22)]
        return _chain()          # candidate expiry -> a normal chain
    calls = []
    held = ManagedPosition("SPY", 700.0, 690.0, credit=1.0, qty=1, expiry="2026-07-10")  # != candidate 6/19
    d = _deps(get_chain=get_chain, max_open=5, features=S2bFeatures(replay_capture=True),
              replay_log=lambda rec: calls.append(rec))
    run_entry_cycle(BotState(open_positions=[held]), d, datetime(2026, 6, 15, 10, 5))
    rec = calls[-1]
    assert "book_chains" in rec, "held-position expiry chain must be captured"
    assert "2026-07-10" in rec["book_chains"]
    assert rec["book_chains"]["2026-07-10"][0]["strike"] == 700.0
    assert "2026-07-10" in fetched     # it fetched the held expiry's chain


def test_book_chains_absent_when_book_shares_candidate_expiry():
    """When the candidate expiry is known and a held position shares it, book_chains must NOT
    redundantly re-capture that expiry (it's already in `chain`). max_open high so the candidate path
    runs and the candidate expiry (6/19) is known."""
    calls = []
    same = ManagedPosition("SPY", 560.0, 550.0, credit=1.0, qty=1, expiry="2026-06-19")  # == candidate
    d = _deps(max_open=5, features=S2bFeatures(replay_capture=True),
              replay_log=lambda rec: calls.append(rec))
    run_entry_cycle(BotState(open_positions=[same]), d, datetime(2026, 6, 15, 10, 5))
    rec = calls[-1]
    assert rec.get("expiry") == "2026-06-19" and rec.get("chain")   # candidate expiry captured in `chain`
    assert "book_chains" not in rec                                 # not duplicated in book_chains


def test_held_expiry_chain_fetch_failure_never_breaks_capture():
    """A get_chain failure for a held expiry must be swallowed -- capture (and trading) continue."""
    def get_chain(sym, exp):
        if exp == "2026-07-10":
            raise RuntimeError("chain feed down")
        return _chain()
    held = ManagedPosition("SPY", 700.0, 690.0, credit=1.0, qty=1, expiry="2026-07-10")
    calls = []
    d = _deps(get_chain=get_chain, features=S2bFeatures(replay_capture=True),
              replay_log=lambda rec: calls.append(rec))
    state, info = run_entry_cycle(BotState(open_positions=[held]), d, datetime(2026, 6, 15, 10, 5))
    assert isinstance(state, BotState)          # did not raise
    assert calls and "book_chains" not in calls[-1]   # the failed held expiry simply isn't captured
