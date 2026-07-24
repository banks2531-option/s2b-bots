"""Strategy research on the first real Phase-A capture (Bot B, 2026-07-24) + markouts.

Two parts:
  1. BASELINE VALIDATION — replay the captured decisions through the bot's REAL run_entry_cycle
     (Phase B harness) and report the reproduction rate, honestly separating instant-determined
     decisions from book-dependent ones (stateful whole-day replay is a documented later refinement).
  2. DESCRIPTIVE RESEARCH on the advisor's answerable questions from ONE session:
     - Q F: what is gap_2atr actually gating? (concentration behavior)
     - Q B: adjacent-strike concentration among blocked/filled candidates
     - Q A/D: favorable-to-seller rate by short-strike distance and by entry time (markouts)

Run: python research/replay_20260724_analysis.py <capture.jsonl> <markouts.csv>
All conclusions from ONE session are hypothesis-generating only (advisor: one day cannot justify a
change). Confidence is capped at Low/Speculative accordingly.
"""
import csv
import json
import sys
from collections import Counter, defaultdict

from bot.app.run_s2b_alldays import ALLDAYS_FEATURES
from replays.replay_runner import load_capture, carry_forward_chains, replay_record


def part1_baseline(capture_path):
    recs = load_capture(capture_path)
    pairs = carry_forward_chains(recs)
    # Bot B config at the time: full validation feature set, $72k allocation, 10%/trade base risk.
    equity = ALLDAYS_FEATURES.allocated_equity
    matched = book_dependent = instant = 0
    per_decision = defaultdict(lambda: [0, 0])   # decision -> [matched, total]
    for rec, snap in pairs:
        dec = rec.get("decision")
        # Book-dependent decisions (gap_2atr and other aggregate-risk-budget rejects) cannot reproduce
        # under INDEPENDENT single-record replay: the reject is a function of the accumulated open book,
        # which fresh-state replay does not reconstruct. Count them separately rather than as failures.
        book_dependent_dec = dec is not None and dec.startswith("risk_budget")
        try:
            out = replay_record(rec, snap, equity=equity, features=ALLDAYS_FEATURES,
                                base_risk_pct=0.10, entry_days=frozenset(range(5)))
            ok = out["matched"]
        except Exception:
            ok = False
        per_decision[dec][1] += 1
        if ok:
            per_decision[dec][0] += 1
            matched += 1
        if book_dependent_dec:
            book_dependent += 1
        else:
            instant += 1
    print("=" * 70)
    print("PART 1 — BASELINE VALIDATION (replay vs captured decision)")
    print("=" * 70)
    print(f"records replayed: {len(recs)}")
    print(f"overall exact-match: {matched}/{len(recs)} = {matched/len(recs):.1%}")
    print(f"  instant-determined records: {instant} | book-dependent (risk_budget*): {book_dependent}")
    print("\nper-decision reproduction (matched/total):")
    for dec, (m, t) in sorted(per_decision.items(), key=lambda x: -x[1][1]):
        tag = "  [book-dependent: independent replay cannot reconstruct the open book]" \
            if dec and dec.startswith("risk_budget") else ""
        print(f"  {m:3d}/{t:<3d}  {dec}{tag}")
    return recs


def part2_gap2atr(recs):
    print("\n" + "=" * 70)
    print("PART 2A — Q F: what is gap_2atr gating? (concentration behavior)")
    print("=" * 70)
    gap = [r for r in recs if r.get("decision") == "risk_budget:gap_2atr" and r.get("candidate")]
    print(f"gap_2atr-blocked candidates with strikes: {len(gap)}")
    shorts = Counter(r["candidate"]["short"] for r in gap)
    print("blocked SHORT strikes (top 10):")
    for k, v in shorts.most_common(10):
        print(f"  {v:4d}  short {k}")
    spots = [r["spot"] for r in gap if r.get("spot") is not None]
    if spots:
        print(f"spot range while blocked: {min(spots):.2f}–{max(spots):.2f}")
    # distance of blocked shorts below spot (in ATR)
    dists = []
    for r in gap:
        s, spot, atr = r["candidate"]["short"], r.get("spot"), r.get("atr")
        if spot and atr:
            dists.append((spot - s) / atr)
    if dists:
        dists.sort()
        print(f"blocked-short distance below spot (ATR): min {min(dists):.2f} | "
              f"median {dists[len(dists)//2]:.2f} | max {max(dists):.2f}")
        print("  -> HYPOTHESIS: gap_2atr blocks a NARROW band of near-money shorts; the bot keeps "
              "re-proposing similar strikes all day, all rejected by the same budget.")


def part2_markouts(markouts_path):
    print("\n" + "=" * 70)
    print("PART 2B — Q A/D: favorable-to-seller by distance & entry time (markouts)")
    print("=" * 70)
    rows = list(csv.DictReader(open(markouts_path)))
    def fnum(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    # favorable-to-seller = spread value FELL over the horizon (seller of the spread profits).
    by_dist = defaultdict(lambda: [0, 0])   # distance bucket -> [favorable, total] at 60m
    by_hour = defaultdict(lambda: [0, 0])
    used = 0
    for r in rows:
        mv = fnum(r.get("spread_move_60m"))
        spy = fnum(r.get("entry_spy"))
        short = fnum(r.get("short_strike"))
        sig = r.get("signal_id", "")
        if mv is None or spy is None or short is None:
            continue
        used += 1
        dist = spy - short   # points OTM
        bucket = ("<6pt" if dist < 6 else "6-9pt" if dist < 9 else "9-12pt" if dist < 12 else ">=12pt")
        fav = mv < 0
        by_dist[bucket][1] += 1
        by_dist[bucket][0] += 1 if fav else 0
        # hour from signal_id date-seq isn't a clock; skip time-of-day unless a timestamp exists
    print(f"markout rows with 60m spread move + strikes: {used}")
    print("favorable-to-seller (spread fell) by short-strike distance (60m):")
    order = ["<6pt", "6-9pt", "9-12pt", ">=12pt"]
    for b in order:
        f, t = by_dist[b]
        if t:
            print(f"  {b:>7}: {f}/{t} = {f/t:.0%} favorable   (n={t})")
    print("  -> HYPOTHESIS: if favorable-rate RISES with distance, requiring more OTM distance "
          "(advisor Q A) trades frequency for a higher seller-win rate. One session; not validated.")


def main():
    cap = sys.argv[1] if len(sys.argv) > 1 else None
    mk = sys.argv[2] if len(sys.argv) > 2 else None
    if not cap:
        print("usage: python research/replay_20260724_analysis.py <capture.jsonl> <markouts.csv>")
        return
    recs = part1_baseline(cap)
    part2_gap2atr(recs)
    if mk:
        part2_markouts(mk)


if __name__ == "__main__":
    main()
