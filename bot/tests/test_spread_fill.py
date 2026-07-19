"""Leg-level Tradier fill normalization (advisor nextsteps2 section 1).

The order-level fields are unusable on the LIVE broker: avg_fill_price is negative for a credit
and exec_quantity counts legs, not contracts. These tests pin the leg-level reconstruction that
replaces them. Fixtures use real live-shaped payloads, not sandbox ones -- the sandbox omits the
leg array entirely, which is why this bug reached production undetected.
"""
from bot.broker.spread_fill import NormalizedSpreadFill, normalize_spread_fill


def _open_order(short_qty=1, long_qty=1, short_px=1.55, long_px=0.63, oid="9001"):
    """A LIVE-shaped filled bull-put-spread OPENING order: sell the higher-strike put, buy the
    lower-strike put. Note the order-level avg_fill_price is NEGATIVE (-0.92) -- this is the
    real broker behaviour that corrupted recorded credits, and the normalizer must ignore it."""
    return {
        "id": oid, "status": "filled", "class": "multileg",
        "avg_fill_price": -0.92, "exec_quantity": 2.0,   # both poisonous; must be unused
        "leg": [
            {"side": "sell_to_open", "option_symbol": "SPY260731P00630000",
             "quantity": float(short_qty), "exec_quantity": float(short_qty),
             "avg_fill_price": short_px, "status": "filled"},
            {"side": "buy_to_open", "option_symbol": "SPY260731P00625000",
             "quantity": float(long_qty), "exec_quantity": float(long_qty),
             "avg_fill_price": long_px, "status": "filled"},
        ],
    }


def _close_order(short_qty=1, long_qty=1, short_px=0.40, long_px=0.12, oid="9002"):
    """A LIVE-shaped filled CLOSING order: buy back the short put, sell the long put."""
    return {
        "id": oid, "status": "filled", "class": "multileg",
        "avg_fill_price": 0.28, "exec_quantity": 2.0,
        "leg": [
            {"side": "buy_to_close", "option_symbol": "SPY260731P00630000",
             "quantity": float(short_qty), "exec_quantity": float(short_qty),
             "avg_fill_price": short_px, "status": "filled"},
            {"side": "sell_to_close", "option_symbol": "SPY260731P00625000",
             "quantity": float(long_qty), "exec_quantity": float(long_qty),
             "avg_fill_price": long_px, "status": "filled"},
        ],
    }


def test_opening_credit_is_positive_magnitude_from_legs():
    f = normalize_spread_fill(_open_order())
    assert f.cash_flow_type == "credit"
    assert abs(f.net_price - 0.92) < 1e-9      # 1.55 - 0.63, POSITIVE
    assert f.contracts == 1
    assert f.balanced is True
    assert f.source_order_id == "9001"


def test_opening_credit_ignores_negative_order_level_price():
    """The regression that caused ~150 rejected stop-closes and a fake -$367 unrealized P&L."""
    f = normalize_spread_fill(_open_order())
    assert f.net_price > 0, "a credit must never be recorded negative"


def test_contract_count_is_not_the_leg_count():
    """Order-level exec_quantity is 2.0 for a 1-contract vertical. The normalizer must report 1."""
    f = normalize_spread_fill(_open_order())
    assert f.contracts == 1
    assert f.short_leg_qty == 1 and f.long_leg_qty == 1


def test_closing_debit_from_legs():
    f = normalize_spread_fill(_close_order())
    assert f.cash_flow_type == "debit"
    assert abs(f.net_price - 0.28) < 1e-9      # 0.40 - 0.12
    assert f.contracts == 1
    assert f.balanced is True


def test_multi_contract_balanced():
    f = normalize_spread_fill(_open_order(short_qty=3, long_qty=3))
    assert f.contracts == 3
    assert f.balanced is True


def test_unbalanced_legs_flagged_and_counted_by_the_minimum():
    """Never invent a complete spread. Three shorts against two longs is TWO covered spreads and
    one NAKED short -- a materially different risk. Report contracts=2 and balanced=False so the
    caller can halt and reconcile rather than book three spreads."""
    f = normalize_spread_fill(_open_order(short_qty=3, long_qty=2))
    assert f.contracts == 2
    assert f.balanced is False
    assert f.short_leg_qty == 3 and f.long_leg_qty == 2


def test_partial_fill_on_both_legs_is_balanced():
    f = normalize_spread_fill(_open_order(short_qty=2, long_qty=2))
    assert f.contracts == 2 and f.balanced is True


def test_zero_fill_returns_zero_contracts_not_none():
    f = normalize_spread_fill(_open_order(short_qty=0, long_qty=0))
    assert f is not None
    assert f.contracts == 0
    assert f.balanced is True


def test_sandbox_order_without_leg_array_returns_none():
    """The sandbox sends no leg array. Returning None lets the caller fall back to the existing
    order-level behaviour, so Bot B's sandbox accounting is byte-identical to before."""
    assert normalize_spread_fill({"id": "1", "status": "filled", "avg_fill_price": 0.9}) is None


def test_non_dict_returns_none():
    assert normalize_spread_fill(None) is None
    assert normalize_spread_fill("filled") is None


def test_single_leg_order_returns_none():
    """Not a spread; there is nothing to net. Fall back rather than guess."""
    order = {"id": "1", "status": "filled",
             "leg": [{"side": "sell_to_open", "quantity": 1.0, "exec_quantity": 1.0,
                      "avg_fill_price": 1.55, "status": "filled"}]}
    assert normalize_spread_fill(order) is None


def test_missing_leg_price_returns_none():
    """Fail closed: a leg with no fill price cannot be netted. Do not substitute zero -- a zero
    long price would inflate the recorded credit to the full short price."""
    order = _open_order()
    order["leg"][1]["avg_fill_price"] = None
    assert normalize_spread_fill(order) is None


def test_negative_net_price_is_flagged_unbalanced():
    """A bull put spread's short strike is higher, so the short leg is always worth more. A
    negative net means the legs were misidentified or the payload is malformed -- refuse it."""
    f = normalize_spread_fill(_open_order(short_px=0.63, long_px=1.55))
    assert f is None or f.balanced is False
