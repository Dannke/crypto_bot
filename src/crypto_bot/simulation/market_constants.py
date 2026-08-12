"""Shared market data constants for backtesting.

Approximate 24h quote volume (USD) per symbol, sourced from exchange tickers
at the time of the validation period.  Historical tickers are not available,
so these are fixed constants used to give the liquidity filter realistic input
in backtest mode.
"""

MARKET_QUOTE_VOLUME: dict[str, float] = {
    "BTC/USDT": 429_000_000,
    "ETH/USDT": 114_000_000,
    "SOL/USDT": 25_000_000,
    "XRP/USDT": 13_000_000,
    "AVAX/USDT": 4_000_000,
    "ADA/USDT": 3_000_000,
    "DOGE/USDT": 3_000_000,
    "BNB/USDT": 2_000_000,
    "POL/USDT": 1_000_000,
}
