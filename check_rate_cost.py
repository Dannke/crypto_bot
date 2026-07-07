"""Check ccxt rate-limiter cost for Bybit's OHLCV endpoint.

If ``cost > 1``, the effective pause between requests is ``rateLimit × cost``
and the configured ``rate_limit_ms`` in YAML should be divided by the cost.
"""
import ccxt

ex = ccxt.bybit({
    "enableRateLimit": True,
    "rateLimit": 1200,
    "options": {"defaultType": "spot"},
})
cost = ex.calculate_rate_limiter_cost("public", "GET", "/v5/market/kline", {}, {})
print(f"cost: {cost}")

