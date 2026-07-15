"""Order lifecycle states and Tradier status mapping (spec §7)."""
from dataclasses import dataclass
from enum import Enum


class OrderState(str, Enum):
    SUBMITTED = "submitted"
    OPEN = "open"
    PARTIAL = "partially_filled"
    FILLED = "filled"
    CANCELLED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    TIMEOUT = "timeout"


TERMINAL = {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED,
            OrderState.EXPIRED, OrderState.TIMEOUT}

_MAP = {
    "filled": OrderState.FILLED, "open": OrderState.OPEN,
    "partially_filled": OrderState.PARTIAL, "canceled": OrderState.CANCELLED,
    "cancelled": OrderState.CANCELLED, "rejected": OrderState.REJECTED,
    "expired": OrderState.EXPIRED,
}


def map_broker_status(status: str) -> OrderState:
    """Map a Tradier order status to OrderState; unknown -> OPEN (never silently terminal)."""
    return _MAP.get((status or "").lower(), OrderState.OPEN)


@dataclass
class ExecutionResult:
    """The outcome of submitting one order (open or close leg), spec §8. `open_spread`/
    `close_spread` return this instead of a bare status string so callers can account off the
    ACTUAL fill (price + quantity + fees) rather than the requested/quoted values."""
    status: str
    requested_quantity: int
    filled_quantity: int
    average_fill_price: float | None
    submitted_limit: float | None
    commissions: float = 0.0
    regulatory_fees: float = 0.0
    order_id: str | None = None
    package_mid_at_submission: float | None = None
    package_natural_at_submission: float | None = None
    quote_timestamp: str | None = None
    first_submission_timestamp: str | None = None
    final_fill_timestamp: str | None = None


def result_status(result) -> str:
    """Extract the status string from an open_spread/close_spread return value. Accepts either
    the new ExecutionResult (spec §8) or a legacy bare status string, so any caller/test double
    that still returns a plain str keeps working unmodified."""
    return getattr(result, "status", result)
