# S2b Dashboard Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the stale 3-bot dashboard with a 2-bot (B + C), Codex-sourced, at-a-glance dashboard served as a separate password-protected site on the existing monarch VPS.

**Architecture:** A pure Python build script (`dashboard/build_dashboard.py`, in the fable-options repo) reads the Codex report files on the bot droplet and writes one self-contained `dashboard.html`, on the `gen_report` cron. The monarch VPS pulls that HTML on a timer and Caddy serves it at a separate obscure subdomain behind its own `basic_auth` — fully independent of the finance app.

**Tech Stack:** Python 3 stdlib only (json, csv, html, datetime, statistics) — no new deps. Caddy + systemd on the VPS. Tests via pytest, imported by file path like `bot/tests/test_gen_report.py`.

**Data shapes (already produced by `reports/gen_report.py`):**
- `reports/latest/bot_{b,c}_performance.json`: keys incl. `equity, realized_today, realized_risk_day, realized_report_date, realized_is_stale, risk_day, report_date, lifetime_realized, halted, entries_today, funnel{entry_cycles_started,raw_candidate_evaluations,unique_candidate_opportunities}, positions[], unrealized, opens_today[], closes_today[], decisions_today, gate_breakdown{}, missed_opportunities{}, config{}, pnl_source, pnl_validation_status, history{total_realized,closed_trades,wins,losses,win_rate_pct,profit_factor,best,worst,by_day{date:pnl}}`.
- `reports/latest/broker_snapshot_{b,c}.json`: `account, equity, legs[{symbol,quantity,cost_basis,ownership}], pending_orders[], reconciliation{owned_legs,foreign_legs,note}`.
- `reports/latest/market_context.json`: `spot, daily{open,high,low,close}, atr14, vix, session_vwap, price_vs_vwap_pts, vwap_bars_out_of_range, expected_move_1d_pts, opening_range{}`.
- `reports/advisor_memory.md`: markdown table `| Date | Recommendation | Reason | Confidence | Status | Implemented | Outcome |` (skip the two `_example_` rows).

---

## Task 1: Scaffold + data loading

**Files:**
- Create: `dashboard/build_dashboard.py`
- Test: `bot/tests/test_build_dashboard.py`

- [ ] **Step 1: Write the failing test for the module import + loader**

```python
# bot/tests/test_build_dashboard.py
import importlib.util, json, os
_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "dashboard", "build_dashboard.py"))
_spec = importlib.util.spec_from_file_location("build_dashboard", _PATH)
bd = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bd)

def _write_fixture(tmp_path):
    latest = tmp_path / "latest"; latest.mkdir()
    (latest / "bot_c_performance.json").write_text(json.dumps({
        "bot": "Bot C", "equity": 900.0, "realized_report_date": 0, "realized_is_stale": True,
        "risk_day": "2026-07-17", "report_date": "2026-07-22", "lifetime_realized": -17.0,
        "pnl_source": "synthetic_mark", "pnl_validation_status": "live_fill_accounting_disabled",
        "entries_today": 0, "opens_today": [], "closes_today": [{"pnl": "26"}],
        "funnel": {"entry_cycles_started": 1091, "unique_candidate_opportunities": 219},
        "gate_breakdown": {"risk_budget:gap_1atr": 234, "off_hours": 29},
        "positions": [], "unrealized": 0,
        "history": {"total_realized": -17.0, "closed_trades": 15, "wins": 10, "losses": 5,
                    "win_rate_pct": 66.7, "profit_factor": 0.86,
                    "by_day": {"2026-07-15": 11.0, "2026-07-17": -14.6}}}))
    (latest / "broker_snapshot_c.json").write_text(json.dumps({"account": "6YB7****", "legs": [],
        "pending_orders": [], "reconciliation": {"owned_legs": 0, "foreign_legs": 0}}))
    # minimal bot_b twins
    (latest / "bot_b_performance.json").write_text(json.dumps({"bot": "Bot B", "equity": 74000.0,
        "realized_report_date": 9.0, "realized_is_stale": False, "risk_day": "2026-07-22",
        "report_date": "2026-07-22", "lifetime_realized": 4077.0, "pnl_source": "actual_fill",
        "pnl_validation_status": "sandbox_unaudited", "entries_today": 1,
        "opens_today": [{}], "closes_today": [{"pnl": "138.8"}],
        "funnel": {"entry_cycles_started": 1101, "unique_candidate_opportunities": 228},
        "gate_breakdown": {"duplicate_strikes": 295, "risk_budget:gap_2atr": 33, "filled": 1},
        "positions": [{}, {}, {}], "unrealized": 9,
        "history": {"total_realized": 4077.0, "closed_trades": 22, "wins": 19, "losses": 3,
                    "win_rate_pct": 86.4, "profit_factor": 2.07,
                    "by_day": {"2026-07-02": -2289.0, "2026-07-21": 293.6, "2026-07-22": 138.8}}}))
    (latest / "broker_snapshot_b.json").write_text(json.dumps({"account": "VA47****",
        "legs": [], "pending_orders": [], "reconciliation": {"owned_legs": 4, "foreign_legs": 2}}))
    (latest / "market_context.json").write_text(json.dumps({"spot": 747.41,
        "daily": {"open": 746.62, "high": 750.02, "low": 746.37, "close": 747.41},
        "vix": 16.6, "session_vwap": 748.30, "price_vs_vwap_pts": -0.94}))
    am = tmp_path / "advisor_memory.md"
    am.write_text("| Date | Recommendation | Reason (evidence) | Confidence | Status | Implemented | Outcome |\n"
                  "|---|---|---|---|---|---|---|\n"
                  "| 2026-07-22 | Do X | because Y | Very High | Pending | - | - |\n"
                  "| _example_ | ignore | - | Moderate | Pending | - | - |\n")
    return str(latest), str(am)

def test_load_reports_pulls_both_bots_and_market(tmp_path):
    latest, am = _write_fixture(tmp_path)
    d = bd.load_reports(latest, am)
    assert d["b"]["perf"]["bot"] == "Bot B"
    assert d["c"]["perf"]["bot"] == "Bot C"
    assert d["market"]["spot"] == 747.41
    assert d["b"]["broker"]["reconciliation"]["foreign_legs"] == 2
```

- [ ] **Step 2: Run it — expect FAIL** (`no attribute 'load_reports'`)

Run: `python -m pytest bot/tests/test_build_dashboard.py -q`

- [ ] **Step 3: Implement the module header + `load_reports`**

```python
#!/usr/bin/env python3
"""S2b dashboard builder. Reads the Codex report files and writes a self-contained dashboard.html.
Runs on the bot droplet (hooked into gen_report cron). Pure stdlib; no external requests."""
import os, json, csv, html, statistics
from datetime import datetime

def _load_json(path):
    try:
        with open(path) as fh: return json.load(fh)
    except Exception:
        return {}

def load_reports(reports_dir, advisor_memory_path):
    out = {"market": _load_json(os.path.join(reports_dir, "market_context.json"))}
    for k in ("b", "c"):
        out[k] = {"perf": _load_json(os.path.join(reports_dir, "bot_%s_performance.json" % k)),
                  "broker": _load_json(os.path.join(reports_dir, "broker_snapshot_%s.json" % k))}
    try:
        with open(advisor_memory_path) as fh:
            out["recommendations"] = parse_recommendations(fh.read())
    except Exception:
        out["recommendations"] = []
    return out
```
(Note: `parse_recommendations` is defined in Task 4; until then the try/except returns `[]`.)

- [ ] **Step 4: Run test — expect PASS**
- [ ] **Step 5: Commit** — `git add dashboard/build_dashboard.py bot/tests/test_build_dashboard.py && git commit -m "dashboard: scaffold build_dashboard + load_reports"`

---

## Task 2: `daily_pnl_series` (historical daily P&L, corrupt-data-aware)

**Files:** Modify `dashboard/build_dashboard.py`; Test `bot/tests/test_build_dashboard.py`

- [ ] **Step 1: Write failing tests**

```python
def test_daily_pnl_series_cumulative_and_sorted():
    perf = {"history": {"by_day": {"2026-07-22": 138.8, "2026-07-21": 293.6, "2026-07-02": -2289.0}}}
    s = bd.daily_pnl_series(perf, "c")   # non-B: no exclusion
    assert [r["date"] for r in s] == ["2026-07-02", "2026-07-21", "2026-07-22"]
    assert s[-1]["cumulative"] == round(-2289.0 + 293.6 + 138.8, 2)

def test_daily_pnl_series_excludes_corrupt_bot_b_pre_0720():
    perf = {"history": {"by_day": {"2026-07-15": 1742.4, "2026-07-20": -1104.0, "2026-07-22": 138.8}}}
    s = bd.daily_pnl_series(perf, "b")   # Bot B: drop dates < 2026-07-20
    assert [r["date"] for r in s] == ["2026-07-20", "2026-07-22"]
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement**

```python
BOT_B_CLEAN_FROM = "2026-07-20"   # Bot B history before this is sandbox-corrupted (see gen_report footer)

def daily_pnl_series(perf, bot_key):
    by_day = ((perf or {}).get("history") or {}).get("by_day") or {}
    items = sorted(by_day.items())
    if bot_key == "b":
        items = [(d, v) for d, v in items if d >= BOT_B_CLEAN_FROM]
    out, cum = [], 0.0
    for d, v in items:
        cum += float(v)
        out.append({"date": d, "realized": round(float(v), 2), "cumulative": round(cum, 2)})
    return out
```

- [ ] **Step 4: Run — expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "dashboard: daily_pnl_series (excludes corrupt Bot B pre-07-20)"`

---

## Task 3: `health_verdict` (the evolving assessment)

**Files:** Modify `dashboard/build_dashboard.py`; Test `bot/tests/test_build_dashboard.py`

Rules (transparent, order matters):
- No clean daily data → `Insufficient data`.
- `entries_today == 0` AND the top gate is a `risk_budget:*` block AND no positions → `Stalled` ("not entering — risk budget capping it").
- Else classify by recent trend: sum of last ≤5 clean days > 0 and win_rate ≥ 60 → `Improving`; recent sum < 0 or win_rate < 45 → `Under pressure`; otherwise `Steady`.
- Narrative always appends the edge caveat + sample note; Bot C also notes synthetic P&L.

- [ ] **Step 1: Write failing tests**

```python
def test_health_stalled_when_not_trading_and_risk_capped():
    perf = {"entries_today": 0, "positions": [], "pnl_source": "synthetic_mark",
            "gate_breakdown": {"risk_budget:gap_1atr": 234, "off_hours": 29},
            "history": {"by_day": {"2026-07-17": -14.6}, "win_rate_pct": 66.7, "profit_factor": 0.86}}
    v = bd.health_verdict(perf, "c")
    assert v["status"] == "Stalled"
    assert "risk budget" in v["narrative"].lower()
    assert "synthetic" in v["narrative"].lower()

def test_health_improving_when_recent_green_and_high_winrate():
    perf = {"entries_today": 1, "positions": [{}], "pnl_source": "actual_fill",
            "gate_breakdown": {"filled": 1, "duplicate_strikes": 295},
            "history": {"by_day": {"2026-07-20": 40.0, "2026-07-21": 293.6, "2026-07-22": 138.8},
                        "win_rate_pct": 86.4, "profit_factor": 2.07}}
    v = bd.health_verdict(perf, "b")
    assert v["status"] == "Improving"

def test_health_insufficient_when_no_clean_days():
    v = bd.health_verdict({"history": {"by_day": {}}}, "c")
    assert v["status"] == "Insufficient data"
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement**

```python
def _top_gate(perf):
    gb = (perf or {}).get("gate_breakdown") or {}
    blocks = {k: v for k, v in gb.items() if k not in ("filled", "off_hours")}
    return max(blocks, key=blocks.get) if blocks else None

def health_verdict(perf, bot_key):
    series = daily_pnl_series(perf, bot_key)
    hist = (perf or {}).get("history") or {}
    wr = hist.get("win_rate_pct")
    pf = hist.get("profit_factor")
    synth = (perf or {}).get("pnl_source") == "synthetic_mark"
    if not series:
        return {"status": "Insufficient data",
                "narrative": "Not enough clean trading days yet to judge the strategy."
                             + (" P&L is synthetic (live-fill accounting off)." if synth else "")}
    recent = series[-5:]
    recent_sum = round(sum(r["realized"] for r in recent), 2)
    top = _top_gate(perf)
    if (perf.get("entries_today") == 0 and not (perf.get("positions") or [])
            and top and top.startswith("risk_budget")):
        status = "Stalled"
        why = ("Not opening trades — the risk budget (%s) is capping every candidate." % top)
    elif recent_sum > 0 and (wr or 0) >= 60:
        status = "Improving"
        why = ("Up over the last %d trading days (%+.0f) with a %.0f%% win rate." % (len(recent), recent_sum, wr or 0))
    elif recent_sum < 0 or (wr is not None and wr < 45):
        status = "Under pressure"
        why = ("Down over the last %d trading days (%+.0f); win rate %.0f%%." % (len(recent), recent_sum, wr or 0))
    else:
        status = "Steady"
        why = ("Roughly flat over the last %d trading days (%+.0f)." % (len(recent), recent_sum))
    caveat = (" Edge note: Monday-only is the validated schedule; all-days is diluted."
              " Small sample — treat as directional.")
    if synth:
        caveat += " Bot C P&L is synthetic until live-fill accounting is validated."
    return {"status": status, "narrative": why + caveat}
```

- [ ] **Step 4: Run — expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "dashboard: health_verdict evolving assessment"`

---

## Task 4: `parse_recommendations` (Codex ledger → rows)

**Files:** Modify `dashboard/build_dashboard.py`; Test `bot/tests/test_build_dashboard.py`

- [ ] **Step 1: Write failing test**

```python
def test_parse_recommendations_splits_and_skips_examples():
    md = ("| Date | Recommendation | Reason (evidence) | Confidence | Status | Implemented | Outcome |\n"
          "|---|---|---|---|---|---|---|\n"
          "| 2026-07-22 | Export cap fields | blank telemetry | Very High | Pending | - | - |\n"
          "| 2026-07-19 | Book reconcile P&L | drift | High | Implemented | 2026-07-20 | fixed |\n"
          "| _example_ | ignore me | - | Moderate | Pending | - | - |\n")
    rows = bd.parse_recommendations(md)
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-07-22" and rows[0]["status"] == "Pending"
    assert rows[1]["implemented"] == "2026-07-20"
    assert all("example" not in r["date"].lower() for r in rows)
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement**

```python
_REC_FIELDS = ["date", "recommendation", "reason", "confidence", "status", "implemented", "outcome"]

def parse_recommendations(md_text):
    rows = []
    for line in (md_text or "").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 7:
            continue
        if cells[0].lower() in ("date", ":---", "---") or cells[0].startswith("_example_") or set(cells[0]) <= {"-", ":"}:
            continue
        rows.append(dict(zip(_REC_FIELDS, cells[:7])))
    return rows
```

- [ ] **Step 4: Run — expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "dashboard: parse_recommendations from advisor_memory"`

---

## Task 5: `why_line` (plain-English per-bot reason)

**Files:** Modify `dashboard/build_dashboard.py`; Test `bot/tests/test_build_dashboard.py`

- [ ] **Step 1: Write failing tests**

```python
def test_why_line_traded():
    perf = {"opens_today": [{}], "closes_today": [{}, {}], "entries_today": 1,
            "gate_breakdown": {"filled": 1, "risk_budget:gap_2atr": 33}}
    assert "opened 1" in bd.why_line(perf).lower()

def test_why_line_stalled_names_the_gate():
    perf = {"opens_today": [], "closes_today": [], "entries_today": 0, "positions": [],
            "gate_breakdown": {"risk_budget:gap_1atr": 234, "off_hours": 29}}
    line = bd.why_line(perf).lower()
    assert "no new" in line and "gap_1atr" in line
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement**

```python
def why_line(perf):
    opens = len((perf or {}).get("opens_today") or [])
    closes = len((perf or {}).get("closes_today") or [])
    top = _top_gate(perf)
    if opens or closes:
        return "Traded today: opened %d, closed %d." % (opens, closes) + (
            " Main limiter on new entries: %s." % top if top else "")
    if top and top.startswith("risk_budget"):
        return "No new trades — risk budget (%s) blocked every candidate." % top
    if top:
        return "No new trades — top blocker was %s." % top
    return "No new trades today."
```

- [ ] **Step 4: Run — expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "dashboard: why_line generator"`

---

## Task 6: `render_html` (3 tabs) + smoke test

**Files:** Modify `dashboard/build_dashboard.py`; Test `bot/tests/test_build_dashboard.py`

Renders a single self-contained page (inline CSS/JS tab switcher, no external requests), theme-neutral dark, mobile-first. Content per the design spec:
- **Header/market strip:** date, SPY close/%, VIX, session VWAP, one-line regime.
- **Tab Overview:** two bot cards (B, C) — health status badge + narrative, why-line, and the KPI grid (equity, today realized w/ *synthetic* tag for C, lifetime, win%, open/opened/closed, pnl_source badge); per-bot daily-P&L sparkline (inline SVG from `daily_pnl_series`).
- **Tab Performance:** per bot — daily P&L table (date/realized/cumulative) + inline SVG cumulative line; win/loss/PF/best/worst; funnel + gate_breakdown table; reconciliation (owned/foreign); risk-config snapshot (from `perf["config"]`).
- **Tab Codex Recs:** `recommendations` split into Pending vs Implemented tables.
- Must contain the string "Bot B" and "Bot C" and MUST NOT contain "Bot A".

- [ ] **Step 1: Write failing smoke test**

```python
def test_render_html_two_bots_no_bot_a_and_has_tabs(tmp_path):
    latest, am = _write_fixture(tmp_path)
    data = bd.load_reports(latest, am)
    doc = bd.render_html(data, generated_at="2026-07-22T17:00")
    assert doc.lstrip().lower().startswith("<!doctype html")
    assert "Bot B" in doc and "Bot C" in doc
    assert "Bot A" not in doc
    for tab in ("Overview", "Performance", "Recommendations"):
        assert tab in doc
    assert "synthetic" in doc.lower()          # Bot C provenance surfaced
    assert "Do X" in doc                        # recommendation row rendered
    assert "http://" not in doc and "https://" not in doc   # self-contained, no external refs
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement `render_html(data, generated_at)`**

Build with a list of HTML fragments joined at the end. Use a small inline `<style>` (dark theme, cards, tables, `.badge`, tab CSS) and a tiny inline `<script>` tab switcher (buttons toggle `.tab` sections by id — no external JS). Helper `esc = html.escape`. Money helper formats `$+/-,.0f` with pos/neg color classes. Sparkline/line = inline `<svg>` polylines computed from `daily_pnl_series`. Pull card fields from `perf`, `health_verdict(perf, key)`, `why_line(perf)`. Render the recs tab from `data["recommendations"]` split on `status == "Implemented"`. (Full content per `docs/superpowers/specs/2026-07-22-s2b-dashboard-redesign-design.md`; the assertions above are the acceptance contract.)

- [ ] **Step 4: Run — expect PASS**; also eyeball once: `python dashboard/build_dashboard.py --self-test-open` (writes to a temp file) is optional.
- [ ] **Step 5: Commit** — `git commit -am "dashboard: render_html three-tab page"`

---

## Task 7: `build()` entrypoint + CLI

**Files:** Modify `dashboard/build_dashboard.py`; Test `bot/tests/test_build_dashboard.py`

- [ ] **Step 1: Write failing test**

```python
def test_build_writes_html_file(tmp_path):
    latest, am = _write_fixture(tmp_path)
    out = tmp_path / "dashboard.html"
    bd.build(latest, am, str(out))
    doc = out.read_text()
    assert "Bot C" in doc and "Bot A" not in doc
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement**

```python
def build(reports_dir, advisor_memory_path, out_path):
    data = load_reports(reports_dir, advisor_memory_path)
    generated_at = datetime.now().strftime("%Y-%m-%dT%H:%M")
    doc = render_html(data, generated_at=generated_at)
    tmp = out_path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(doc)
    os.replace(tmp, out_path)

if __name__ == "__main__":
    BASE = "/root/s2b-bot"
    build(BASE + "/reports/latest", BASE + "/reports/advisor_memory.md",
          BASE + "/reports/latest/dashboard.html")
    print("wrote dashboard.html")
```

- [ ] **Step 4: Run — expect PASS**; then full suite `python -m pytest -q` — expect all green.
- [ ] **Step 5: Commit** — `git commit -am "dashboard: build() entrypoint + CLI"`

---

## Task 8: VPS deploy config (Caddy block + pull timer + runbook)

**Files:**
- Create: `deploy/s2b-dashboard.Caddyfile` (site block snippet)
- Create: `deploy/s2b-dashboard-pull.service`, `deploy/s2b-dashboard-pull.timer`
- Create: `deploy/S2B-DASHBOARD-SETUP.md` (runbook)

- [ ] **Step 1: Write the Caddy site block** (`deploy/s2b-dashboard.Caddyfile`)

```
# S2b dashboard — SEPARATE site from the finance app. Replace the host + hash.
s2b-CHANGEME.yourdomain.com {
    basic_auth {
        s2b JELLY_REPLACE_WITH_caddy_hash-password_OUTPUT
    }
    root * /var/www/s2b
    file_server
}
```

- [ ] **Step 2: Write the pull service + timer**

`deploy/s2b-dashboard-pull.service`:
```
[Unit]
Description=Pull S2b dashboard.html from the bot droplet
[Service]
Type=oneshot
ExecStart=/usr/bin/scp -o BatchMode=yes -i /home/monarch/.ssh/s2b_pull root@159.89.45.162:/root/s2b-bot/reports/latest/dashboard.html /var/www/s2b/dashboard.html
```

`deploy/s2b-dashboard-pull.timer`:
```
[Unit]
Description=Pull S2b dashboard every 15 min
[Timer]
OnCalendar=*:0/15
Persistent=true
[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Write the runbook** (`deploy/S2B-DASHBOARD-SETUP.md`) — exact VPS steps: (1) `sudo mkdir -p /var/www/s2b`; (2) generate an SSH keypair on the VPS (`ssh-keygen -t ed25519 -f ~/.ssh/s2b_pull`), add the PUBLIC key to the bot droplet's `/root/.ssh/authorized_keys` **restricted** to the reports dir via a forced-command wrapper; (3) `caddy hash-password` → paste into the Caddyfile block; pick the obscure subdomain + add its DNS A record; (4) `sudo cp deploy/s2b-dashboard.Caddyfile` into a `/etc/caddy/` import and `systemctl reload caddy`; (5) install + enable the pull timer; (6) verify `https://<subdomain>` prompts for the password and shows the dashboard. Note: this is entirely separate from the finance site block.

- [ ] **Step 4: Commit** — `git add deploy/s2b-dashboard* deploy/S2B-DASHBOARD-SETUP.md && git commit -m "deploy: S2b dashboard Caddy block, pull timer, runbook"`

---

## Task 9: Deploy the builder to the bot droplet + hook the cron

**Files:** (droplet operations — no repo change beyond a note)

- [ ] **Step 1** Pre-flight: confirm `reports/latest/*.json` + `advisor_memory.md` exist on the droplet (they do — gen_report writes them).
- [ ] **Step 2** `scp dashboard/build_dashboard.py root@159.89.45.162:/root/s2b-bot/dashboard/build_dashboard.py` (create the dir if needed).
- [ ] **Step 3** Manual build on droplet: `cd /root/s2b-bot && ./venv/bin/python dashboard/build_dashboard.py && ls -la reports/latest/dashboard.html`. Expect the file written.
- [ ] **Step 4** Hook the cron: append `&& ./venv/bin/python dashboard/build_dashboard.py` to the existing `gen_report.py` cron line (so the dashboard rebuilds right after each data pass), OR add a second line after it. Verify `crontab -l`.
- [ ] **Step 5** No repo commit needed here (droplet op); record the deploy in the session notes / memory.

---

## Self-review

- **Spec coverage:** Bot A removed (Task 6 asserts absent) ✓; Codex-sourced (Task 1) ✓; 3 tabs (Task 6) ✓; health verdict (Task 3) ✓; historical daily P&L incl. 7/22 (Task 2, fixture includes 07-22) ✓; recommendations tab (Task 4/6) ✓; why-line (Task 5) ✓; separate-subdomain static + pull (Tasks 8/9) ✓.
- **Placeholders:** the Caddyfile/runbook use CHANGEME markers by design (host/hash are user secrets) — documented, not code gaps.
- **Type consistency:** `daily_pnl_series(perf, bot_key)`, `health_verdict(perf, bot_key)`, `why_line(perf)`, `parse_recommendations(md)`, `load_reports(dir, path)`, `render_html(data, generated_at)`, `build(dir, path, out)` — consistent across tasks; `_top_gate` shared by Tasks 3 & 5.
