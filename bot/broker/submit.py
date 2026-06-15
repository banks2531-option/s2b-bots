"""Submit an order and verify its terminal state, cancelling on timeout (spec §4, §7)."""
from bot.broker.order_state import map_broker_status, TERMINAL, OrderState
from bot.broker.tradier import BrokerError


def submit_and_verify(client, payload, poll_s, timeout_s, now, sleep):
    """Place order, poll until terminal or timeout. On timeout, cancel. Returns (OrderState, order)."""
    oid = client.place_order(payload)
    start = now()
    while True:
        order = client.get_order(oid)
        state = map_broker_status(order.get("status"))
        if state in TERMINAL:
            return state, order
        if now() - start >= timeout_s:
            try:
                client.cancel_order(oid)
            except BrokerError:
                pass  # cancel can fail if the order just filled — re-query below
            final = client.get_order(oid)
            fstate = map_broker_status(final.get("status"))
            if fstate in TERMINAL:
                return fstate, final   # order reached a real terminal state (e.g. FILLED won the race)
            return OrderState.TIMEOUT, final
        sleep(poll_s)
