"""Post-vol-event (IV-crush harvest) test for the S2b put-spread (2026-07-02).

Hypothesis: being SHORT premium THROUGH a scheduled macro vol event (FOMC 2pm decision, CPI 8:30 release)
harvests the post-event IV crush and improves the outcome. Also tests the "enter the day AFTER" (post-crush
calm) variant the user mentioned.

Method: the REAL all-days S2b ledger (`trades_panel_daily.csv`, +2x twin) has entry AND exit dates, so tag
each trade by whether its holding window [entry..exit] SPANNED an event (short through the crush), vs
entered the day AFTER an event, vs neither. Compare PF; NULL = same-size random subset (5000 draws); +
OOS H1/H2. Event dates are exact FOMC announce Wednesdays + confirmed 2025 CPI releases (Oct'25 CPI was
NOT published — govt shutdown; Sep'25 CPI rescheduled to Oct 24). Sample is inherently small — reported honestly."""
import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd

FOMC = pd.to_datetime(["2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30",
                       "2025-09-17", "2025-10-29", "2025-12-10", "2026-01-28"])
CPI = pd.to_datetime(["2025-03-12", "2025-04-10", "2025-05-13", "2025-06-11", "2025-07-15",
                      "2025-08-12", "2025-09-11", "2025-10-24", "2025-12-18", "2026-01-13", "2026-02-11"])
EVENTS = FOMC.append(CPI).sort_values()


def pf(p):
    p = np.asarray(p); w = p[p > 0].sum(); l = -p[p <= 0].sum()
    return (w / l) if l > 0 else float("inf")


def spans_event(row, events):
    return any((row["ent"] <= e <= row["ext"]) for e in events)


def day_after_event(row, events):
    # entry is the 1st or 2nd business day at/after an event date (post-crush entry)
    return any(0 < np.busday_count(e.date(), row["ent"].date()) <= 1 for e in events)


def run(path, label):
    df = pd.read_csv(path)
    df["ent"] = pd.to_datetime(df["entry_date"]); df["ext"] = pd.to_datetime(df["exit_date"])
    df = df.sort_values("ent").reset_index(drop=True)
    df["span_any"] = df.apply(lambda r: spans_event(r, EVENTS), axis=1)
    df["span_fomc"] = df.apply(lambda r: spans_event(r, FOMC), axis=1)
    df["span_cpi"] = df.apply(lambda r: spans_event(r, CPI), axis=1)
    df["after"] = df.apply(lambda r: day_after_event(r, EVENTS), axis=1)
    p = df["pnl"].values

    print("\n===== %s =====" % label)
    print("  BASELINE all-days      n=%3d  PF=%.2f  total=$%+.0f" % (len(p), pf(p), p.sum()))
    buckets = [("SPANNED any event (crush-harvest)", df["span_any"]),
               ("  spanned FOMC only", df["span_fomc"]),
               ("  spanned CPI only", df["span_cpi"]),
               ("entered DAY-AFTER event (post-crush)", df["after"]),
               ("NON-event control", ~df["span_any"] & ~df["after"])]
    rng = np.random.default_rng(11); n = len(p)
    for name, mask in buckets:
        m = mask.values; sub = p[m]; K = int(m.sum())
        if K == 0:
            print("  %-38s (none)" % name); continue
        null = np.array([pf(p[rng.choice(n, K, replace=False)]) for _ in range(5000)])
        pval = (null >= pf(sub)).mean()   # p(random subset PF >= this bucket's PF)
        print("  %-38s n=%2d  PF=%5.2f  total=$%+6.0f  avg=$%+6.1f  null p=%.3f"
              % (name, K, pf(sub), sub.sum(), sub.mean(), pval))

    # OOS split on the main hypothesis (spanned-event)
    mid = df["ent"].quantile(0.5)
    print("  -- OOS split, SPANNED-event bucket --")
    for tag, sub in [("H1", df["ent"] <= mid), ("H2", df["ent"] > mid)]:
        s = df.loc[sub & df["span_any"], "pnl"]
        print("    %s spanned  n=%2d  PF=%.2f  $%+.0f" % (tag, len(s), pf(s.values) if len(s) else float('nan'), s.sum()))


run("simulations/trades_panel_daily.csv", "REAL all-days ledger (base slippage)")
run("simulations/trades_panel_daily_slip2x.csv", "REAL all-days ledger (2x slippage)")
