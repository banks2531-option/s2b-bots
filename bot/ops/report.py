"""Daily/weekly report generator for the all-days S2b bot (partner spec §18). READ-ONLY.

Reads a bot's own CSV logs (`trades_<tag>.csv`, produced by `bot.app.wiring.make_trade_logger`,
plus the optional research-only `markouts_<tag>.csv`, spec §14) and aggregates the §18 report
metrics into a plain dict (`build_report`) + a markdown renderer (`render_markdown`).

This module is purely retrospective: it never opens a broker connection, never touches bot state,
and never feeds back into a trading decision. It is run out-of-band, after the fact, against
files already on disk -- `python -m bot.ops.report --trades trades_alldays.csv`.

Log shape recap (spec §1/§3/§5/§8/§10/§12/§13; see bot.app.wiring):
  - event in {OPEN, CLOSE, DEGROSS, FLOW_DEGROSS, DECISION, SHADOW} (DEGROSS/FLOW_DEGROSS are
    closes too, from the opt-in de-gross cycles -- treated identically to CLOSE here).
  - OPEN/CLOSE-like rows: event,date,ticker,short,long,expiry,qty,credit,action,exit_value,pnl,
    status (+ gross_pnl/net_pnl when actual_fill_accounting is on, §8).
  - DECISION rows (decision_logging, §1/§12): decision,flags,positions_today,positions_in_expiry,
    adjacent_strike_distance,agg_remaining_stop,agg_structural,agg_gap_stress_1_5, plus
    credit_ratio/credit_pctl40/credit_sample_count/credit_threshold/credit_quality_mult (§3, once
    credit_tiers ran) and cost_gross_target/cost_round_trip/cost_target_ratio (§5, once
    transaction_cost_gate ran) and expected_executable_credit (§4).
  - SHADOW rows (regime_shadow_monitor, §13): shadow_score, shadow_action, + the §13 signal set.

Not every row has every column -- csv.DictReader gives missing/blank fields, never a KeyError, so
every accessor here goes through `.get()` + best-effort float coercion and degrades to None rather
than raising. A field genuinely absent from a given log (e.g. a live-bot log with the original
12-column shape) simply reads as "not available" in the report rather than crashing it.
"""
import argparse
import csv
import os
import statistics
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta

from bot.strategy.credit_quality import dte_bucket

DEFAULT_ALLOCATED_EQUITY = 72000.0   # spec §2 -- BOT_ALLOCATED_EQUITY / features.allocated_equity

# A CLOSE-like row realizes P&L; DEGROSS/FLOW_DEGROSS are opt-in de-gross closes logged with the
# exact same pnl/gross_pnl/net_pnl shape as CLOSE (bot.app.orchestrator), so they count too.
CLOSE_EVENTS = {"CLOSE", "DEGROSS", "FLOW_DEGROSS"}

# DECISION reasons logged BEFORE a candidate order even exists (run_entry_cycle's early gates) --
# excluded from "candidate DECISIONs" for fill-rate/reject purposes (spec §18: fill rate is filled
# OPENs over candidate decisions, not every tick the bot happened to look at the clock).
PRE_SIGNAL_GATES = {"halted", "wrong_weekday", "off_hours", "max_open", "max_entries"}


# ── low-level parsing helpers (never raise) ─────────────────────────────────────────────────────

def _read_rows(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _f(x):
    """Best-effort float coercion: None/""/unparsable -> None (never raises)."""
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _pnl_of(row):
    """Realized P&L for a CLOSE-like row: net_pnl, falling back to pnl when the cost split isn't
    present (spec §18: 'fall back to pnl when the split isn't present')."""
    net = _f(row.get("net_pnl"))
    return net if net is not None else _f(row.get("pnl"))


def _gross_pnl_of(row):
    gross = _f(row.get("gross_pnl"))
    return gross if gross is not None else _f(row.get("pnl"))


def _pos_key(row):
    return (row.get("ticker") or "SPY", row.get("short"), row.get("long"), row.get("expiry"))


def _tier_label(mult):
    if mult is None:
        return "unclassified"
    if mult <= 0:
        return "unclassified"   # a mult==0.0 candidate was rejected (credit_too_low), never opens
    if mult >= 0.999:
        return "full"
    return "probe"


# ── period windowing ─────────────────────────────────────────────────────────────────────────────

def _period_window(rows, period, asof):
    """(start, end) datetimes (inclusive) for the report window, or (None, None) if no reference
    date is available anywhere (empty log and no asof given -> the report simply covers everything,
    unfiltered, rather than raising)."""
    if asof is None:
        dates = [d for d in (_parse_date(r.get("date")) for r in rows) if d is not None]
        asof_dt = max(dates) if dates else None
    else:
        asof_dt = asof if isinstance(asof, datetime) else _parse_date(asof)
    if asof_dt is None:
        return None, None
    if period == "weekly":
        start = asof_dt - timedelta(days=asof_dt.weekday())   # Monday of asof's ISO week
        end = start + timedelta(days=6)                       # Sunday
    else:
        start = end = asof_dt
    return start, end


def _in_window(date_str, start, end):
    if start is None:
        return True
    d = _parse_date(date_str)
    return d is not None and start <= d <= end


# ── trade matching: pair each CLOSE-like row back to the OPEN that created it ───────────────────

def _match_trades(rows):
    """FIFO-pair every CLOSE-like row (by ticker/short/long/expiry) to the OPEN that created it,
    attaching entry_date/weekday/dte_bucket/entry_number_in_day/credit-quality tier -- everything
    the §18 by-tier/by-DTE/by-weekday/by-entry-number breakdowns need. Run over the FULL log
    (never just a period slice) so a position opened one day and closed a later day still joins
    correctly; callers filter the resulting trades by exit_date for a given report window.

    Best-effort: a CLOSE with no matching OPEN in this log (e.g. the log starts mid-position) still
    contributes its P&L, just with entry-side attribution left None ('unknown'/'unclassified' at
    render time)."""
    open_queues = defaultdict(deque)
    entry_seq = defaultdict(int)   # date -> running OPEN count so far (entry-number-within-day)
    pending_tier = None   # the last DECISION(filled) row's tier, consumed by the very next OPEN
    trades = []
    for row in rows:
        event = row.get("event")
        if event == "DECISION":
            if row.get("decision") == "filled":
                pending_tier = _tier_label(_f(row.get("credit_quality_mult")))
            continue
        if event == "OPEN" and (row.get("status") or "").strip().lower() == "filled":
            date = row.get("date")
            entry_seq[date] += 1
            entry_dt = _parse_date(date)
            expiry_dt = _parse_date(row.get("expiry"))
            dte = (expiry_dt - entry_dt).days if entry_dt and expiry_dt else None
            meta = {
                "entry_date": date,
                "weekday": entry_dt.strftime("%A") if entry_dt else None,
                "dte_bucket": dte_bucket(dte) if dte is not None else None,
                "entry_number_in_day": entry_seq[date],
                "tier": pending_tier,
            }
            pending_tier = None
            open_queues[_pos_key(row)].append(meta)
            continue
        if event in CLOSE_EVENTS:
            q = open_queues.get(_pos_key(row))
            meta = q.popleft() if q else {}
            base = {"entry_date": None, "weekday": None, "dte_bucket": None,
                    "entry_number_in_day": None, "tier": None}
            base.update(meta)
            base.update({"exit_date": row.get("date"), "pnl": _pnl_of(row),
                         "gross_pnl": _gross_pnl_of(row)})
            trades.append(base)
    return trades


# ── §18 metric blocks (each operates on the already period-filtered rows) ──────────────────────

def _fill_and_reject_stats(period_rows):
    decisions = [r for r in period_rows if r.get("event") == "DECISION"]
    candidates = [r for r in decisions if r.get("decision") not in PRE_SIGNAL_GATES]
    filled_opens = sum(1 for r in period_rows if r.get("event") == "OPEN"
                       and (r.get("status") or "").strip().lower() == "filled")
    rejects = [r for r in candidates if r.get("decision") != "filled"]
    fill_rate = (filled_opens / len(candidates)) if candidates else None
    return {
        "total_decisions": len(decisions),
        "candidate_decisions": len(candidates),
        "filled": filled_opens,
        "fill_rate": round(fill_rate, 4) if fill_rate is not None else None,
        "rejected_total": len(rejects),
        "reject_counts": dict(Counter(r.get("decision") for r in rejects)),
        "pre_signal_gate_counts": dict(Counter(
            r.get("decision") for r in decisions if r.get("decision") in PRE_SIGNAL_GATES)),
    }


def _slippage_stats(period_rows):
    """Best-effort actual slippage: the conservative expected-executable credit (spec §4, logged on
    the DECISION(filled) row immediately preceding each OPEN) vs. the credit the OPEN actually got.
    NOT raw package-mid vs. fill (the trade log doesn't carry package mid at all) -- the closest
    proxy the current log schema supports. Positive = filled better than the conservative estimate."""
    diffs = []
    pending_expected = None
    for row in period_rows:
        event = row.get("event")
        if event == "DECISION":
            pending_expected = (_f(row.get("expected_executable_credit"))
                                if row.get("decision") == "filled" else None)
            continue
        if event == "OPEN" and (row.get("status") or "").strip().lower() == "filled":
            if pending_expected is not None:
                actual = _f(row.get("credit"))
                if actual is not None:
                    diffs.append(round(actual - pending_expected, 4))
            pending_expected = None
    if not diffs:
        return {"available": False, "count": 0, "avg": None, "min": None, "max": None,
                "note": "expected_executable_credit not present on DECISION rows in this window "
                        "(credit_tiers/transaction_cost_gate/decision_logging off, or no fills) -- "
                        "actual slippage cannot be computed from this log"}
    return {"available": True, "count": len(diffs),
            "avg": round(sum(diffs) / len(diffs), 4), "min": min(diffs), "max": max(diffs)}


def _risk_telemetry(period_rows):
    """Max aggregate book-wide risk seen across every DECISION row in the window (spec §18: same-
    day/expiry/total-structural risk, 1.5-ATR stress exposure), sourced from the DECISION
    telemetry's agg_* fields (bot.app.orchestrator._decision_telemetry). Included over EVERY
    DECISION row, not just candidates -- a pre-signal-gate reject still snapshots the real book
    state at that moment."""
    decisions = [r for r in period_rows if r.get("event") == "DECISION"]
    structural = [x for x in (_f(r.get("agg_structural")) for r in decisions) if x is not None]
    stop = [x for x in (_f(r.get("agg_remaining_stop")) for r in decisions) if x is not None]
    gap = [x for x in (_f(r.get("agg_gap_stress_1_5")) for r in decisions) if x is not None]
    return {
        "max_total_structural_risk": max(structural) if structural else None,
        "max_total_remaining_stop_risk": max(stop) if stop else None,
        "max_gap_stress_1_5": max(gap) if gap else None,
    }


def _same_day_and_expiry_structural_risk(period_rows):
    """Same-day / per-expiration structural risk (spec §18): the DECISION telemetry only aggregates
    agg_structural over the WHOLE book, never split by day or expiry, so this is computed directly
    from filled OPEN rows instead -- wing_width = |short - long| (both on every OPEN row), summed
    per calendar date opened and per expiration date. This is worst-case STRUCTURAL loss at entry
    (spec §9's structural_max_loss_per_contract), not the live mark-dependent remaining-stop risk."""
    by_day, by_expiry = defaultdict(float), defaultdict(float)
    for row in period_rows:
        if row.get("event") != "OPEN" or (row.get("status") or "").strip().lower() != "filled":
            continue
        short, long_ = _f(row.get("short")), _f(row.get("long"))
        credit, qty = _f(row.get("credit")), _f(row.get("qty"))
        if None in (short, long_, credit, qty):
            continue
        structural = (abs(short - long_) - credit) * 100.0 * qty
        by_day[row.get("date")] += structural
        by_expiry[row.get("expiry")] += structural
    return {
        "max_same_day_structural_risk": round(max(by_day.values()), 2) if by_day else None,
        "max_expiration_structural_risk": round(max(by_expiry.values()), 2) if by_expiry else None,
    }


def _drawdown_and_worst_session(period_rows, allocated_equity):
    """Max drawdown on the cumulative net-P&L equity curve (ordered by exit date; ties keep the
    log's own append order) + the worst single session (date with the most negative net P&L),
    spec §18. Returns are ALSO expressed as a fraction of allocated_equity (spec §2/§18), never any
    account balance -- this function never reads one."""
    closes = [r for r in period_rows if r.get("event") in CLOSE_EVENTS]
    ordered = sorted(enumerate(closes),
                     key=lambda pair: (_parse_date(pair[1].get("date")) or datetime.min, pair[0]))
    by_day = defaultdict(float)
    cum = peak = max_dd = 0.0
    for _, row in ordered:
        pnl = _pnl_of(row)
        if pnl is None:
            continue
        cum += pnl
        by_day[row.get("date")] += pnl
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    worst_date = worst_pnl = None
    if by_day:
        worst_date, worst_pnl = min(by_day.items(), key=lambda kv: kv[1])
    return {
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct_of_allocated_equity": (round(max_dd / allocated_equity, 4)
                                                 if allocated_equity else None),
        "worst_session_date": worst_date,
        "worst_session_pnl": round(worst_pnl, 2) if worst_pnl is not None else None,
        "ending_cum_pnl": round(cum, 2),
    }


def _shadow_stats(period_rows):
    """Shadow-regime decision summary (spec §13/§18): counts of shadow_action labels + the
    caution-score distribution, over every SHADOW row in the window."""
    shadows = [r for r in period_rows if r.get("event") == "SHADOW"]
    actions = Counter(r.get("shadow_action") for r in shadows if r.get("shadow_action"))
    scores = [x for x in (_f(r.get("shadow_score")) for r in shadows) if x is not None]
    dist = None
    if scores:
        dist = {"count": len(scores), "min": min(scores), "max": max(scores),
                "mean": round(statistics.fmean(scores), 4), "median": round(statistics.median(scores), 4)}
    return {"count": len(shadows), "action_counts": dict(actions), "score_distribution": dist}


def _markout_summary(markout_rows):
    """Lightweight best-effort summary of the §14 research markout log, when supplied. Purely
    informational -- never feeds back into any §18 metric above."""
    if not markout_rows:
        return None
    def _is_true(v):
        return str(v).strip().lower() in ("true", "1")
    filled = [r for r in markout_rows if _is_true(r.get("filled"))]
    rejected = [r for r in markout_rows if not _is_true(r.get("filled"))]
    def _avg(rs, field):
        vals = [x for x in (_f(r.get(field)) for r in rs) if x is not None]
        return round(sum(vals) / len(vals), 4) if vals else None
    return {
        "count": len(markout_rows), "filled_count": len(filled), "rejected_count": len(rejected),
        "avg_mfe_filled": _avg(filled, "mfe"), "avg_mae_filled": _avg(filled, "mae"),
        "avg_mfe_rejected": _avg(rejected, "mfe"), "avg_mae_rejected": _avg(rejected, "mae"),
    }


def _group_performance(trades, key_fn):
    """count/total/avg/win-rate of trade['pnl'] grouped by key_fn(trade)."""
    groups = defaultdict(list)
    for t in trades:
        groups[key_fn(t)].append(t)
    out = {}
    for k, ts in groups.items():
        pnls = [t["pnl"] for t in ts if t["pnl"] is not None]
        out[k] = {
            "count": len(ts),
            "total_pnl": round(sum(pnls), 2) if pnls else 0.0,
            "avg_pnl": round(sum(pnls) / len(pnls), 2) if pnls else None,
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4) if pnls else None,
        }
    return out


# ── top-level entry point ───────────────────────────────────────────────────────────────────────

def build_report(trade_log_path, markout_log_path=None, allocated_equity=DEFAULT_ALLOCATED_EQUITY,
                 period="daily", asof=None):
    """Build the partner-spec §18 report dict from a bot's own CSV logs. READ-ONLY.

    `asof`: reference date (YYYY-MM-DD string or datetime); defaults to the latest date found in
    the trade log. period="daily" windows to that single calendar date; period="weekly" windows to
    the Monday-Sunday ISO week containing asof.

    All return/percent figures are expressed against `allocated_equity` (spec §2 -- the bot's
    ALLOCATED equity, e.g. 72000 -- never the shared account's total balance; this module never
    reads an account balance at all, so there is nothing else it could use)."""
    if period not in ("daily", "weekly"):
        raise ValueError(f"period must be 'daily' or 'weekly', got {period!r}")

    all_rows = _read_rows(trade_log_path)
    start, end = _period_window(all_rows, period, asof)
    period_rows = [r for r in all_rows if _in_window(r.get("date"), start, end)]

    all_trades = _match_trades(all_rows)
    period_trades = [t for t in all_trades if _in_window(t["exit_date"], start, end)]

    close_rows = [r for r in period_rows if r.get("event") in CLOSE_EVENTS]
    gross_total = sum(g for g in (_gross_pnl_of(r) for r in close_rows) if g is not None)
    net_total = sum(n for n in (_pnl_of(r) for r in close_rows) if n is not None)

    markouts = _markout_summary(_read_rows(markout_log_path)) if markout_log_path else None

    return {
        "period": period,
        "window_start": start.strftime("%Y-%m-%d") if start else None,
        "window_end": end.strftime("%Y-%m-%d") if end else None,
        "allocated_equity": allocated_equity,
        "gross_pnl": round(gross_total, 2),
        "net_pnl": round(net_total, 2),
        "return_pct_of_allocated_equity": (round(net_total / allocated_equity, 4)
                                           if allocated_equity else None),
        "closed_trade_count": sum(1 for t in period_trades if t["pnl"] is not None),
        "fill": _fill_and_reject_stats(period_rows),
        "slippage": _slippage_stats(period_rows),
        "risk": {**_risk_telemetry(period_rows), **_same_day_and_expiry_structural_risk(period_rows)},
        "drawdown": _drawdown_and_worst_session(period_rows, allocated_equity),
        "shadow": _shadow_stats(period_rows),
        "by_credit_tier": _group_performance(period_trades, lambda t: t["tier"] or "unclassified"),
        "by_dte_bucket": _group_performance(period_trades, lambda t: t["dte_bucket"] or "unknown"),
        "by_weekday": _group_performance(period_trades, lambda t: t["weekday"] or "unknown"),
        "by_entry_number": _group_performance(
            period_trades,
            lambda t: t["entry_number_in_day"] if t["entry_number_in_day"] is not None else "unknown"),
        "markouts": markouts,
    }


# ── markdown rendering ───────────────────────────────────────────────────────────────────────────

def _sorted_group_items(d):
    def key(kv):
        k = kv[0]
        return (0, k, "") if isinstance(k, (int, float)) else (1, 0, str(k))
    return sorted(d.items(), key=key)


def _table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(lines)


def _group_table(group_dict, label):
    rows = [(k, v["count"], v["total_pnl"], v["avg_pnl"], v["win_rate"])
            for k, v in _sorted_group_items(group_dict)]
    if not rows:
        return f"_no closed trades in this window to break down by {label}._"
    return _table([label, "count", "total pnl", "avg pnl", "win rate"], rows)


def render_markdown(report: dict) -> str:
    """Render a `build_report()` dict as a markdown report covering every partner-spec §18 section."""
    lines = []
    lines.append(f"# S2b All-Days Report -- {report['period']} "
                 f"({report['window_start']} to {report['window_end']})")
    lines.append("")
    lines.append(f"Allocated equity: ${report['allocated_equity']:,.2f} "
                 "(spec §2 -- NOT the shared account's total balance)")
    lines.append("")

    lines.append("## P&L")
    lines.append(f"- Gross P&L: ${report['gross_pnl']:,.2f}")
    lines.append(f"- Net P&L: ${report['net_pnl']:,.2f}")
    rpct = report["return_pct_of_allocated_equity"]
    lines.append(f"- Return on allocated equity: {rpct * 100:.2f}%" if rpct is not None
                else "- Return on allocated equity: n/a")
    lines.append(f"- Closed trades: {report['closed_trade_count']}")
    lines.append("")

    fill = report["fill"]
    lines.append("## Fill Rate & Order Outcomes")
    fr = fill["fill_rate"]
    lines.append(f"- Fill rate: {fr * 100:.1f}% ({fill['filled']} filled / "
                 f"{fill['candidate_decisions']} candidates)" if fr is not None
                else "- Fill rate: n/a (no candidate decisions in this window)")
    lines.append(f"- Rejected/canceled: {fill['rejected_total']}")
    for reason, count in sorted(fill["reject_counts"].items()):
        lines.append(f"  - {reason}: {count}")
    if fill["pre_signal_gate_counts"]:
        lines.append(f"- Pre-signal gates skipped (not counted as candidates):")
        for reason, count in sorted(fill["pre_signal_gate_counts"].items()):
            lines.append(f"  - {reason}: {count}")
    lines.append("")

    slip = report["slippage"]
    lines.append("## Actual Slippage")
    if slip["available"]:
        lines.append(f"- {slip['count']} filled entries matched to their expected-executable "
                     f"credit (spec §4): avg {slip['avg']:+.4f}, min {slip['min']:+.4f}, "
                     f"max {slip['max']:+.4f} (credit units/contract; + = better than expected)")
    else:
        lines.append(f"- Not available: {slip['note']}")
    lines.append("")

    lines.append("## Performance by Credit-Quality Tier")
    lines.append(_group_table(report["by_credit_tier"], "tier"))
    lines.append("")
    lines.append("## Performance by DTE Bucket")
    lines.append(_group_table(report["by_dte_bucket"], "dte bucket"))
    lines.append("")
    lines.append("## Performance by Weekday")
    lines.append(_group_table(report["by_weekday"], "weekday"))
    lines.append("")
    lines.append("## Performance by Entry Number (within day)")
    lines.append(_group_table(report["by_entry_number"], "entry #"))
    lines.append("")

    risk = report["risk"]
    def _money(x):
        return f"${x:,.2f}" if x is not None else "n/a"
    lines.append("## Risk Exposure")
    lines.append(f"- Max same-day structural risk: {_money(risk['max_same_day_structural_risk'])}")
    lines.append(f"- Max expiration structural risk: {_money(risk['max_expiration_structural_risk'])}")
    lines.append(f"- Max total structural risk (book, DECISION telemetry): "
                f"{_money(risk['max_total_structural_risk'])}")
    lines.append(f"- Max total remaining-stop risk (book, DECISION telemetry): "
                f"{_money(risk['max_total_remaining_stop_risk'])}")
    lines.append(f"- Max 1.5-ATR gap-stress exposure: {_money(risk['max_gap_stress_1_5'])}")
    lines.append("")

    dd = report["drawdown"]
    lines.append("## Drawdown & Worst Session")
    ddpct = dd["max_drawdown_pct_of_allocated_equity"]
    lines.append(f"- Max drawdown: ${dd['max_drawdown']:,.2f}"
                + (f" ({ddpct * 100:.2f}% of allocated equity)" if ddpct is not None else ""))
    if dd["worst_session_date"] is not None:
        lines.append(f"- Worst session: {dd['worst_session_date']} (${dd['worst_session_pnl']:,.2f})")
    else:
        lines.append("- Worst session: n/a (no closes in this window)")
    lines.append("")

    shadow = report["shadow"]
    lines.append("## Shadow-Regime Summary")
    lines.append(f"- SHADOW records: {shadow['count']}")
    for action, count in sorted(shadow["action_counts"].items()):
        lines.append(f"  - {action}: {count}")
    if shadow["score_distribution"]:
        d = shadow["score_distribution"]
        lines.append(f"- Caution score: min {d['min']:.4f}, max {d['max']:.4f}, "
                    f"mean {d['mean']:.4f}, median {d['median']:.4f} (n={d['count']})")
    lines.append("")

    if report.get("markouts") is not None:
        mo = report["markouts"]
        lines.append("## Research Markouts (spec §14, informational only)")
        lines.append(f"- {mo['count']} records ({mo['filled_count']} filled / "
                    f"{mo['rejected_count']} rejected)")
        lines.append(f"  - avg MFE/MAE (filled): {mo['avg_mfe_filled']} / {mo['avg_mae_filled']}")
        lines.append(f"  - avg MFE/MAE (rejected): {mo['avg_mfe_rejected']} / {mo['avg_mae_rejected']}")
        lines.append("")

    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────────

def _main(argv=None):
    parser = argparse.ArgumentParser(
        description="Daily/weekly report for an S2b bot's trade log (partner spec §18).")
    parser.add_argument("--trades", required=True, help="path to trades_<tag>.csv")
    parser.add_argument("--markouts", default=None, help="path to markouts_<tag>.csv (optional)")
    parser.add_argument("--weekly", action="store_true", help="weekly report (default: daily)")
    parser.add_argument("--asof", default=None, help="reference date YYYY-MM-DD (default: latest in log)")
    parser.add_argument("--allocated-equity", type=float, default=DEFAULT_ALLOCATED_EQUITY,
                        help=f"bot's allocated equity, spec §2 (default {DEFAULT_ALLOCATED_EQUITY})")
    args = parser.parse_args(argv)
    report = build_report(args.trades, markout_log_path=args.markouts,
                         allocated_equity=args.allocated_equity,
                         period="weekly" if args.weekly else "daily", asof=args.asof)
    print(render_markdown(report))


if __name__ == "__main__":
    _main()
