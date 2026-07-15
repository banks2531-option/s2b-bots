"""Persist BotState across process restarts so a crash/redeploy resumes managing open
positions (instead of orphaning them — the bot-B failure mode). Atomic JSON write."""
import json
import os
from dataclasses import asdict

from bot.app.orchestrator import BotState
from bot.strategy.manage import ManagedPosition


def save_state(state: BotState, path: str) -> None:
    data = {
        "open_positions": [asdict(p) for p in state.open_positions],
        "halted": state.halted,
        "halt_reason": state.halt_reason,
        "last_entry_date": state.last_entry_date,
        "entries_today": state.entries_today,
        "prev_flow_bias": state.prev_flow_bias,
        "credit_ratio_history": state.credit_ratio_history,
        "credit_obs_last": state.credit_obs_last,
        "realized_today": state.realized_today,
        "risk_day": state.risk_day,
        "markout_pending": state.markout_pending,
        "markout_seq": state.markout_seq,
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)        # atomic: never leave a half-written state file


def load_state(path: str) -> BotState:
    """Load persisted state, or a fresh BotState if no file exists yet."""
    if not os.path.exists(path):
        return BotState()
    with open(path) as f:
        d = json.load(f)
    positions = [ManagedPosition(**p) for p in d.get("open_positions", [])]
    return BotState(open_positions=positions,
                    halted=d.get("halted", False),
                    halt_reason=d.get("halt_reason", ""),
                    last_entry_date=d.get("last_entry_date", ""),
                    entries_today=d.get("entries_today", 0),
                    prev_flow_bias=d.get("prev_flow_bias", ""),
                    credit_ratio_history=d.get("credit_ratio_history", {}),
                    credit_obs_last=d.get("credit_obs_last", {}),
                    realized_today=d.get("realized_today", 0.0),
                    risk_day=d.get("risk_day", ""),
                    markout_pending=d.get("markout_pending", []),
                    markout_seq=d.get("markout_seq", 0))
