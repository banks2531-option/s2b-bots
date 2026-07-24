"""Phase B replay-harness tests: prove the runner reproduces captured decisions through the bot's
REAL run_entry_cycle, deterministically, and can never reach a broker."""
import json
from datetime import datetime

import pytest

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.features import S2bFeatures
from bot.strategy.s2b import OptionQuote, S2bConfig
from bot.risk_gate import AccountState

from bot.app.replay_capture import snapshot_chain
from bot.strategy.manage import ManagedPosition
from bot.features import S2bFeatures as _Feat
from replays.replay_deps import (build_replay_deps, chain_from_snapshot, ReplayOrderAttempt,
                                  snapshot_iv_resolver)
from replays.replay_runner import (replay_record, replay_capture_file, load_capture,
                                    carry_forward_chains, replay_day)


# ---- shared scenario: the same one the orchestrator/capture tests use (opens short 568 on Monday) ----
MONDAY = datetime(2026, 6, 15, 10, 5)


def _chain():
    return [OptionQuote(572.0, 0.45, 4.50, 4.60), OptionQuote(568.0, 0.36, 3.40, 3.50),
            OptionQuote(565.0, 0.30, 2.80, 2.90), OptionQuote(560.0, 0.22, 2.00, 2.10),
            OptionQuote(558.0, 0.18, 1.60, 1.70)]


def _acct(today, conc):
    return AccountState(20_000.0, 20_000.0, 0.0, conc, 0.0, {}, today)


def _capture_one_record(now=MONDAY):
    """Run the REAL entry cycle once with capture on, and return the single REPLAY record it wrote.
    This is genuine Phase-A output -- the replay then has to reproduce it."""
    sink = []
    d = Deps(
        get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
        get_chain=lambda s, e: _chain(), pick_expiry=lambda t: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=_acct,
        mark_position=lambda p: 3.0, dte_of=lambda p, t: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 20_000.0,
        bot_equity=lambda: 20_000.0, alert_sink=lambda a: None, s2b_cfg=S2bConfig(),
        features=S2bFeatures(replay_capture=True), replay_log=sink.append,
        entry_days=frozenset({0}),
    )
    run_entry_cycle(BotState(), d, now)
    assert sink, "expected a capture record"
    return sink[-1]


# ---------------- helpers ----------------

def test_chain_from_snapshot_round_trips():
    snap = snapshot_chain(_chain())
    rebuilt = chain_from_snapshot(snap)
    assert [q.strike for q in rebuilt] == [558.0, 560.0, 565.0, 568.0, 572.0]
    q = next(q for q in rebuilt if q.strike == 568.0)
    assert (q.delta, q.bid, q.ask) == (0.36, 3.40, 3.50)


# ---------------- safety: never reach a broker ----------------

def test_close_spread_always_raises():
    deps = build_replay_deps(chain=_chain(), spot=575.0, atr=6.0, expiry="2026-06-19",
                             equity=20_000.0, features=S2bFeatures(), base_risk_pct=0.10)
    with pytest.raises(ReplayOrderAttempt):
        deps.close_spread(None, None)


def test_allow_fills_false_makes_an_entry_attempt_raise_loudly():
    rec = _capture_one_record()   # a scenario that DOES enter
    with pytest.raises(ReplayOrderAttempt):
        replay_record(rec, rec["chain"], equity=20_000.0, features=S2bFeatures(),
                      base_risk_pct=0.10, entry_days=frozenset({0}), allow_fills=False)


# ---------------- baseline reproduction ----------------

def test_replay_reproduces_the_captured_decision():
    rec = _capture_one_record()
    assert rec["decision"] == "filled"                       # the scenario opens a position
    out = replay_record(rec, rec["chain"], equity=20_000.0, features=S2bFeatures(),
                        base_risk_pct=0.10, entry_days=frozenset({0}))
    assert out["matched"] is True
    assert out["replayed_decision"] == "filled"
    # and it reproduced the SAME order as the captured candidate (strikes + sized qty) via the real
    # logic, no broker -- a true round-trip: capture -> replay -> identical decision.
    assert len(out["orders"]) == 1
    o = out["orders"][0]
    cand = rec["candidate"]
    assert o["quantity[0]"] == int(round(float(cand["qty"])))
    assert o["option_symbol[0]"].endswith("P%08d" % int(round(float(cand["short"]) * 1000)))


def test_replay_is_deterministic():
    rec = _capture_one_record()
    a = replay_record(rec, rec["chain"], equity=20_000.0, features=S2bFeatures(),
                      base_risk_pct=0.10, entry_days=frozenset({0}))
    b = replay_record(rec, rec["chain"], equity=20_000.0, features=S2bFeatures(),
                      base_risk_pct=0.10, entry_days=frozenset({0}))
    assert a["replayed_decision"] == b["replayed_decision"]
    assert a["orders"] == b["orders"]


def test_a_reject_scenario_reproduces_as_the_same_reject():
    # Tuesday is not an entry day for this deps -> the bot rejects with "wrong_weekday".
    rec = _capture_one_record(now=datetime(2026, 6, 16, 10, 5))
    assert rec["decision"] == "wrong_weekday"
    out = replay_record(rec, rec.get("chain"), equity=20_000.0, features=S2bFeatures(),
                        base_risk_pct=0.10, entry_days=frozenset({0}))
    assert out["matched"] is True and out["replayed_decision"] == "wrong_weekday"
    assert out["orders"] == []                               # nothing placed


# ---------------- file level: carry-forward + summary ----------------

# ---------------- stateful whole-day replay ----------------

def test_snapshot_iv_resolver_maps_strike_to_iv():
    snap = [{"strike": 568.0, "iv": 0.21}, {"strike": 558.0, "iv": 0.24}, {"strike": 560.0, "iv": None}]
    fn = snapshot_iv_resolver(snap, "2026-06-19")
    from bot.strategy.s2b import _occ
    sym = _occ("SPY", "2026-06-19", "P", 568.0)
    assert fn([sym]) == {sym: 0.21}
    assert snapshot_iv_resolver([{"strike": 1, "iv": None}], "2026-06-19") is None   # no usable IV


def test_replay_day_threads_state_and_opens_position():
    rec = _capture_one_record()                       # a filled 568/558 entry
    out = replay_day([rec], equity=20_000.0, features=_Feat(), base_risk_pct=0.10,
                     entry_days=frozenset({0}), max_open=5)
    assert out["total"] == 1 and out["matched"] == 1
    # the fill THREADED into state -> the position is now on the book
    assert len(out["final_positions"]) == 1
    assert out["final_positions"][0][0] == 568.0     # short strike


def test_replay_day_book_dependent_gate_responds_to_seed():
    # THE stateful proof: the identical record that FILLS from a flat book must be BLOCKED when the
    # book is seeded to the position cap. Independent replay can't show this; stateful replay can.
    rec = _capture_one_record()
    flat = replay_day([rec], equity=20_000.0, features=_Feat(), base_risk_pct=0.10,
                      entry_days=frozenset({0}), max_open=1)
    assert flat["results"][0]["replayed_decision"] == "filled"
    seeded = replay_day([rec], equity=20_000.0, features=_Feat(), base_risk_pct=0.10,
                        entry_days=frozenset({0}), max_open=1,
                        day_open_positions=[ManagedPosition("SPY", 560.0, 550.0, credit=1.0, qty=1,
                                                            expiry="2026-06-19")])
    assert seeded["results"][0]["replayed_decision"] == "max_open"   # the book changed the decision
    assert seeded["matched"] is False or seeded["results"][0]["matched"] is False


def test_replay_day_is_deterministic():
    rec = _capture_one_record()
    a = replay_day([rec], equity=20_000.0, features=_Feat(), base_risk_pct=0.10, entry_days=frozenset({0}))
    b = replay_day([rec], equity=20_000.0, features=_Feat(), base_risk_pct=0.10, entry_days=frozenset({0}))
    assert a["results"] == b["results"] and a["final_positions"] == b["final_positions"]


def test_replay_capture_file_carries_chain_forward(tmp_path):
    rec = _capture_one_record()
    later = dict(rec); later.pop("chain", None); later["ts_et"] = "2026-06-15T10:07:00"  # same bucket
    p = tmp_path / "cap.jsonl"
    p.write_text(json.dumps(rec) + "\n" + json.dumps(later) + "\n")
    # both records should carry the same chain (second one inherits the first's snapshot)
    pairs = carry_forward_chains(load_capture(str(p)))
    assert pairs[1][1] is not None and pairs[1][1] == rec["chain"]
    summary = replay_capture_file(str(p), equity=20_000.0, features=S2bFeatures(),
                                  base_risk_pct=0.10, entry_days=frozenset({0}))
    assert summary["total"] == 2 and summary["matched"] == 2 and summary["match_rate"] == 1.0
