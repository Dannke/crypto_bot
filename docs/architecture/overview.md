# Архитектура crypto_bot — обзор

**Обновлено:** 2026-09-27. Документ описывает систему такой, какая она в коде; при расхождении
прав код. Открытые пункты — в [`backlog.md`](backlog.md), исследовательские вердикты — в
[`docs/research/`](../research/), карта всей документации — в [`docs/README.md`](../README.md).

---

## 1. Что это

Платформа исследования и исполнения систематических стратегий на perpetual futures Bybit.
Один общий `Backtester` (режимы `candidate` и `portfolio`), один composition root
`pipeline/factory.py`, один портфельный слой (риск, исполнение, режимы) для всех стратегий —
параллельных веток не создаётся.

Принципы:

- **Одни и те же компоненты** в бэктесте и в живом режиме: построители признаков, пайплайны,
  исполнители, риск-движок.
- **Только закрытые бары и один анкер времени:** данные режутся по времени закрытия бара, заглядывания
  вперёд нет по построению (`HistoricalCandleSource.slice`, `portfolio/market_snapshot.py`).
- **Детерминизм:** фиксированные seed, одинаковые лимиты во всех окнах walk-forward.

---

## 2. Слои решения

```
┌─────────────────────────────────────────────────────────────┐
│                      Backtester (единый)                    │
├──────────────────────────────┬──────────────────────────────┤
│ candidate (single-timeframe) │ portfolio                    │
│ DecisionPipeline             │ PortfolioDecisionPipeline    │
│      ↓                       │      ↓                       │
│ SignalExecutor               │ PortfolioStrategy            │
│                              │      ↓                       │
│                              │ RegimeGatedFusion            │
│                              │      ↓                       │
│                              │ PortfolioRiskEngine          │
│                              │      ↓                       │
│                              │ PortfolioExecutor            │
└──────────────────────────────┴──────────────────────────────┘
```

---

## 3. Портфельный тик в бэктесте — фактические вызовы

```
HistoricalCandleSource (вся история загружается один раз)
  ↓ единые часы: тик на закрытии каждого бара любого таймфрейма
Backtester.run_async, на каждом тике:
  0. _accrue_funding(as_of)             — расчёты фандинга до этого тика, каждый один раз на
                                          позицию (simulation/funding_accrual.py); до решений:
                                          закрытая на тике позиция расчёт этого тика получает,
                                          открытая на нём — нет
  1. аварийный стоп по просадке (risk.emergency_drawdown_pct; стоп липкий)
  2. _process_post_only(as_of)            — исполнение висящих post-only заявок
  3. _run_portfolio_tick — только если наступил ребаланс (rebalance_hours активной стратегии):
       PortfolioDecisionPipeline.process_market(snapshot, state, strategy)
         → strategy.evaluate_market(snapshot, state)           → PortfolioIntent
         → classify_regime(...) → RegimeGatedFusion.fuse([intent], regime)
       PortfolioRiskEngine.evaluate(intent, state, cross_section, sizing)
       _rebalance_positions(report)        — закрыть всё, что выпало из целевой книги
       PortfolioExecutor.open_position(...) — открыть новые позиции
  4. check_positions_range                 — SL/TP (для mean_reversion_v0 отключены)
  5. _record_equity(as_of)                 — одна mark-to-market точка на тик (таблица equity):
                                          начальный капитал + PnL закрытых + фандинг открытых +
                                          нереализованный PnL; тот же расчёт у аварийного стопа
```

Исключение внутри тика ловится широким `except`: тик теряется целиком, вместе с входами и
выходами (`scripts/count_mr_trades.py` считает такие тики). Начисление фандинга стоит до этого
блока, и потерянный тик расчёт не теряет. Walk-forward (`simulation/walk_forward.py`) режет
историю первого символа `calendar_split`-ом на 2 или 3 окна; `--start/--end` закрепляют окно
данных. Каждое окно идёт на свежей БД, поэтому фандинг ему передаётся из БД данных
(`fetch_funding_source`), как свечи.

---

## 4. Компоненты

| область | что реализовано | где |
|---|---|---|
| исполнение (R0) | фандинг; спецификации инструментов двух рынков, снимки, допуск по `launchTime`; маржа и плечо; модель издержек fee + slippage + funding | `data/funding.py`, `simulation/funding_accrual.py`, `data/instruments.py`, `portfolio/risk.py`, `execution/costs.py` |
| режимы (R1–R5) | конфиг; индикаторы ADX, ATR%, перцентиль; классификатор 2 оси → 4 режима с гистерезисом; множители экспозиции по стратегиям (без override — 1.0) | `config/schemas.py`, `indicators/regime.py`, `portfolio/regime.py`, `pipeline/portfolio_fusion.py` |
| проводка (R6) | Settings → PortfolioConfig → стратегия и режимы через фабрику | `pipeline/factory.py` |
| walk-forward (R7) | сравнение с гейтингом и без; проверялось на CSM без эджа — полезность гейта не доказана | `scripts/walk_forward_regime_comparison.py` |
| снапшот рынка (R8) | один анкер, только закрытые бары, минимальная история | `portfolio/market_snapshot.py` |
| стратегии | `cross_sectional_momentum_v0`, `mean_reversion_v0`, `random_baseline`, `reverse_momentum_v0` | `strategy/portfolio_strategies.py` |
| признак MR | горизонт-согласованный z | `portfolio/mean_reversion_features.py` |

---

## 5. Данные и конфигурация

- `data/crypto_bot.db` (git не отслеживает): свечи 5m / 15m / 1h / 4h по вселенной
  `simulation/market_constants.py::MARKET_QUOTE_VOLUME` — спотовые (п. 10 бэклога), таблица
  `funding_rates` — только январь 2024, таблицы исполнения `positions`, `trades`, `equity`,
  `decisions`. Покрытие свечей печатает команда из
  [приложения B](../research/mean-reversion/cycle-2/1-signal-definition.md#приложение-b-покрытие-данных--только-временные-метки)
  документа `cycle-2/1-signal-definition.md`. Файл заморожен на схеме v11
  ([поправка 2](../research/funding-basis/cycle-1/1-hypothesis-and-decision-rule.md#поправка-2-2026-10-06-до-данных-комиссии-в-эквити-отдельный-файл-данных-снимок-спецификаций) Task 0' funding/basis):
  код читает его только через `Database(path, read_only=True)`, а открытие на запись отказывает —
  свечи до v12 не мигрируют. Данные цикла funding/basis — отдельный файл `data/funding_basis.db`.
- `candles` с v12 хранит рынок в ключе: `spot`, `linear` или `unverified` — ряд устаревших
  загрузчиков ccxt, не сверенный с API. Значения по умолчанию нет: запись без рынка — ошибка;
  чтение без рынка допустимо, только пока у пары один ряд. `positions` и `trades` хранят
  `market` (`linear` по умолчанию), позиции — `leg_group`; открытая позиция одна на
  `(symbol, timeframe, market)`.
- Спецификации инструментов: бэктест берёт их из снимка `data/instruments/*.json`
  (`scripts/snapshot_instruments.py`, mainnet, sha256 и источник — в файле; снимки в git).
  Портфельный walk-forward без `--instrument-snapshot` не стартует; `--live-instruments` —
  только для разведки. `data/cache/bybit_instruments.json` — кэш живого пути, TTL 24 ч,
  testnet или mainnet — смотря кто обновил его первым (п. 13 бэклога).
- `config/settings.yaml` — единственный источник истины конфигурации. В документах он не
  дублируется; снимок, по которому получены числа исследования, лежит в его pre-registration
  и сверяется `scripts/check_config_snapshot.py`.

---

## 6. Карта ключевых файлов

```
src/crypto_bot/
├── config/schemas.py                  # Pydantic-схемы конфига
├── pipeline/factory.py                # composition root: стратегии, пайплайны, риск-движок
├── pipeline/portfolio_decision_pipeline.py
├── pipeline/portfolio_fusion.py       # RegimeGatedFusion
├── portfolio/                         # market_snapshot, regime, risk, models, mean_reversion_features
├── strategy/portfolio_strategies.py   # CSM, MR, baselines
├── simulation/backtester.py           # единый Backtester
├── simulation/walk_forward.py         # calendar_split, pin_candles, run_walk_forward
├── simulation/portfolio_executor.py   # PortfolioExecutor
├── simulation/funding_accrual.py      # начисление фандинга открытым позициям, общее для исполнителей
├── simulation/pnl.py                  # PnLTracker, PnLSummary, функции Sharpe
├── orchestrator_portfolio.py          # живой portfolio-оркестратор
└── cli.py

scripts/                               # исследовательские инструменты
├── walk_forward.py                    # прогон walk-forward
├── check_config_snapshot.py           # сверка снимка конфига из pre-registration
├── estimate_mr_turnover.py            # cost/turnover-гейт
├── scan_sigma_events.py               # статистическая валидация z и сырая частота
├── count_mr_trades.py                 # счёт сделок реальным Backtester, без PnL
├── mr_decision_rule.py                # decision rule цикла 2 MR из БД прогона
├── reproduce_mr_cycle2.py             # воспроизведение прогона цикла 2 MR число в число
├── snapshot_instruments.py           # снимок спецификаций mainnet (linear + spot) для бэктеста
└── sigma_definition_theory.py         # теория и синтетика для определения z
```

---

## 7. Тесты

Правило проекта — полный прогон без исключений (`CLAUDE.md`, правило 1):

```bash
python -m pytest tests/ -q
```

Параллельно: `python -m pytest -n auto --dist worksteal`. `pytest` на PATH может принадлежать
другому окружению без `pytest-xdist`; прогоны проекта идут интерпретатором `C:\Python311`.
Последний зафиксированный результат — 664 passed, 19 skipped, 1 xfailed на `822b90b` (описание
PR #5). 19 пропусков — тесты `test_cross_validate.py`, которым нужны данные БД после
`FIX_CUTOFF_MS`; 1 xfail — strict-тест декоративного `mean_reversion.max_positions`.
