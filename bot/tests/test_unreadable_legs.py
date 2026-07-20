"""An unreadable leg array must fail closed, not fall back to the corrupt path (review finding 4).

`normalize_spread_fill` returns None for two very different reasons:

  (a) there is NO leg array at all -- the sandbox shape. Falling back to the order-level fields is
      correct here, and is what keeps Bot B byte-identical.
  (b) a leg array IS present but cannot be read with certainty -- a missing exec_quantity, three
      legs, a nonsensical negative net.

Case (b) only ever happens on LIVE, because only the live broker sends leg arrays. Treating it like
(a) means falling back to `avg_fill_price` (NEGATIVE for a credit on live) and `exec_quantity` (a
LEG COUNT) -- the exact fields that produced a negative stop threshold, a phantom STOP, ~150
rejected stop-closes and a fake -$367 unrealized P&L. Silently, with no halt.

So the two cases are separated: no leg array falls back as before; an unreadable leg array records
NOTHING and halts, because a number we cannot explain is worse than no number.
"""
from bot.features import S2bFeatures
from bot.tests.test_spread_fill import _open_order


def _unreadable_order():
    """A live-shaped order whose legs cannot be netted: the long leg reports no fill quantity.
    The poisonous order-level fields are present and readable -- that is the trap."""
    o = _open_order()
    del o["leg"][1]["exec_quantity"]
    return o


def test_the_fixture_is_genuinely_unreadable():
    """Guard the premise: if the parser ever learns to read this shape, these tests are vacuous."""
    from bot.broker.spread_fill import normalize_spread_fill
    assert normalize_spread_fill(_unreadable_order()) is None
    assert _unreadable_order()["avg_fill_price"] == -0.92     # the corrupt value still sitting there
    assert _unreadable_order()["exec_quantity"] == 2.0        # leg count, not contracts


def test_unreadable_leg_array_records_no_quantity_and_flags_unbalanced():
    from bot.app import wiring
    r = wiring.build_execution_result("filled", _unreadable_order(),
                                     {"quantity[0]": 1, "price": 0.92}, S2bFeatures())
    assert r.filled_quantity == 0, "must not adopt the order-level leg count as a contract count"
    assert r.average_fill_price != -0.92, "must not adopt the negative order-level credit"
    assert r.legs_balanced is False, "must trip the halt rather than pass silently"


def test_sandbox_payload_without_leg_array_still_falls_back_unchanged():
    """The other half of the discrimination, and the Bot B safety claim: no leg array means the
    pre-existing order-level behaviour, untouched."""
    from bot.app import wiring
    sandbox = {"id": "1", "status": "filled", "exec_quantity": 1.0, "avg_fill_price": 0.9}
    r = wiring.build_execution_result("filled", sandbox,
                                     {"quantity[0]": 1, "price": 0.9}, S2bFeatures())
    assert r.filled_quantity == 1
    assert r.average_fill_price == 0.9
    assert r.legs_balanced is True


def test_readable_leg_array_is_unaffected():
    from bot.app import wiring
    r = wiring.build_execution_result("filled", _open_order(),
                                     {"quantity[0]": 1, "price": 0.92}, S2bFeatures())
    assert r.filled_quantity == 1
    assert abs(r.average_fill_price - 0.92) < 1e-9
    assert r.legs_balanced is True
