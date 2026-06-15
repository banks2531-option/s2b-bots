from bot.broker.order_state import OrderState, TERMINAL, map_broker_status


def test_map_known_statuses():
    assert map_broker_status("filled") == OrderState.FILLED
    assert map_broker_status("open") == OrderState.OPEN
    assert map_broker_status("partially_filled") == OrderState.PARTIAL
    assert map_broker_status("canceled") == OrderState.CANCELLED
    assert map_broker_status("rejected") == OrderState.REJECTED
    assert map_broker_status("expired") == OrderState.EXPIRED


def test_unknown_status_maps_to_open():
    # unknown/pending -> treat as still-working (OPEN), never silently terminal
    assert map_broker_status("pending") == OrderState.OPEN
    assert map_broker_status("") == OrderState.OPEN


def test_terminal_set():
    assert OrderState.FILLED in TERMINAL
    assert OrderState.OPEN not in TERMINAL
    assert OrderState.TIMEOUT in TERMINAL
