# Архитектура crypto_bot — обзор

**Обновлено:** 2026-09-26. Документ описывает систему такой, какая она в коде; при расхождении
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
  5. accrue_funding
  6. _record_equity(as_of)                 — одна mark-to-market точка на тик (таблица equity)
```

Исключение внутри тика ловится широким `except`: тик теряется целиком, вместе с входами и
выходами (`scripts/count_mr_trades.py` считает такие тики). Walk-forward
(`simulation/walk_forward.py`) режет историю первого символа `calendar_split`-ом на 2 или 3
окна; `--start/--end` закрепляют окно данных.

---

## 4. Компоненты

| область | что реализовано | где |
|---|---|---|
| исполнение (R0) | фандинг; спецификации инструментов; маржа и плечо; модель издержек fee + slippage + funding | `data/funding.py`, `data/instruments.py`, `portfolio/risk.py`, `execution/costs.py` |
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
  документа `cycle-2/1-signal-definition.md`.
- `data/cache/bybit_instruments.json` — кэш спецификаций инструментов, TTL 24 ч (см. бэклог).
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
