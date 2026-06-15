"""S2b entry strategy: Monday SPY bull put spread, cushion-aware (spec §3)."""
from datetime import datetime
from dataclasses import dataclass


def is_entry_day(date_str: str) -> bool:
    """True iff date_str (YYYY-MM-DD) is a Monday (the validated S2b entry day)."""
    return datetime.strptime(date_str, "%Y-%m-%d").weekday() == 0


@dataclass
class OptionQuote:
    strike: float
    delta: float    # absolute delta of the put (0..1)
    bid: float
    ask: float


@dataclass
class S2bConfig:
    target_delta: float = 0.35
    wing_width: float = 10.0
    min_cushion_atr: float = 1.0


def select_short_put(chain, spot, atr, cfg):
    """Pick the OTM put nearest target_delta that is also >= min_cushion_atr OTM. None if none qualify."""
    if atr <= 0:
        return None
    eligible = [o for o in chain
                if o.strike < spot and (spot - o.strike) / atr >= cfg.min_cushion_atr]
    if not eligible:
        return None
    return min(eligible, key=lambda o: abs(o.delta - cfg.target_delta))


from bot.risk_gate import SpreadOrder


def build_spread_order(spot, atr, chain, cfg):
    """Select short+long puts and build a bull_put_spread SpreadOrder. None if not buildable."""
    short = select_short_put(chain, spot, atr, cfg)
    if short is None:
        return None
    long_strike = short.strike - cfg.wing_width
    longs = [o for o in chain if o.strike == long_strike]
    if not longs:
        return None
    long = longs[0]
    credit = round(short.bid - long.ask, 2)
    if credit <= 0:
        return None
    max_loss = round((cfg.wing_width - credit) * 100, 2)
    return SpreadOrder(ticker="SPY", structure="bull_put_spread",
                       short_strike=short.strike, long_strike=long_strike,
                       credit=credit, spot=spot, atr=atr,
                       max_loss_per_contract=max_loss, qty=1)
