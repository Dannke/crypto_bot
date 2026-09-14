-- =============================================================================
-- crypto_bot — SQLite schema (version 1)
--
-- Design notes:
--   * Timestamps are stored as epoch milliseconds (ccxt convention).
--   * ENUMs are stored as TEXT with CHECK constraints mirroring core.enums.
--   * Every domain table has created_at/updated_at + a status/reason column
--     so the decision journal and trade lifecycle are fully auditable.
--   * ON DELETE RESTRICT on positions prevents orphaning a trade from its
--     position, and a self-FK would be overkill; instead trades reference
--     positions by (symbol, opened_at_ms).
--   * PRAGMA foreign_keys = ON is enforced in code at connection time.
-- =============================================================================

-- ---- Meta: schema versioning (single-row table) ----------------------------
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---- Candles ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candles (
    symbol    TEXT    NOT NULL,
    timeframe TEXT    NOT NULL,
    ts_ms     INTEGER NOT NULL,
    open      REAL    NOT NULL,
    high      REAL    NOT NULL,
    low       REAL    NOT NULL,
    close     REAL    NOT NULL,
    volume    REAL    NOT NULL,
    PRIMARY KEY (symbol, timeframe, ts_ms)
);

CREATE INDEX IF NOT EXISTS idx_candles_symbol_tf_ts
    ON candles (symbol, timeframe, ts_ms);

-- ---- Signals ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms       INTEGER NOT NULL,
    symbol      TEXT    NOT NULL,
    signal      TEXT    NOT NULL CHECK (signal IN ('BUY','SELL','HOLD')),
    side        TEXT        CHECK (side IS NULL OR side IN ('LONG','SHORT')),
    confidence  REAL    NOT NULL,
    score       REAL,
    timeframe   TEXT,
    reason      TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_signals_ts_symbol ON signals (ts_ms, symbol);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals (symbol, ts_ms);

-- ---- Decision journal (accept/reject per candidate) ------------------------
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms         INTEGER NOT NULL,
    symbol        TEXT    NOT NULL,
    timeframe     TEXT    NOT NULL DEFAULT '',
    accepted      INTEGER NOT NULL CHECK (accepted IN (0,1)),
    reject_reason TEXT        CHECK (reject_reason IS NULL
                                    OR reject_reason IN (
        'insufficient_liquidity','spread_too_wide','volatility_out_of_range',
        'low_score','low_confidence','conflicting_timeframes','in_cooldown',
        'position_exists','blacklisted','insufficient_data',
        'risk_budget_exhausted','max_positions_reached','drawdown_halt',
        'no_direction'
    )),
    detail        TEXT,
    score         REAL,
    signal        TEXT        CHECK (signal IS NULL OR signal IN ('BUY','SELL','HOLD')),
    outcome       TEXT        CHECK (outcome IS NULL OR outcome IN (
        'position_opened','drawdown_halt','slot_taken','max_positions_reached',
        'no_position','open_unrealized_drawdown'
    )),
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts        ON decisions (ts_ms);
CREATE INDEX IF NOT EXISTS idx_decisions_symbol_ts ON decisions (symbol, ts_ms);
CREATE INDEX IF NOT EXISTS idx_decisions_accepted  ON decisions (accepted);

-- ---- Positions --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT    NOT NULL,
    timeframe     TEXT    NOT NULL,
    side          TEXT    NOT NULL CHECK (side IN ('LONG','SHORT')),
    size          REAL    NOT NULL,
    entry_price   REAL    NOT NULL,
    stop          REAL    NOT NULL,
    take          REAL    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'open'
                  CHECK (status IN ('proposed','open','closed','rejected','cancelled')),
    closed_by     TEXT        CHECK (closed_by IS NULL OR closed_by IN ('stop_loss','take_profit','manual','signal','emergency_drawdown','rebalance','timeout_fallback')),
    opened_at_ms  INTEGER NOT NULL,
    closed_at_ms  INTEGER,
    exit_price    REAL,
    pnl_pct       REAL,
    mode          TEXT    NOT NULL CHECK (mode IN ('signal_only','paper','live')),
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_positions_status_symbol ON positions (status, symbol);
CREATE INDEX IF NOT EXISTS idx_positions_symbol_opened ON positions (symbol, opened_at_ms);
CREATE INDEX IF NOT EXISTS idx_positions_open           ON positions (status);
CREATE INDEX IF NOT EXISTS idx_positions_symbol_tf_status ON positions (symbol, timeframe, status);

-- ---- Trades (fills/executions, linked to a position) -----------------------
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id  INTEGER NOT NULL,
    symbol       TEXT    NOT NULL,
    side         TEXT    NOT NULL CHECK (side IN ('LONG','SHORT')),
    order_type   TEXT    NOT NULL CHECK (order_type IN ('market','limit')),
    size         REAL    NOT NULL,
    price        REAL    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'filled'
                 CHECK (status IN ('pending','filled','cancelled','rejected')),
    ts_ms        INTEGER NOT NULL,
    mode         TEXT    NOT NULL CHECK (mode IN ('signal_only','paper','live')),
    external_id  TEXT,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    FOREIGN KEY (position_id) REFERENCES positions(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_trades_position ON trades (position_id);
CREATE INDEX IF NOT EXISTS idx_trades_ts       ON trades (ts_ms);

-- ---- Equity curve -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS equity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms       INTEGER NOT NULL,
    currency    TEXT    NOT NULL,
    equity      REAL    NOT NULL,
    drawdown_pct REAL,
    mode        TEXT    NOT NULL CHECK (mode IN ('signal_only','paper','live')),
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity (ts_ms);

CREATE INDEX IF NOT EXISTS idx_positions_symbol_tf_status ON positions (symbol, timeframe, status);

-- ---- Funding rates (R0.1) ----------------------------------------------------
CREATE TABLE IF NOT EXISTS funding_rates (
    symbol           TEXT    NOT NULL,
    funding_time_ms  INTEGER NOT NULL,
    funding_rate     REAL    NOT NULL,
    mark_price       REAL,
    PRIMARY KEY (symbol, funding_time_ms)
);

CREATE INDEX IF NOT EXISTS idx_funding_rates_symbol_time
    ON funding_rates (symbol, funding_time_ms);

-- ---- Funding payments audit trail (R0.2) ------------------------------------
CREATE TABLE IF NOT EXISTS funding_payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     INTEGER NOT NULL,
    funding_time_ms INTEGER NOT NULL,
    amount          REAL    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    FOREIGN KEY (position_id) REFERENCES positions(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_funding_payments_position
    ON funding_payments (position_id);
CREATE INDEX IF NOT EXISTS idx_funding_payments_time
    ON funding_payments (funding_time_ms);

-- ---- schema version ---------------------------------------------------------
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '8')
    ON CONFLICT(key) DO UPDATE SET value='8';
