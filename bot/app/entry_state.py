"""Operational entry states (T9; spec §16, advisor botbnextsteps Step 2).

Answers "what is the bot actually doing right now?" without writing an identical line every polling
cycle. A bot that sits in one state for six hours should produce ONE state record, not 360 -- a log
nobody reads is not observability.

OBSERVABILITY ONLY. Nothing here influences an order decision; the entry path's outcome is an INPUT
to this module, never the reverse.

PURE: no clock (timestamps are injected), no I/O (the sink is injected)."""
from enum import Enum


class EntryState(str, Enum):
    ACTIVE = "active"
    NO_QUALIFIED_CREDIT = "no_qualified_credit"
    NO_RISK_CAPACITY = "no_risk_capacity"
    NO_GAP_CAPACITY = "no_gap_capacity"
    NO_VALID_QUOTES = "no_valid_quotes"
    NO_VALID_EXPIRATION = "no_valid_expiration"
    RISK_OFF = "risk_off"
    DATA_FAILURE = "data_failure"


# Spec §16 precedence, most severe first. Declared explicitly rather than inferred from the enum
# order so the two can be changed independently without silently reordering severity.
STATE_PRECEDENCE = [
    EntryState.DATA_FAILURE,
    EntryState.RISK_OFF,
    EntryState.NO_VALID_EXPIRATION,
    EntryState.NO_VALID_QUOTES,
    EntryState.NO_QUALIFIED_CREDIT,
    EntryState.NO_GAP_CAPACITY,
    EntryState.NO_RISK_CAPACITY,
    EntryState.ACTIVE,
]

# Decision reasons the entry cycle can return -> the state they imply. Reasons NOT listed fall
# through to ACTIVE on purpose: an unrecognized string means "the cycle ran and did something we
# haven't classified", which is far closer to healthy than to a data failure. Mapping the unknown to
# DATA_FAILURE would turn every newly-added decision reason into a phantom outage in the report.
_REASON_STATES = {
    "data_failure": EntryState.DATA_FAILURE,
    "reconcile_failed": EntryState.DATA_FAILURE,
    "trend_paused": EntryState.RISK_OFF,
    "regime_blocked": EntryState.RISK_OFF,
    "risk_off": EntryState.RISK_OFF,
    "no_expiry": EntryState.NO_VALID_EXPIRATION,
    "no_order": EntryState.NO_VALID_EXPIRATION,
    "quote_invalid": EntryState.NO_VALID_QUOTES,
    "quote_wide": EntryState.NO_VALID_QUOTES,
    "quote_stale": EntryState.NO_VALID_QUOTES,
    "credit_too_low": EntryState.NO_QUALIFIED_CREDIT,
    "cost_gate": EntryState.NO_QUALIFIED_CREDIT,
}


def classify_entry_state(reason, limiting_gate=None):
    """Map an entry-cycle outcome to its operational state (spec §16).

    Zero-capacity outcomes are split by which cap bound: a gap-stress scenario means the BOOK cannot
    absorb another shock, while an aggregate budget means the RISK ALLOWANCE is spent. Those call for
    different responses, so the report must not blur them into one "no capacity" bucket."""
    if reason is None:
        return EntryState.ACTIVE
    if str(reason).startswith("risk_budget"):
        gate = limiting_gate or ""
        return (EntryState.NO_GAP_CAPACITY if gate.startswith("gap_")
                else EntryState.NO_RISK_CAPACITY)
    return _REASON_STATES.get(str(reason), EntryState.ACTIVE)


def note_entry_state(state, new_state, now, sink, context=None):
    """Record `new_state`, emitting a transition record ONLY when it differs from the current one.

    Returns True when a transition was emitted. `state` needs `current_entry_state` and
    `entry_state_changed_at` attributes; both are stored as plain strings so they round-trip through
    the JSON state file unchanged."""
    value = new_state.value if isinstance(new_state, EntryState) else str(new_state)
    previous = getattr(state, "current_entry_state", None)
    if previous == value:
        return False
    state.current_entry_state = value
    state.entry_state_changed_at = now.isoformat() if hasattr(now, "isoformat") else str(now)
    if sink is not None:
        record = {"event": "ENTRY_STATE", "previous_state": previous, "new_state": value,
                  "changed_at": state.entry_state_changed_at}
        if context:
            record.update(context)
        sink(record)
    return True
