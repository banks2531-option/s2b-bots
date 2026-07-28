"""Phase B replay: build a `Deps` backed entirely by ARCHIVED data, then call the bot's REAL
`run_entry_cycle`/`tick` with it. Design: docs/superpowers/specs/2026-07-23-replay-harness-design.md.

The safety guarantee the design requires: there is NO broker or http anywhere in these deps. Market
data comes from the Phase-A capture; the order path is a LOCAL simulator that records the attempted
order and returns a deterministic synthetic fill (no network). A strict no-trade mode
(`allow_fills=False`) turns any order attempt into a loud `ReplayOrderAttempt`, so a coding mistake
can never silently 'trade' during replay.
"""
from bot.app.orchestrator import Deps
from bot.strategy.s2b import OptionQuote, S2bConfig, _occ
from bot.risk_gate import AccountState
from bot.broker.order_state import ExecutionResult


class ReplayOrderAttempt(RuntimeError):
    """Raised when replay logic reaches an order/broker path it must not. Converts a would-be trade
    into a loud failure -- the replay must never reach a broker, and must never silently execute."""


def chain_from_snapshot(snap):
    """Inverse of replay_capture.snapshot_chain: [dict] -> [OptionQuote]. Rebuilds strike/delta/bid/
    ask/iv. Quote timestamps are not carried by the snapshot, so they default to None (a v1 fidelity
    limitation noted in the design -- quote-age gates that need timestamps degrade to their fallback)."""
    out = []
    for r in (snap or []):
        out.append(OptionQuote(strike=r["strike"], delta=r["delta"], bid=r["bid"], ask=r["ask"],
                               iv=r.get("iv")))
    return out


def multi_expiry_iv_resolver(chains_by_expiry):
    """Build an option_greeks_iv(occ_symbols) -> {occ: iv} callable from captured chains ACROSS
    expiries ({expiry: snapshot}), so the BS gap-stress reprice can price the WHOLE gap-stress book --
    the candidate AND every held-position expiry -- with the real per-leg IV the bot saw. This is what
    lets the stateful gap_2atr baseline reproduce (the single-expiry resolver left held legs on flat
    fallback IV). Any leg not in the map degrades to features.gap_fallback_iv (live behavior when
    greeks are missing). Returns None if no usable IV anywhere (caller then leaves greeks unwired)."""
    m = {}
    for expiry, snapshot in (chains_by_expiry or {}).items():
        if not expiry:
            continue
        for r in (snapshot or []):
            iv = r.get("iv")
            if iv is not None and r.get("strike") is not None:
                try:
                    m[_occ("SPY", expiry, "P", r["strike"])] = float(iv)
                except Exception:
                    continue
    if not m:
        return None
    def option_greeks_iv(symbols):
        return {s: m[s] for s in symbols if s in m}
    return option_greeks_iv


def snapshot_iv_resolver(snapshot, expiry):
    """Single-expiry convenience wrapper over multi_expiry_iv_resolver (kept for the independent
    single-record replay path). See that function for details."""
    return multi_expiry_iv_resolver({expiry: snapshot})


def build_replay_deps(*, chain, spot, atr, expiry, equity, features, base_risk_pct,
                      s2b_cfg=None, entry_days=frozenset(range(5)), max_open=1,
                      max_entries_per_day=1, vix_regime=(0.5, 0.0), orders=None,
                      allow_fills=True, option_greeks_iv=None):
    """A `Deps` whose market data is the archived snapshot and whose order path is a local simulator.

    `orders` (a list) collects every simulated order payload -- the replay's record of what the bot
    WOULD have done. `allow_fills=False` makes any order attempt raise `ReplayOrderAttempt` instead
    (used to prove a no-entry scenario never trades). No network, no broker client, deterministic.
    """
    s2b_cfg = s2b_cfg or S2bConfig()
    orders = orders if orders is not None else []

    def _open_spread(payload):
        if not allow_fills:
            raise ReplayOrderAttempt("open_spread reached with allow_fills=False")
        orders.append(payload)
        qty = payload.get("quantity[0]", 0)
        price = payload.get("price")
        # Deterministic synthetic FULL fill at the submitted price -- zero slippage/fees in v1. A
        # slippage/fee model is a deliberate later refinement (design experiments E/F); modelling it
        # here would bake an unvalidated assumption into the baseline.
        return ExecutionResult(status="filled", requested_quantity=qty, filled_quantity=qty,
                               average_fill_price=price, submitted_limit=price,
                               commissions=0.0, regulatory_fees=0.0, order_id="replay",
                               normalized_fill=None, legs_balanced=True)

    def _close_spread(pos, action):
        # Position management/exit replay is out of v1 scope (entry-decision reproduction only). If a
        # replay path reaches here it is a bug, not a silent no-op -- fail loudly.
        raise ReplayOrderAttempt("close_spread is not modeled in replay v1")

    def account_state(today, concurrent):
        return AccountState(equity, equity, 0.0, concurrent, 0.0, {}, today)

    return Deps(
        get_spot=lambda s: spot, get_atr=lambda s: atr,
        get_chain=lambda s, e: chain, pick_expiry=lambda today: expiry,
        get_expirations=lambda today: [expiry],
        get_vix_regime=lambda: vix_regime,
        account_state=account_state,
        mark_position=lambda p: (p.credit if p is not None else 0.0),
        dte_of=lambda p, today: 5,
        open_spread=_open_spread, close_spread=_close_spread,
        broker_positions=lambda: [], broker_equity=lambda: equity, bot_equity=lambda: equity,
        alert_sink=lambda alerts: None,
        s2b_cfg=s2b_cfg, base_risk_pct=base_risk_pct,
        entry_days=entry_days, max_open=max_open, max_entries_per_day=max_entries_per_day,
        features=features,
        # Snapshot-backed IV for the BS gap-stress reprice (None -> flat fallback = live behavior when
        # greeks are missing). Own-book gap exposure comes from state.open_positions (threaded by the
        # stateful replay); foreign exposure is empty (no shared account in replay).
        option_greeks_iv=option_greeks_iv,
        risk_equity=lambda: equity,
        account_spy_exposure=lambda: {"structural": 0.0, "stop": 0.0},
        account_spy_spreads=lambda: [],
    )
