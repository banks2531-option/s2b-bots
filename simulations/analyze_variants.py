"""Variant-correlation analysis for the 2026-06-11 S2b scaling study.

Reads trade CSVs produced by prior backtest runs (no simulation, no DB, no
network), buckets net P&L by settling ISO week (week of exit_date), and
compares each entry-day variant arm against the Monday baseline
(trades_scale_sz5.csv): activity overlap, loss weeks, co-loss weeks, Pearson
correlation of weekly nets, and worst combined week. Note: for v2/v4 the
Monday entries are already inside the variant, so the combined figure
double-counts Mondays; the standalone worst week is also reported.
Writes variant_correlation.json.
"""
import csv, json, math, os
from datetime import date
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE = "trades_scale_sz5.csv"
VARIANTS = ["trades_scale_v1_wed.csv", "trades_scale_v1_fri.csv",
            "trades_scale_v2_mwf.csv", "trades_scale_v3_qqq_mon.csv",
            "trades_scale_v4_mwf_plus_qqq.csv"]
OVERLAPS_BASELINE = {"trades_scale_v2_mwf.csv", "trades_scale_v4_mwf_plus_qqq.csv"}


def iso_week(datestr):
    y, w, _ = date.fromisoformat(datestr).isocalendar()
    return "%d-W%02d" % (y, w)


def weekly_pnl(path):
    """Return {iso_week: net_pnl} keyed by settling week of exit_date."""
    weeks = defaultdict(float)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            weeks[iso_week(row["exit_date"])] += float(row["pnl"])
    return dict(weeks)


def worst_week(weeks):
    if not weeks:
        return None
    wk = min(weeks, key=weeks.get)
    return {"week": wk, "net": round(weeks[wk], 2)}


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def vs_baseline(base, var, name):
    union = sorted(set(base) | set(var))
    b = [base.get(w, 0.0) for w in union]
    v = [var.get(w, 0.0) for w in union]
    combined = {w: bb + vv for w, bb, vv in zip(union, b, v)}
    r = pearson(b, v)
    return {
        "weeks_union": len(union),
        "weeks_both_active": sum(1 for w in union if base.get(w) and var.get(w)),
        "weeks_baseline_loss": sum(1 for x in b if x < 0),
        "weeks_variant_loss": sum(1 for x in v if x < 0),
        "weeks_co_loss": sum(1 for bb, vv in zip(b, v) if bb < 0 and vv < 0),
        "pearson_weekly": None if r is None else round(r, 4),
        "worst_combined_week": worst_week(combined),
        "baseline_inside_variant": name in OVERLAPS_BASELINE,
    }


def main():
    results, missing = {}, []
    for name in [BASELINE] + VARIANTS:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            missing.append(name)
            continue
        weeks = weekly_pnl(path)
        results[name] = {"weekly": {w: round(p, 2) for w, p in sorted(weeks.items())},
                         "worst_week": worst_week(weeks)}

    base = {w: results[BASELINE]["weekly"][w] for w in results[BASELINE]["weekly"]} \
        if BASELINE in results else None

    print("=== S2b scaling study: variant correlation vs Monday baseline ===")
    if base is None:
        print("Baseline %s missing -- per-arm summaries only." % BASELINE)
    else:
        print("\nBaseline (%s) weekly net P&L (settling week of exit):" % BASELINE)
        for w, p in results[BASELINE]["weekly"].items():
            print("  %s  %10.2f" % (w, p))
        ww = results[BASELINE]["worst_week"]
        print("  worst week: %s  %.2f" % (ww["week"], ww["net"]))

    hdr = ("arm", "weeks", "both_act", "base<0", "var<0", "co-loss", "corr", "worst_comb", "worst_alone")
    print("\n%-34s %5s %8s %6s %5s %7s %7s %11s %11s" % hdr)
    for name in VARIANTS:
        if name not in results:
            print("%-34s  (missing -- skipped)" % name)
            continue
        own = results[name]["worst_week"]
        if base is not None:
            cmp_ = vs_baseline(base, results[name]["weekly"], name)
            results[name]["vs_baseline"] = cmp_
            r = cmp_["pearson_weekly"]
            note = "*" if cmp_["baseline_inside_variant"] else ""
            print("%-34s %5d %8d %6d %5d %7d %7s %11.2f %10.2f%s" % (
                name, cmp_["weeks_union"], cmp_["weeks_both_active"],
                cmp_["weeks_baseline_loss"], cmp_["weeks_variant_loss"],
                cmp_["weeks_co_loss"], "n/a" if r is None else "%.3f" % r,
                cmp_["worst_combined_week"]["net"], own["net"], note))
        else:
            print("%-34s  standalone worst week: %s %.2f" % (name, own["week"], own["net"]))
    print("\n* baseline Monday entries already inside this variant; combined sum double-counts Mondays.")
    if missing:
        print("Missing inputs (skipped): " + ", ".join(missing))

    out = os.path.join(HERE, "variant_correlation.json")
    with open(out, "w") as f:
        json.dump({"study": "2026-06-11 S2b scaling", "baseline": BASELINE,
                   "missing": missing, "arms": results}, f, indent=2)
    print("Wrote %s" % out)


if __name__ == "__main__":
    main()
