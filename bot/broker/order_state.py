"""Order lifecycle states and Tradier status mapping (spec §7)."""
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
