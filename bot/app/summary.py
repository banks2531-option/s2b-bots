"""Daily A/B summary: per-bot realized P&L / win-rate / profit-factor from the trade logs.
Run by cron after market close; writes ab_summary.txt (latest) + ab_summary_history.csv."""
import csv
import json
import os
from datetime import datetime, timezone


def summarize_trades(rows) -> dict:
    """rows: list of trade-record dicts (from trades_<bot>.csv). Stats over CLOSE rows with a pnl."""
    pnls = [float(r["pnl"]) for r in rows
            if r.get("event") == "CLOSE" and r.get("pnl") not in (None, "")]
    wins = [p for p in pnls if p > 0]
    gp = sum(wins)
    gl = -sum(p for p in pnls if p <= 0)
    pf = round(gp / gl, 2) if gl > 0 else (float("inf") if gp > 0 else 0.0)
    return {
        "closed": len(pnls),
        "wins": len(wins),
        "win_rate": round(100 * len(wins) / len(pnls), 1) if pnls else 0.0,
        "net": round(sum(pnls), 2),
        "pf": pf,
    }


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _open_count(state_path):
    if not os.path.exists(state_path):
        return 0
    return len(json.load(open(state_path)).get("open_positions", []))


def bot_summary(tag):
    s = summarize_trades(_read_csv(f"trades_{tag}.csv"))
    s["open"] = _open_count(f"state_{tag}.json")
    return s


def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = [("MONDAY", bot_summary("monday")), ("ALL-DAYS", bot_summary("alldays"))]
    lines = [f"=== S2b A/B summary {ts} ==="]
    for label, s in rows:
        lines.append(f"{label:9s} closed={s['closed']:>3}  win%={s['win_rate']:>5.1f}  "
                     f"net=${s['net']:>9.2f}  PF={s['pf']!s:>5}  open={s['open']}")
    out = "\n".join(lines)
    print(out)
    with open("ab_summary.txt", "w") as f:
        f.write(out + "\n")
    hist = "ab_summary_history.csv"
    new = not os.path.exists(hist)
    with open(hist, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "bot", "closed", "wins", "win_rate", "net", "pf", "open"])
        for label, s in rows:
            w.writerow([ts, label, s["closed"], s["wins"], s["win_rate"], s["net"], s["pf"], s["open"]])
    return out


if __name__ == "__main__":
    main()
