"""Учёт фандинга в бэктесте: дефекты F0–F5 и конвенция границы расчёта.

До исправления фандинг не доходил до эквити портфельного бэктеста ни при каких
данных (раздел 0.2 docs/research/funding-carry/cycle-1/1-hypothesis-and-decision-rule.md):

- F0 — walk-forward читал ``funding_rates`` из свежей БД окна, событий там ноль;
- F1 — на каждом тике заново начислялись все события с открытия позиции;
- F2 — ``PaperPosition.close()`` перезаписывал ``pnl_abs`` и стирал начисленное;
- F3 — эквити тика и аварийного стопа брали нереализованный PnL только по цене;
- F4 — номинал считался как доля эквити, умноженная на цену, а не в USDT;
- F5 — журнал ``funding_payments`` копил дубли, уникальности не было.

Каждый тест ниже падал на коде до исправления. Конвенция границы: расчёт в
момент ``τ`` получает позиция, открытая раньше ``τ``; закрытая на тике ``τ`` его
тоже получает — начисление идёт в начале тика, до решений.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime

import pytest

from crypto_bot.config.env import Config, EnvConfig
from crypto_bot.config.schemas import Settings
from crypto_bot.core.enums import Mode, Side, StrategyType, TradeStatus
from crypto_bot.core.types import Candle
from crypto_bot.data.funding import FundingEvent, FundingRepository, HistoricalFundingSource
from crypto_bot.execution.costs import CompositeCostModel
from crypto_bot.portfolio import PositionIntent
from crypto_bot.simulation import backtester as backtester_module
from crypto_bot.simulation import walk_forward
from crypto_bot.simulation.backtester import Backtester
from crypto_bot.simulation.executor import SignalExecutor
from crypto_bot.simulation.historical_source import HistoricalCandleSource
from crypto_bot.simulation.paper_position import PaperPosition
from crypto_bot.simulation.pnl import PnLSummary, PnLTracker
from crypto_bot.simulation.portfolio_executor import PortfolioExecutor
from crypto_bot.storage.db import Database, Repositories

HOUR = 3_600_000
T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC — момент расчёта при 8-часовом интервале
INSERT_PAYMENT = "INSERT INTO funding_payments (position_id, funding_time_ms, amount) VALUES (?, ?, ?)"


def _flat(price: float, n_bars: int, start: int = T0) -> list[Candle]:
    return [
        Candle(timestamp=start + i * HOUR, open=price, high=price, low=price, close=price, volume=1.0)
        for i in range(n_bars)
    ]


def _events(symbol: str, rates: dict[int, float]) -> HistoricalFundingSource:
    source = HistoricalFundingSource()
    source.load_events(symbol, [FundingEvent(symbol, ts, rate, None) for ts, rate in sorted(rates.items())])
    return source


def _executor(tmp_path) -> tuple[PortfolioExecutor, Database]:
    db = Database(tmp_path / "run.db")
    executor = PortfolioExecutor(
        Config(settings=Settings(), env=EnvConfig()), Repositories(db), PnLTracker(),
        cost_model=CompositeCostModel.bybit_perp_default(),
    )
    return executor, db


def _open(executor, symbol: str, side: Side, weight: float, price: float, ts: int) -> PaperPosition:
    result = executor.open_position(
        PositionIntent(symbol=symbol, side=side, target_weight=weight, timeframe="1h"),
        entry_price=price, atr_pct=1.0, timestamp_ms=ts,
    )
    assert result.handled, result.message
    return next(p for p in executor.tracker.positions if p.is_open and p.symbol == symbol)


def _portfolio_config() -> Config:
    base = Settings().model_dump()
    base["portfolio"]["strategy_name"] = "cross_sectional_momentum_v0"
    return Config(settings=Settings.model_validate(base), env=EnvConfig())


def _backtester(tmp_path, candles: dict[str, list[Candle]], start: int, end: int, **kwargs) -> Backtester:
    source = HistoricalCandleSource()
    for symbol, bars in candles.items():
        source.load_all(symbol, "1h", bars)
    return Backtester(
        _portfolio_config(), symbols=list(candles), timeframes=["1h"], start_ms=start, end_ms=end,
        source=source, db=Database(tmp_path / "bt.db"), strategy_mode=StrategyType.PORTFOLIO, **kwargs,
    )


class _PermissiveInstruments:
    """Кэш спецификаций без обращения к диску и API: любой символ торгуется."""

    def is_tradable_linear_perpetual(self, symbol: str) -> bool:
        return True

    def round_qty_down(self, symbol: str, qty: float) -> float:
        return qty

    def check_min_notional(self, symbol: str, qty: float, price: float) -> bool:
        return True


# --------------------------------------------------------------------------- F0


def test_fetch_funding_source_reads_the_data_db(tmp_path) -> None:
    """Источник фандинга walk-forward строится из БД данных, со всей историей."""
    data = Database(tmp_path / "data.db")
    FundingRepository(data).upsert_many([
        FundingEvent("BTC/USDT", T0 - 200 * HOUR, 0.0001, None),
        FundingEvent("BTC/USDT", T0 + 8 * HOUR, 0.0002, None),
    ])
    data.close()

    source = walk_forward.fetch_funding_source(str(tmp_path / "data.db"), ["BTC/USDT", "ETH/USDT"])

    assert [e.funding_time_ms for e in source.events_up_to(T0 + 8 * HOUR, "BTC/USDT")] == [
        T0 - 200 * HOUR, T0 + 8 * HOUR,
    ]
    assert source.events_up_to(T0 + 8 * HOUR, "ETH/USDT") == []


def test_every_walk_forward_window_gets_the_funding_source(monkeypatch) -> None:
    seen = []

    async def capture(*args, **kwargs):
        seen.append(kwargs.get("funding_source"))
        return PnLSummary()

    monkeypatch.setattr(walk_forward, "_run_single_window", capture)
    candles = HistoricalCandleSource()
    candles.load_all("BTC/USDT", "1h", _flat(100.0, 100))
    funding = HistoricalFundingSource()

    walk_forward.run_walk_forward(
        _portfolio_config(), ["BTC/USDT"], "1h", candles, 0.5,
        strategy_mode=StrategyType.PORTFOLIO, three_way=True, validation_ratio=0.2,
        funding_source=funding,
    )

    assert seen == [funding, funding, funding]


def test_single_window_hands_the_funding_source_to_the_backtester(tmp_path, monkeypatch) -> None:
    captured: dict = {}

    class FakeBacktester:
        def __init__(self, *args, **kwargs) -> None:
            captured.update(kwargs)
            self._executor = type("Executor", (), {})()

        async def run_async(self) -> PnLSummary:
            return PnLSummary()

    monkeypatch.setattr(walk_forward, "Backtester", FakeBacktester)
    funding = HistoricalFundingSource()

    asyncio.run(walk_forward._run_single_window(
        _portfolio_config(), ["BTC/USDT"], "1h", HistoricalCandleSource(), T0, T0 + HOUR,
        strategy_mode=StrategyType.PORTFOLIO, db_path=str(tmp_path / "window.db"),
        funding_source=funding,
    ))

    assert captured["funding_source"] is funding


def test_backtester_reads_its_own_db_with_a_168h_lookback(tmp_path) -> None:
    """Без переданного источника фандинг берётся из БД прогона — с запасом 168 ч до окна."""
    bt = _backtester(tmp_path, {"BTC/USDT": _flat(100.0, 10)}, T0, T0 + 10 * HOUR)
    FundingRepository(bt._db).upsert_many([
        FundingEvent("BTC/USDT", ts, 0.0001, None)
        for ts in (T0 - 200 * HOUR, T0 - 160 * HOUR, T0 + 8 * HOUR)
    ])

    bt._load_funding()

    assert [e.funding_time_ms for e in bt._funding_source.events_up_to(T0 + 10 * HOUR, "BTC/USDT")] == [
        T0 - 160 * HOUR, T0 + 8 * HOUR,
    ]


# --------------------------------------------------------------------------- F1, F2, F4, F5


def test_each_settlement_is_credited_once(tmp_path) -> None:
    executor, db = _executor(tmp_path)
    position = _open(executor, "BTC/USDT", Side.SHORT, 0.5, 100.0, T0 + HOUR)  # 50 монет
    source = _events("BTC/USDT", {T0 + 8 * HOUR: 0.0001})

    for hours in (8, 9, 10, 11):
        executor.accrue_funding(source, T0 + hours * HOUR, lambda symbol, timeframe, ts: 100.0)

    # шорт получает положительную ставку один раз: 50 × 100 × 0.0001
    assert position.funding_abs == pytest.approx(0.5)
    # журнал пишет платёж позиции: получено 0.5 — строка −0.5
    rows = db.conn.execute("SELECT funding_time_ms, amount FROM funding_payments").fetchall()
    assert [tuple(row) for row in rows] == [(T0 + 8 * HOUR, pytest.approx(-0.5))]


def test_closed_position_keeps_its_funding(tmp_path) -> None:
    executor, _ = _executor(tmp_path)
    position = _open(executor, "BTC/USDT", Side.SHORT, 0.5, 100.0, T0 + HOUR)
    executor.accrue_funding(_events("BTC/USDT", {T0 + 8 * HOUR: 0.0001}), T0 + 8 * HOUR, lambda *_: 100.0)

    executor.close_position_for_symbol("BTC/USDT", "1h", exit_price=100.0, closed_at_ms=T0 + 9 * HOUR)

    # цена не менялась: брутто 0; taker 0.1% с 5000 USDT на входе и выходе — по 5; фандинг +0.5
    assert position.pnl_abs == pytest.approx(-9.5)


def test_notional_is_quantity_times_price(tmp_path) -> None:
    """Различающий тест F4: равный номинал при ценах, различающихся в 10⁵ раз."""
    executor, _ = _executor(tmp_path)
    prices = {"BTC/USDT": 50_000.0, "DOGE/USDT": 0.5}
    btc = _open(executor, "BTC/USDT", Side.SHORT, 0.1, prices["BTC/USDT"], T0 + HOUR)    # 0.02 BTC
    doge = _open(executor, "DOGE/USDT", Side.SHORT, 0.1, prices["DOGE/USDT"], T0 + HOUR)  # 2000 DOGE
    source = HistoricalFundingSource()
    for symbol in prices:
        source.load_events(symbol, [FundingEvent(symbol, T0 + 8 * HOUR, 0.0001, None)])

    executor.accrue_funding(source, T0 + 8 * HOUR, lambda symbol, timeframe, ts: prices[symbol])

    # номинал 1000 USDT у обеих: 1000 × 0.0001
    assert btc.funding_abs == pytest.approx(0.1)
    assert doge.funding_abs == pytest.approx(0.1)


def test_mark_price_of_the_event_sets_the_notional(tmp_path) -> None:
    executor, _ = _executor(tmp_path)
    position = _open(executor, "BTC/USDT", Side.LONG, 0.5, 100.0, T0 + HOUR)  # 50 монет
    source = HistoricalFundingSource()
    source.load_events("BTC/USDT", [FundingEvent("BTC/USDT", T0 + 8 * HOUR, 0.0001, 120.0)])

    executor.accrue_funding(source, T0 + 8 * HOUR, lambda *_: 100.0)

    # лонг платит положительную ставку с номинала по mark price: 50 × 120 × 0.0001
    assert position.funding_abs == pytest.approx(-0.6)


def test_position_opened_at_the_settlement_does_not_get_it(tmp_path) -> None:
    executor, _ = _executor(tmp_path)
    position = _open(executor, "BTC/USDT", Side.SHORT, 0.5, 100.0, T0 + 8 * HOUR)

    executor.accrue_funding(_events("BTC/USDT", {T0 + 8 * HOUR: 0.0001}), T0 + 9 * HOUR, lambda *_: 100.0)

    assert position.funding_abs == 0.0


def _candidate_short(tmp_path) -> tuple[SignalExecutor, PaperPosition]:
    """Кандидатный путь: SignalExecutor с шортом 50 монет по 100, открытым в T0 + 1 ч."""
    executor = SignalExecutor(
        Config(settings=Settings(), env=EnvConfig()), Repositories(Database(tmp_path / "cand.db")), PnLTracker(),
    )
    position = PaperPosition(
        symbol="BTC/USDT", timeframe="1h", side=Side.SHORT, size=50.0, entry_price=100.0,
        stop_loss=110.0, take_profit=90.0, entry_time=datetime.fromtimestamp((T0 + HOUR) / 1000, tz=UTC),
    )
    executor.tracker.add_position(position)
    return executor, position


def test_candidate_path_credits_each_settlement_once(tmp_path, monkeypatch) -> None:
    """Тот же расчёт на кандидатном пути: прежний запасной вариант начислял событие на каждом тике."""
    executor, position = _candidate_short(tmp_path)
    # модель издержек кандидатному исполнителю ставит walk-forward, как здесь
    monkeypatch.setattr(executor, "_costs", CompositeCostModel.bybit_perp_default(), raising=False)
    source = _events("BTC/USDT", {T0 + 8 * HOUR: 0.0001})

    for hours in (8, 9, 10, 11):
        executor.accrue_funding(source, T0 + hours * HOUR, lambda *_: 100.0)

    assert position.funding_abs == pytest.approx(0.5)  # 50 × 100 × 0.0001, один раз


def test_candidate_path_without_a_funding_model_credits_nothing(tmp_path) -> None:
    """Без модели фандинга начислений нет — как у портфельного исполнителя с legacy_default()."""
    executor, position = _candidate_short(tmp_path)

    executor.accrue_funding(_events("BTC/USDT", {T0 + 8 * HOUR: 0.0001}), T0 + 8 * HOUR, lambda *_: 100.0)

    assert position.funding_abs == 0.0


def test_duplicate_funding_payment_is_rejected(tmp_path) -> None:
    db = Database(tmp_path / "f5.db")
    position_id = Repositories(db).positions.insert(
        symbol="BTC/USDT", timeframe="1h", side=Side.SHORT, size=1.0, entry_price=100.0,
        stop=110.0, take=90.0, mode=Mode.PAPER, opened_at_ms=T0, status=TradeStatus.OPEN,
    )
    db.conn.execute(INSERT_PAYMENT, (position_id, T0 + 8 * HOUR, 0.01))

    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(INSERT_PAYMENT, (position_id, T0 + 8 * HOUR, 0.01))


def _legacy_v10_db(path) -> int:
    """БД, какой её оставляет код до v11: без индекса уникальности, версия 10."""
    db = Database(path)
    position_id = Repositories(db).positions.insert(
        symbol="BTC/USDT", timeframe="1h", side=Side.SHORT, size=1.0, entry_price=100.0,
        stop=110.0, take=90.0, mode=Mode.PAPER, opened_at_ms=T0, status=TradeStatus.OPEN,
    )
    db.conn.execute("DROP INDEX uq_funding_payments_position_time")
    db.conn.execute("UPDATE schema_meta SET value='10' WHERE key='schema_version'")
    db.conn.commit()
    db.close()
    return position_id


def _payments(db: Database) -> list[tuple]:
    rows = db.conn.execute(
        "SELECT funding_time_ms, amount FROM funding_payments ORDER BY funding_time_ms, id"
    ).fetchall()
    return [tuple(row) for row in rows]


def test_v11_collapses_duplicates_left_by_repeated_accrual(tmp_path) -> None:
    """БД v10 с дублями F1 доезжает до v11: одна строка на пару позиция × расчёт."""
    path = tmp_path / "legacy.db"
    position_id = _legacy_v10_db(path)
    conn = sqlite3.connect(path)
    for _ in range(3):  # один расчёт, записанный на трёх тиках подряд
        conn.execute(INSERT_PAYMENT, (position_id, T0 + 8 * HOUR, 0.01))
    conn.execute(INSERT_PAYMENT, (position_id, T0 + 16 * HOUR, -0.02))
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        assert db.schema_version() == "11"
        assert _payments(db) == [(T0 + 8 * HOUR, 0.01), (T0 + 16 * HOUR, -0.02)]
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(INSERT_PAYMENT, (position_id, T0 + 8 * HOUR, 0.01))
    finally:
        db.close()


def test_v11_runs_again_after_older_code_stamps_10(tmp_path) -> None:
    """Код до v11 при открытии штампует версию 10 — повторная v11 ничего не меняет и не падает.

    Так бывает с общей data/crypto_bot.db при переключении веток.
    """
    path = tmp_path / "shared.db"
    db = Database(path)
    position_id = Repositories(db).positions.insert(
        symbol="BTC/USDT", timeframe="1h", side=Side.SHORT, size=1.0, entry_price=100.0,
        stop=110.0, take=90.0, mode=Mode.PAPER, opened_at_ms=T0, status=TradeStatus.OPEN,
    )
    db.conn.execute(INSERT_PAYMENT, (position_id, T0 + 8 * HOUR, -0.5))
    db.conn.execute("UPDATE schema_meta SET value='10' WHERE key='schema_version'")
    db.conn.commit()
    db.close()

    db = Database(path)
    try:
        assert db.schema_version() == "11"
        assert _payments(db) == [(T0 + 8 * HOUR, -0.5)]
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(INSERT_PAYMENT, (position_id, T0 + 8 * HOUR, -0.5))
    finally:
        db.close()


# --------------------------------------------------------------------------- F3, сверка, порядок тика


def test_equity_counts_funding_of_open_positions(tmp_path, monkeypatch) -> None:
    bt = _backtester(
        tmp_path, {"BTC/USDT": _flat(100.0, 48)}, T0, T0 + 48 * HOUR,
        funding_source=_events("BTC/USDT", {T0 + 8 * HOUR: 0.0001}),
    )
    monkeypatch.setattr(bt._executor, "_costs", CompositeCostModel.bybit_perp_default())
    _open(bt._executor, "BTC/USDT", Side.SHORT, 0.5, 100.0, T0 + HOUR)

    bt._accrue_funding(T0 + 8 * HOUR)
    bt._record_equity(T0 + 8 * HOUR)

    # цена стоит: эквити = начальные 10 000 + фандинг; комиссия входа списывается при закрытии
    assert bt._mark_to_market_equity(T0 + 8 * HOUR) == pytest.approx(10_000.5)
    row = bt._db.conn.execute("SELECT equity FROM equity ORDER BY ts_ms DESC LIMIT 1").fetchone()
    assert row[0] == pytest.approx(10_000.5)


def test_equity_increment_matches_funding_computed_by_hand(tmp_path, monkeypatch) -> None:
    rates = {T0 + 8 * HOUR: 0.0001, T0 + 16 * HOUR: -0.00005, T0 + 24 * HOUR: 0.0002}
    bt = _backtester(
        tmp_path, {"BTC/USDT": _flat(100.0, 48)}, T0, T0 + 48 * HOUR,
        funding_source=_events("BTC/USDT", rates),
    )
    monkeypatch.setattr(bt._executor, "_costs", CompositeCostModel.bybit_perp_default())
    _open(bt._executor, "BTC/USDT", Side.SHORT, 0.5, 100.0, T0 + HOUR)  # 50 монет по 100

    for hours in range(1, 26):
        bt._accrue_funding(T0 + hours * HOUR)
        bt._record_equity(T0 + hours * HOUR)

    equities = [row[0] for row in bt._db.conn.execute("SELECT equity FROM equity ORDER BY ts_ms")]
    # вручную: 50 × 100 × (0.0001 − 0.00005 + 0.0002) = 1.25, шорт получает
    assert equities[-1] - equities[0] == pytest.approx(1.25)


def test_tick_credits_funding_before_it_decides(tmp_path, monkeypatch) -> None:
    """Порядок тика: начисление раньше решений, иначе закрытая на τ позиция теряет расчёт τ."""
    async def permissive_cache(config):
        return _PermissiveInstruments()

    monkeypatch.setattr(backtester_module, "build_instrument_cache", permissive_cache)
    bt = _backtester(
        tmp_path, {"BTC/USDT": _flat(100.0, 6)}, T0, T0 + 6 * HOUR,
        funding_source=HistoricalFundingSource(),
    )
    calls: list[tuple[str, int]] = []
    accrue = bt._executor.accrue_funding

    def spy_accrue(funding_source, as_of, *args, **kwargs):
        calls.append(("accrue", as_of))
        return accrue(funding_source, as_of, *args, **kwargs)

    def spy_decide(features_map, symbol_candles, as_of) -> None:
        calls.append(("decide", as_of))

    monkeypatch.setattr(bt._executor, "accrue_funding", spy_accrue)
    monkeypatch.setattr(bt, "_run_portfolio_tick", spy_decide)

    bt.run()

    decided = [ts for kind, ts in calls if kind == "decide"]
    assert decided, "портфельный тик не вызывался ни разу"
    assert all(calls.index(("accrue", ts)) < calls.index(("decide", ts)) for ts in decided)
