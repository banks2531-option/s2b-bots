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
    recent_losses: dict              # ticker -> sessions since last loss in that ticker
    current_date: str


@dataclass
class RiskConfig:
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
        return Decision(True, "ok")
