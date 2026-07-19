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
        # T10 (spec §17): the reconciled entry funnel -- see build_funnel.
        "funnel": build_funnel(period_rows),
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


    # ── T10 (spec §17): the reconciled entry funnel ─────────────────────────────────────────────
    fn = report.get("funnel")
    if fn:
        a, c, o = fn["activity"], fn["contracts"], fn["orders"]
        lines.append("")
        lines.append("## Entry funnel (spec §17)")
        lines.append("")
        lines.append("### Activity")
        lines.append(f"- Entry cycles started: {a['entry_cycles_started']}")
        lines.append(f"- Raw candidate evaluations: {a['raw_candidate_evaluations']}")
        lines.append(f"- Unique candidate opportunities: {a['unique_candidate_opportunities']}")
        lines.append(f"- Unique strike pairs: {a['unique_strike_pairs']}")
        lines.append(f"- Unique expirations evaluated: {a['unique_expirations_evaluated']}")
        lines.append("")
        lines.append("> Profitability figures must use UNIQUE candidate opportunities, never raw")
        lines.append("> evaluations -- the raw count is a polling-frequency measure (spec §12).")
        lines.append("")
        lines.append("### Credit and quality gates")
        lines.append(_table(["gate", "count"],
                            [[k, v] for k, v in _sorted_group_items(fn["credit_gates"])]))
        lines.append("")
        lines.append("### Quantity outcomes")
        lines.append(_table(["outcome", "count"],
                            [[k, v] for k, v in _sorted_group_items(fn["quantity_outcomes"])]))
        if fn["limiting_caps"]:
            lines.append("")
            lines.append("### Limiting cap on zero-capacity blocks")
            lines.append(_table(["cap", "count"],
                                [[k, v] for k, v in _sorted_group_items(fn["limiting_caps"])]))
        lines.append("")
        lines.append("### Contract reconciliation")
        lines.append(f"- Requested: {c['requested']}")
        lines.append(f"- Quality-adjusted: {c['quality_adjusted']}")
        lines.append(f"- Risk-approved: {c['risk_approved']}")
        lines.append(f"- Submitted: {c['submitted']}")
        lines.append(f"- Filled: {c['filled']}")
        lines.append(f"- Removed by credit sizing: {c['removed_by_credit_sizing']}")
        lines.append(f"- Removed by aggregate risk: {c['removed_by_aggregate_risk']}")
        lines.append(f"- Removed by gap stress: {c['removed_by_gap_stress']}")
        lines.append(f"- **Reconciles: {c['reconciles']}**"
                     + ("" if c["reconciles"] else
                        "  <- requested != filled + removed; a stage is misreporting"))
        lines.append("")
        lines.append("### Order funnel")
        lines.append(f"- Candidates allowed: {o['candidates_allowed']}")
        lines.append(f"- Orders submitted: {o['orders_submitted']}")
        lines.append(f"- Orders filled: {o['orders_filled']}")
        lines.append(f"- Orders partially filled: {o['orders_partially_filled']}")
        lines.append(f"- Orders cancelled: {o['orders_cancelled']}")
        lines.append(f"- Orders expired: {o['orders_expired']}")
        lines.append(f"- Qualified but unfilled: {o['qualified_but_unfilled']}")
        g = fn.get("gap_comparison")
        if g:
            lines.append("")
            lines.append("### Gap model comparison (Black-Scholes enforcing, intrinsic for reference)")
            lines.append(f"- Candidates compared: {g['candidates_compared']}")
            lines.append(f"- Contracts intrinsic would permit: {g['contracts_intrinsic_would_permit']}")
            lines.append(f"- Contracts Black-Scholes permits: {g['contracts_black_scholes_permits']}")
            lines.append("- Candidates Black-Scholes turned tradeable -> zero: "
                         f"{g['candidates_zeroed_by_black_scholes']}")
            lines.append(f"- Incremental stress difference avg: {g['avg_incremental_difference']}")
            lines.append(f"- Incremental stress difference median: {g['median_incremental_difference']}")
            lines.append(f"- Incremental stress difference max: {g['max_incremental_difference']}")
        fw = fn.get("five_wide_shadow")
        if fw:
            lines.append("")
            lines.append("### $5-wide shadow (research only -- never traded)")
            lines.append(f"- Evaluated: {fw['evaluated']}")
            lines.append(f"- Narrower leg available: {fw['available']}")
            lines.append(f"- Would have qualified: {fw['would_have_qualified']}")
            lines.append(f"- Hypothetical contracts: {fw['hypothetical_contracts']}")


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


# ── T10 (spec §17): the reconciled entry funnel ─────────────────────────────────────────────────
# Accounts for every candidate from "the bot woke up" to "contracts actually filled", so a session
# with no trades can be EXPLAINED rather than guessed at. The reconciliation check is the point: if
# requested contracts do not equal filled plus everything removed along the way, a stage is lying.

_QUOTE_REASONS = ("quote_invalid", "quote_wide", "quote_stale")


def _i(x):
    """Int coercion for CSV strings; None on anything unparseable (never raises)."""
    v = _f(x)
    return int(v) if v is not None else None


def _truthy(x):
    return str(x).strip().lower() in ("true", "1", "yes")


def _max_counter(rows, key):
    """Cumulative BotState counters are snapshotted onto every row, so the window's value is the
    HIGHEST seen -- summing them would multiply by the number of decisions logged."""
    vals = [_i(r.get(key)) for r in rows]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else 0


def _percentile_sorted(s, p):
    """Percentile of an already-sorted list (linear interpolation). Empty -> None."""
    if not s:
        return None
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def build_funnel(rows):
    """The section 17 funnel for an already-windowed set of log rows."""
    decisions = [r for r in rows if r.get("event") == "DECISION"]
    opens = [r for r in rows if r.get("event") == "OPEN"]
    shadows = [r for r in rows if r.get("event") == "FIVE_WIDE_SHADOW"]

    def dec(name):
        return [r for r in decisions if r.get("decision") == name]

    # ── activity ────────────────────────────────────────────────────────────────────────────────
    pairs = {(r.get("short"), r.get("long")) for r in decisions
             if r.get("short") not in (None, "") and r.get("long") not in (None, "")}
    expiries = {r.get("expiry") for r in decisions if r.get("expiry") not in (None, "")}
    activity = {
        "entry_cycles_started": _max_counter(decisions, "entry_cycles_started"),
        "raw_candidate_evaluations": _max_counter(decisions, "raw_candidate_evaluations"),
        "unique_candidate_opportunities": _max_counter(decisions, "unique_candidate_opportunities"),
        "unique_strike_pairs": len(pairs),
        "unique_expirations_evaluated": len(expiries),
    }

    # ── credit / quality gates ──────────────────────────────────────────────────────────────────
    # The 8-10% low-credit band is distinguished from an outright sub-floor reject by the ratio, so
    # "the lane declined it" is never conflated with "the credit was never plausible".
    low_band = [r for r in decisions
                if 0.08 <= (_f(r.get("credit_ratio")) or 0) < 0.10]
    credit_gates = {
        "rejected_below_absolute_floor": sum(
            1 for r in dec("credit_too_low") if (_f(r.get("credit_ratio")) or 0) < 0.08),
        "rejected_low_credit_safety": sum(
            1 for r in low_band if r.get("decision") == "credit_too_low"),
        "qualified_low_credit_safety": sum(
            1 for r in low_band if r.get("decision") != "credit_too_low"),
        "qualified_probe": sum(1 for r in decisions if _f(r.get("credit_quality_mult")) == 0.4),
        "qualified_full_size": sum(1 for r in decisions if _f(r.get("credit_quality_mult")) == 1.0),
        "rejected_transaction_cost": len(dec("cost_gate")),
        "rejected_quote_quality": sum(len(dec(x)) for x in _QUOTE_REASONS),
        "rejected_trend": len(dec("trend_paused")),
        "rejected_regime": len(dec("regime_blocked")),
        "rejected_duplicate": len(dec("duplicate_strikes")),
    }

    # ── quantity outcomes ───────────────────────────────────────────────────────────────────────
    def outcome(name):
        return [r for r in decisions if r.get("decision_outcome") == name]
    reduced = outcome("allowed_reduced")
    quantity_outcomes = {
        "allowed_at_requested_quantity": len(outcome("allowed_full")),
        "allowed_at_reduced_quantity": len(reduced),
        "allowed_as_one_contract_probe": sum(1 for r in reduced if _i(r.get("final_qty")) == 1),
        "blocked_at_zero_capacity": len(outcome("blocked_zero_capacity")),
        "blocked_credit_quality": len(outcome("blocked_credit_quality")),
    }

    # Limiting caps counted ONLY for zero-capacity blocks. A cap that merely trimmed a trade is not
    # the same as one that killed it, and merging them would make gap stress look like it is
    # rejecting trades it actually only reduced.
    limiting_caps = Counter(r.get("limiting_gate") for r in outcome("blocked_zero_capacity")
                            if r.get("limiting_gate"))

    # ── contract reconciliation ─────────────────────────────────────────────────────────────────
    requested = sum(_i(r.get("requested_qty")) or 0 for r in decisions)
    quality = sum(_i(r.get("quality_adjusted_qty")) or 0 for r in decisions)
    approved = sum(_i(r.get("final_qty")) or 0 for r in decisions)
    filled_contracts = sum(_i(r.get("qty")) or 0 for r in opens
                           if (r.get("status") or "").strip().lower() == "filled")
    by_gap = by_agg = 0
    for r in decisions:
        q, fin = _i(r.get("quality_adjusted_qty")), _i(r.get("final_qty"))
        if q is None or fin is None or q <= fin:
            continue
        removed = q - fin
        if str(r.get("limiting_gate") or "").startswith("gap_"):
            by_gap += removed
        else:
            by_agg += removed
    by_credit = max(0, requested - quality)
    contracts = {
        "requested": requested,
        "quality_adjusted": quality,
        "risk_approved": approved,
        "submitted": sum(_i(r.get("qty")) or 0 for r in opens),
        "filled": filled_contracts,
        "removed_by_credit_sizing": by_credit,
        "removed_by_aggregate_risk": by_agg,
        "removed_by_gap_stress": by_gap,
    }
    contracts["reconciles"] = (requested == filled_contracts + by_credit + by_agg + by_gap)

    # ── order funnel ────────────────────────────────────────────────────────────────────────────
    def status_count(name):
        return sum(1 for r in opens if (r.get("status") or "").strip().lower() == name)
    orders_filled = status_count("filled")
    orders = {
        "candidates_allowed": len(dec("filled")),
        "orders_submitted": len(opens),
        "orders_filled": orders_filled,
        "orders_partially_filled": status_count("partially_filled") + status_count("partial"),
        "orders_cancelled": status_count("cancelled") + status_count("canceled"),
        "orders_expired": status_count("expired"),
        "qualified_but_unfilled": max(0, len(dec("filled")) - orders_filled),
    }

    # ── gap-model comparison (Bot B validation) ─────────────────────────────────────────────────
    cmp_rows = [r for r in decisions if r.get("bs_qty_1_5") not in (None, "")]
    gap_comparison = None
    if cmp_rows:
        diffs = []
        for r in cmp_rows:
            b, i = _f(r.get("bs_incremental_1_5")), _f(r.get("intrinsic_incremental_1_5"))
            if b is not None and i is not None:
                diffs.append(b - i)
        gap_comparison = {
            "candidates_compared": len(cmp_rows),
            "contracts_intrinsic_would_permit": sum(_i(r.get("intrinsic_qty_1_5")) or 0
                                                    for r in cmp_rows),
            "contracts_black_scholes_permits": sum(_i(r.get("bs_qty_1_5")) or 0 for r in cmp_rows),
            "candidates_zeroed_by_black_scholes": sum(
                1 for r in cmp_rows if _truthy(r.get("bs_zeroed_the_candidate"))),
            "avg_incremental_difference": round(sum(diffs) / len(diffs), 2) if diffs else None,
            "median_incremental_difference": (round(_percentile_sorted(sorted(diffs), 50), 2)
                                              if diffs else None),
            "max_incremental_difference": round(max(diffs), 2) if diffs else None,
        }

    # ── $5-wide shadow ──────────────────────────────────────────────────────────────────────────
    five_wide = None
    if shadows:
        qualified = [r for r in shadows if (_i(r.get("five_wide_final_qty")) or 0) > 0]
        five_wide = {
            "evaluated": len(shadows),
            "available": sum(1 for r in shadows if _truthy(r.get("five_wide_available"))),
            "would_have_qualified": len(qualified),
            "hypothetical_contracts": sum(_i(r.get("five_wide_final_qty")) or 0 for r in qualified),
            "by_ten_wide_gate": dict(Counter(r.get("ten_wide_limiting_gate") for r in shadows
                                             if r.get("ten_wide_limiting_gate"))),
        }

    return {
        "activity": activity,
        "credit_gates": credit_gates,
        "quantity_outcomes": quantity_outcomes,
        "limiting_caps": dict(limiting_caps),
        "contracts": contracts,
        "orders": orders,
        "gap_comparison": gap_comparison,
        "five_wide_shadow": five_wide,
    }
