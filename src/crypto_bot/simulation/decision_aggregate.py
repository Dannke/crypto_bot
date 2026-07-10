from __future__ import annotations


def aggregate_raw_rows(
    rows: list[dict],
) -> dict[tuple[str, str, int], dict]:
    """Group raw decision rows by (symbol, timeframe, ts_ms).

    Each input dict must have at least keys:
        ts_ms, symbol, timeframe, accepted, reject_reason, score, signal

    Returns a dict keyed by (symbol, timeframe, ts_ms) with the full row
    as value.  When multiple rows exist for the same key (e.g. one
    accepted + one rejected on the same bar), the accepted row wins.
    """
    out: dict[tuple[str, str, int], dict] = {}
    for r in rows:
        key = (r["symbol"], r.get("timeframe") or "", int(r["ts_ms"]))
        # Accepted decisions override rejected ones for the same bar
        if key not in out or (r.get("accepted") and not out[key].get("accepted")):
            out[key] = r
    return out
