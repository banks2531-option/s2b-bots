"""A position's whole life through the orchestrator: open -> stop fires -> closed; and the
failure mode: a stop that can't close halts the bot."""
from datetime import datetime
from bot.app.orchestrator import BotState, Deps, tick
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState


def _chain():
    return [OptionQuote(568.0, 0.36, 3.40, 3.50), OptionQuote(565.0, 0.30, 2.80, 2.90),
            OptionQuote(560.0, 0.22, 2.00, 2.10), OptionQuote(558.0, 0.18, 1.60, 1.70)]


def _deps(mark, close_status, broker_positions):
    return Deps(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _chain(), pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01),
        account_state=lambda today, conc: AccountState(20_000.0, 20_000.0, 0.0, conc, 0.0, {}, today),
        mark_position=mark, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: close_status,
        broker_positions=broker_positions, broker_equity=lambda: 20_000.0,
        bot_equity=lambda: 20_000.0, alert_sink=lambda alerts: None,
    )


def test_open_then_stop_closes_clean():
    state = BotState()
    # tick 1: Monday 10:05, flat, mark irrelevant (no positions) -> opens
    d_open = _deps(mark=lambda p: 3.0, close_status="filled", broker_positions=lambda: [])
    state = tick(state, d_open, datetime(2026, 6, 15, 10, 5))
    assert len(state.open_positions) == 1

    # tick 2 (a later day): the spread blew out past the stop; broker confirms the position; close fills
    held = list(state.open_positions)
    d_stop = _deps(mark=lambda p: 10.0, close_status="filled", broker_positions=lambda: held)
    state = tick(state, d_stop, datetime(2026, 6, 17, 11, 0))   # Wednesday
    assert state.open_positions == [] and state.halted is False


def test_open_then_stop_cannot_close_halts():
    state = BotState()
    d_open = _deps(mark=lambda p: 3.0, close_status="filled", broker_positions=lambda: [])
    state = tick(state, d_open, datetime(2026, 6, 15, 10, 5))
    held = list(state.open_positions)

    # stop triggers but the close does NOT fill -> position retained, bot halted (the bot-B fix, top level)
    d_fail = _deps(mark=lambda p: 10.0, close_status="timeout", broker_positions=lambda: held)
    state = tick(state, d_fail, datetime(2026, 6, 17, 11, 0))
    assert state.halted is True and len(state.open_positions) == 1
