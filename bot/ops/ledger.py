"""Truth ledger: reconcile bot-tracked state against broker truth (spec §7, C2)."""
from dataclasses import dataclass


def position_key(p):
    """Stable identity of a spread position."""
    return (p.ticker, p.short_strike, p.long_strike, p.expiry)


@dataclass
class DriftReport:
    missing_at_broker: list    # bot thinks open, broker has not (phantom/already-closed)
    untracked_at_broker: list  # broker holds, bot is not tracking (lost track)
    equity_drift: float        # bot_equity - broker_equity

    def positions_match(self) -> bool:
        return not self.missing_at_broker and not self.untracked_at_broker

    def should_halt(self, equity_tolerance: float = 50.0) -> bool:
        return (not self.positions_match()) or abs(self.equity_drift) > equity_tolerance


def reconcile(bot_positions, broker_positions, bot_equity, broker_equity) -> DriftReport:
    bot_keys = {position_key(p): p for p in bot_positions}
    brk_keys = {position_key(p): p for p in broker_positions}
    missing = [bot_keys[k] for k in bot_keys if k not in brk_keys]
    untracked = [brk_keys[k] for k in brk_keys if k not in bot_keys]
    return DriftReport(missing_at_broker=missing, untracked_at_broker=untracked,
                       equity_drift=round(bot_equity - broker_equity, 2))
