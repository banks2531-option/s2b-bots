"""Decision-input capture for the deterministic replay harness (design:
docs/superpowers/specs/2026-07-23-replay-harness-design.md, Phase A).

PURE serialization helpers only -- no I/O, no state mutation, never raises on well-formed input.
The orchestrator calls these behind the OFF-by-default ``Features.replay_capture`` flag, inside a
``try/except`` so nothing here can ever break the trading loop. The record they build is what an
offline replay (Phase B) re-feeds to the bot's real ``run_entry_cycle`` to reproduce a decision.
"""


def _iso(ts):
    """A datetime -> ISO string; anything else (None, already-a-string) passes through unchanged."""
    try:
        return ts.isoformat()
    except AttributeError:
        return ts


def snapshot_chain(chain):
    """[OptionQuote] -> [dict] capturing exactly what the bot saw per strike: strike/delta/bid/ask/iv
    plus whatever quote timestamps the feed carried. Sorted by strike for stable, diffable output.
    Returns [] for a falsy chain. Best-effort per leg -- a malformed quote is skipped, never fatal."""
    out = []
    for q in (chain or []):
        try:
            out.append({
                "strike": q.strike, "delta": q.delta, "bid": q.bid, "ask": q.ask,
                "iv": getattr(q, "iv", None),
                "exch_ts": _iso(getattr(q, "exchange_timestamp", None)),
                "recv_ts": _iso(getattr(q, "received_timestamp", None)),
            })
        except Exception:
            continue
    out.sort(key=lambda r: (r["strike"] is None, r["strike"]))
    return out


def build_replay_record(*, ts_et, bot, decision, limiting_gate=None, spot=None, atr=None,
                        expiry=None, regime=None, order=None, recorded_credit=None,
                        actual_fill_credit=None, deployed_commit=None, config_hash=None,
                        chain_snapshot=None, book_chains=None):
    """Assemble one replay-capture record. ``chain_snapshot`` is the candidate expiry's chain (from
    snapshot_chain) when this (expiry, 15-min bucket) has not been captured yet, else None (the
    replayer reuses the bucket's earlier snapshot). ``book_chains`` is {expiry: snapshot} for the OPEN
    BOOK's other expiries captured this cycle -- so the stateful replay can price the whole gap-stress
    book with real per-leg IV, not flat fallback IV. Only non-None fields are emitted so the JSONL
    stays compact and self-describing."""
    rec = {"event": "REPLAY", "ts_et": _iso(ts_et), "bot": bot, "decision": decision}
    if limiting_gate is not None:
        rec["limiting_gate"] = limiting_gate
    if spot is not None:
        rec["spot"] = spot
    if atr is not None:
        rec["atr"] = atr
    if expiry is not None:
        rec["expiry"] = expiry
    if regime is not None:
        rec["regime"] = regime
    if order is not None:
        rec["candidate"] = {
            "short": getattr(order, "short_strike", None), "long": getattr(order, "long_strike", None),
            "qty": getattr(order, "qty", None), "credit": getattr(order, "credit", None),
        }
    if recorded_credit is not None:
        rec["recorded_credit"] = recorded_credit
    if actual_fill_credit is not None:
        rec["actual_fill_credit"] = actual_fill_credit
    if deployed_commit is not None:
        rec["deployed_commit"] = deployed_commit
    if config_hash is not None:
        rec["config_hash"] = config_hash
    if chain_snapshot is not None:
        rec["chain"] = chain_snapshot
    if book_chains:
        rec["book_chains"] = book_chains
    return rec
