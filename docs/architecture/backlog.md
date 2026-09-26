# Бэклог — открытые пункты платформы

**Обновлено:** 2026-09-26. Единственное место, где собраны известные открытые пункты. Новый
пункт добавляется сюда с источником; закрытый — удаляется в том же PR, где его закрыли.
Бывший раздел 8 `audit.md`; из него удалены закрытые пункты «Replace CSM strategy»
(проверены два цикла MR) и «Full walk-forward test» (walk-forward работает, им прогнан цикл 2 MR).

---

## Блокирует paper / live

1. **На portfolio-пути нет закрытия позиций в живом оркестраторе.** В
   `orchestrator_portfolio.py` закрытие одно — `_sim_check_positions`, и оно вызывает
   `executor.check_positions(...)`, которого у `PortfolioExecutor` нет (есть только
   `check_positions_range`). `AttributeError` ловится в `price_simulator.py` и логируется
   каждую секунду, пока есть открытые позиции. Касается любой portfolio-стратегии, не только MR.
   Смежно: post-only заявки в живом оркестраторе не обрабатывает никто —
   `process_post_only_entries` / `exits` вызываются только из бэктестера.
   Candidate-режим (single-timeframe) закрывает штатно через `SignalExecutor.check_positions`.

2. **Фандинг.** В `funding_rates` данные только за январь 2024: PnL любого бэктеста после
   февраля 2024 — до фандинга. Перед решением о реальном капитале нужна валидация модели
   фандинга на пересекающихся данных (план MR, Task 9).

## Не блокирует

3. **Состав вселенной в бэктесте зависит от живого ответа API.** `PortfolioExecutor.open_position`
   молча отвергает символ без спецификации инструмента, а спецификации берутся из кэша
   `data/cache/bybit_instruments.json` (TTL 24 ч), при промахе — из текущего списка Bybit. За
   одни сутки список изменился с 874 до 885 спецификаций: AVAXUSDT во вчерашнем отсутствовал,
   в сегодняшнем есть. Одна и та же команда в разные дни может прогнать разную эффективную
   вселенную. Источник — поправка 2 в
   [`cycle-2/1-signal-definition.md`](../research/mean-reversion/cycle-2/1-signal-definition.md).

4. **`PnLSummary.sharpe_ratio` — не тот ряд и не те единицы.** `PnLTracker.close_position`
   дописывает в `equity_history` запись с настенным временем и эквити без нереализованного PnL
   остальных позиций; эквити пишется на каждом тике (часовые доходности), а множитель —
   дневной `sqrt(365)`. Знак не искажается, величина — примерно годовой Sharpe, делённый на
   `sqrt(24)`. Вердикт цикла 2 MR поэтому считался по таблице `equity` БД прогона
   (`scripts/mr_decision_rule.py`). Источник — раздел 8
   [`cycle-2/3-preregistration.md`](../research/mean-reversion/cycle-2/3-preregistration.md).

5. **time-stop сдвигается на интервал каденции при post-only входе.** `opened_at` пишется
   временем исполнения лимитной заявки, то есть не раньше чем через бар после тика решения.
   При `rebalance_hours > 1` проверка на тике `T + max_holding` видит возраст на бар меньше, и
   выход уезжает на следующий тик (так в v4 вышло 60 ч вместо 48). При каденции в один бар и
   рыночном входе не возникает. Механизм установлен чтением кода — раздел 8
   [`cycle-2/1-signal-definition.md`](../research/mean-reversion/cycle-2/1-signal-definition.md).

6. **Объявленные, но не действующие настройки.**
   - корреляционный фильтр инертен для market-стратегий: бэктестер передаёт риск-движку
     `cross_section = None`, а `_apply_correlation_filter` при `features is None` пропускает всех;
   - `portfolio.risk.max_correlation` и соседние поля фабрика не читает — берёт их из
     глобального `risk`;
   - `--max-leverage` и `--maintenance-margin-buffer` в `scripts/walk_forward.py` не
     используются: риск-движок строится из `portfolio.risk`;
   - `portfolio.rebalance_persist` не читает никто.

7. **`mean_reversion.max_positions` декоративен.** Поле не читается в
   `MeanReversionStrategy.evaluate_market`; книгу ограничивает `portfolio.risk.max_positions`.
   Зафиксировано `xfail(strict=True)` в `tests/test_mean_reversion_fields_wired.py`.

8. **`scripts/signal_frequency_check.py` — устаревшая параллельная имитация стратегии.** Она
   пять раз расходилась с боевым кодом (закрытие цикла 1 MR, открытый пункт 3); её заменил
   `scripts/count_mr_trades.py`, который гоняет реальный `Backtester`. Кандидат на удаление
   вместе с `scripts/test_signal_freq.py` и `scripts/test_signal_freq2.py`.

9. **Одноразовые скрипты в корне репозитория.** `check_*.py`, `debug_*.py`, `diagnose_*.py`,
   `find_bug.py`, `fix_schemas.py`, `trace_maxpos.py`, `validation_report_20260710_134924.txt` —
   следы старых отладочных сессий. Документацией не являются; кандидаты на удаление после
   проверки, что на них ничего не ссылается.
