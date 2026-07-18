"""S2b entry strategy: Monday SPY bull put spread, cushion-aware (spec §3)."""
from datetime import datetime
from dataclasses import dataclass
from bot.risk_gate import SpreadOrder


def is_entry_day(date_str: str) -> bool:
    """True iff date_str (YYYY-MM-DD) is a Monday (the validated S2b entry day)."""
    return datetime.strptime(date_str, "%Y-%m-%d").weekday() == 0


@dataclass
class OptionQuote:
    strike: float
    delta: float    # absolute delta of the put (0..1)
    bid: float
    ask: float
    # Advisor Step 1A/1B. All optional with None defaults so existing positional constructions stay
    # valid and a feed that cannot supply them degrades to fail-closed rather than breaking.
    exchange_timestamp: object = None   # datetime | None -- broker/exchange quote time, PREFERRED
    received_timestamp: object = None   # datetime | None -- local receive time, fallback. Never
                                         # defaulted to "now": a synthesized timestamp would make
                                         # stale cached data read as fresh.
    iv: float = None                    # mid implied vol for this strike, feeds the ATM IV /
                                         # expected-move calculation


@dataclass
class S2bConfig:
    target_delta: float = 0.35
    wing_width: float = 10.0
    min_cushion_atr: float = 1.0
    min_delta: float = 0.20
    max_delta: float = 0.50


def _strike_key(strike):
    """Integer key for strike comparison — avoids float-equality bugs."""
    return int(round(round(strike, 3) * 1000))


def select_short_put(chain, spot, atr, cfg):
    """Pick the OTM put nearest target_delta that is also >= min_cushion_atr OTM. None if none qualify."""
    if atr <= 0:
        return None
    eligible = [o for o in chain
                if o.strike < spot
                and (spot - o.strike) / atr >= cfg.min_cushion_atr
                and cfg.min_delta <= o.delta <= cfg.max_delta]
    if not eligible:
        return None
    return min(eligible, key=lambda o: abs(o.delta - cfg.target_delta))


def build_spread_order(spot, atr, chain, cfg, ticker="SPY"):
    """Select short+long puts and build a bull_put_spread SpreadOrder. None if not buildable."""
    short = select_short_put(chain, spot, atr, cfg)
    if short is None:
        return None
    long_strike = short.strike - cfg.wing_width
    longs = [o for o in chain if _strike_key(o.strike) == _strike_key(long_strike)]
    if not longs:
        return None
    long = longs[0]
    credit = round(short.bid - long.ask, 2)
    if credit <= 0:
        return None
    max_loss = round((cfg.wing_width - credit) * 100, 2)
    return SpreadOrder(ticker=ticker, structure="bull_put_spread",
                       short_strike=short.strike, long_strike=long_strike,
                       credit=credit, spot=spot, atr=atr,
                       max_loss_per_contract=max_loss, qty=1,
                       short_bid=short.bid, short_ask=short.ask,
                       long_bid=long.bid, long_ask=long.ask)


def _occ(symbol, expiry, right, strike):
    """OCC option symbol, e.g. SPY260619P00568000."""
    yymmdd = datetime.strptime(expiry, "%Y-%m-%d").strftime("%y%m%d")
    strike_int = _strike_key(strike)
    return f"{symbol}{yymmdd}{right}{strike_int:08d}"


def to_tradier_payload(order, expiry, qty):
    """Render a bull_put_spread SpreadOrder to a Tradier multileg credit order payload."""
    sym = order.ticker
    return {
        "class": "multileg", "symbol": sym, "type": "credit", "duration": "day",
        "price": round(order.credit, 2),
        "option_symbol[0]": _occ(sym, expiry, "P", order.short_strike),
        "side[0]": "sell_to_open", "quantity[0]": qty,
        "option_symbol[1]": _occ(sym, expiry, "P", order.long_strike),
        "side[1]": "buy_to_open", "quantity[1]": qty,
    }
