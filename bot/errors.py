"""Shared bot exception types."""


class MarketDataUnavailable(Exception):
    """Required market data (a quote, option chain, or price history) could not be obtained from the
    broker -- a transient outage, read timeout, 5xx, or an empty/absent quote or bar set.

    This is deliberately DISTINCT from a real broker rejection (`BrokerError`) and from ordinary logic
    errors: it means "try again later", not "something is wrong with our request". The tick loop
    catches it and SKIPS the cycle (taking no new risk) rather than crashing the live bot, which is the
    root-cause remedy for the 2026-07-24 Bot C crash-loop (uncaught Tradier timeouts/504s) and the
    false "failed close" halt (a quote-fetch failure mislabelled as a stuck close).
    """
