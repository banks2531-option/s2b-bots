"""Phase B replay runner: re-feed Phase-A capture records to the bot's REAL `run_entry_cycle` and
compare the reproduced decision to the captured one (the design's baseline-calibration gate).
Offline, deterministic, no broker (see replay_deps). Design:
docs/superpowers/specs/2026-07-23-replay-harness-design.md.

The reproduced decision is read back through the SAME capture mechanism (replay_capture forced on
with an in-memory sink), so "captured decision" and "replayed decision" are the identical field
produced by the identical code path -- an apples-to-apples comparison.
"""
import json
from dataclasses import replace
from datetime import datetime

from bot.app.orchestrator import BotState, run_entry_cycle
from replays.replay_deps import build_replay_deps, chain_from_snapshot


def load_capture(path):
    """Read a Phase-A capture JSONL into a list of records (skips blank/corrupt lines, never raises
    on a single bad line -- a truncated trailing write must not sink the whole replay)."""
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def carry_forward_chains(records):
    """The full chain is snapshotted once per (expiry,15-min bucket); later same-bucket records omit
    it. Attach the applicable snapshot to every record by carrying the last-seen chain per expiry."""
    last_by_expiry = {}
    out = []
    for r in records:
        if r.get("chain") is not None:
            last_by_expiry[r.get("expiry")] = r["chain"]
        out.append((r, last_by_expiry.get(r.get("expiry"))))
    return out


def replay_record(record, chain_snapshot, *, equity, features, base_risk_pct, s2b_cfg=None,
                  entry_days=frozenset(range(5)), max_open=1, max_entries_per_day=1,
                  state=None, allow_fills=True):
    """Replay ONE captured decision through the real run_entry_cycle. Returns a dict with the
    reproduced decision, the simulated orders, and whether it matches the captured decision."""
    chain = chain_from_snapshot(chain_snapshot)
    orders = []
    sink = []
    # Force replay_capture on (with an in-memory sink) so we read the reproduced decision from the
    # exact same field the original capture wrote -- regardless of the source bot's flag settings.
    feats = replace(features, replay_capture=True)
    deps = build_replay_deps(chain=chain, spot=record.get("spot"), atr=record.get("atr"),
                             expiry=record.get("expiry"), equity=equity, features=feats,
                             base_risk_pct=base_risk_pct, s2b_cfg=s2b_cfg, entry_days=entry_days,
                             max_open=max_open, max_entries_per_day=max_entries_per_day,
                             orders=orders, allow_fills=allow_fills)
    deps.replay_log = sink.append
    st = state if state is not None else BotState()
    st, info = run_entry_cycle(st, deps, datetime.fromisoformat(record["ts_et"]))
    replayed = sink[-1]["decision"] if sink else None
    return {
        "ts_et": record.get("ts_et"),
        "captured_decision": record.get("decision"),
        "replayed_decision": replayed,
        "matched": replayed == record.get("decision"),
        "orders": orders,
        "state": st,
        "info": info,
    }


def replay_capture_file(path, *, equity, features, base_risk_pct, **kw):
    """Replay an entire capture file INDEPENDENTLY per record (fresh state each) and summarize the
    baseline match rate. Independent replay answers 'given this instant, does the real decision
    logic reproduce the logged decision?' -- the design's calibration test. (Stateful whole-day
    replay, where entries_today/open_positions accumulate, is a later refinement that also needs the
    day-open state package.)"""
    results = [replay_record(rec, snap, equity=equity, features=features,
                             base_risk_pct=base_risk_pct, **kw)
               for rec, snap in carry_forward_chains(load_capture(path))]
    matched = sum(1 for r in results if r["matched"])
    return {"total": len(results), "matched": matched,
            "match_rate": (matched / len(results) if results else None),
            "mismatches": [r for r in results if not r["matched"]],
            "results": results}
