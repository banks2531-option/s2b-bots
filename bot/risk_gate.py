"""Single pre-trade risk chokepoint for the S2b bot (spec §7, §9)."""
from dataclasses import dataclass, field

ALLOWED_STRUCTURES = {"bull_put_spread", "bear_call_spread", "iron_condor"}


@dataclass
class SpreadOrder:
    ticker: str
    structure: str
    short_strike: float
    long_strike: float
    credit: float
    spot: float
    atr: float                       # ATR(14) of the underlying
    max_loss_per_contract: float     # dollars
    qty: int


@dataclass
class AccountState:
    equity: float
    settled_cash: float              # cash-account settled funds available
    open_risk: float                 # sum of max-loss across open positions ($)
    concurrent_positions: int
    realized_pnl_today: float
    recent_losses: dict[str, int]   # ticker -> sessions since last loss in that ticker
    current_date: str


@dataclass
class RiskConfig:
    """Risk configuration for the S2b bot.

    Note: iron_condor cushion is deferred to the strategy; it is not gated here.
    """
    max_risk_pct: float = 0.10
    max_total_risk_pct: float = 0.30
    max_concurrent: int = 3
    daily_loss_halt_pct: float = 0.02
    cushion_min_atr: float = 1.0
    cooldown_sessions: int = 5
    allowed_structures: set = field(default_factory=lambda: set(ALLOWED_STRUCTURES))


@dataclass
class Decision:
    allowed: bool
    reason: str = "ok"


class RiskGate:
    def __init__(self, config: RiskConfig):
        self.cfg = config

    def is_order_allowed(self, order: SpreadOrder, state: AccountState) -> Decision:
        if order.structure not in self.cfg.allowed_structures:
            return Decision(False, f"structure {order.structure} not allowed")
        # C1: zero/negative ATR is a hard reject for credit spreads (cushion check requires it)
        if order.structure in ("bull_put_spread", "bear_call_spread") and order.atr <= 0:
            return Decision(False, "atr must be > 0 for cushion check")
        cushion = self._cushion_atr(order)
        if cushion is not None and cushion < self.cfg.cushion_min_atr:
            return Decision(False, f"cushion {cushion:.2f} ATR < {self.cfg.cushion_min_atr}")
        trade_risk = order.max_loss_per_contract * order.qty
        if trade_risk > state.equity * self.cfg.max_risk_pct + 1e-9:
            return Decision(False, "per-trade risk exceeds cap")
        if state.open_risk + trade_risk > state.equity * self.cfg.max_total_risk_pct + 1e-9:
            return Decision(False, "total open risk exceeds cap")
        if state.concurrent_positions >= self.cfg.max_concurrent:
            return Decision(False, "max concurrent positions reached")
        # halt at OR beyond the loss threshold (conservative)
        if state.realized_pnl_today <= -self.cfg.daily_loss_halt_pct * state.equity:
            return Decision(False, "daily loss halt active")
        if trade_risk > state.settled_cash + 1e-9:
            return Decision(False, "insufficient settled cash")
        sessions_since = state.recent_losses.get(order.ticker)
        if sessions_since is not None and sessions_since < self.cfg.cooldown_sessions:
            return Decision(False, f"{order.ticker} in cooldown")
        return Decision(True, "ok")

    def _cushion_atr(self, order: SpreadOrder):
        """Distance from spot to short strike, in ATRs. None if not applicable.

        For bull_put_spread and bear_call_spread, atr <= 0 is rejected upstream
        in is_order_allowed before this method is called.
        """
        if order.structure == "bull_put_spread":
            dist = order.spot - order.short_strike
        elif order.structure == "bear_call_spread":
            dist = order.short_strike - order.spot
        else:
            # iron_condor: per-side cushion is the strategy's responsibility,
            # intentionally not gated here
            return None
        return dist / order.atr
