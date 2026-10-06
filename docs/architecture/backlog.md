# Бэклог — открытые пункты платформы

**Обновлено:** 2026-09-27. Единственное место, где собраны известные открытые пункты. Новый
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
   Фандинг живой portfolio-оркестратор не начисляет вовсе: `accrue_funding` вызывает только
   бэктестер, и эквити paper-режима funding-стратегии будет без её дохода. При подключении: если
   позиции восстанавливаются из БД после рестарта, `PaperPosition.funding_through_ms` нужно
   восстанавливать из `funding_payments` — иначе первый тик повторит записанный расчёт и упадёт на
   уникальном индексе v11. Источник — ревью миграции v11 при
   [Task 1](../research/funding-basis/plan.md#task-1--учёт-фандинга-f0f5--m--pr-a) плана funding/basis.

2. **Фандинг: данных нет.**
   - В `funding_rates` — только 2024-01-01 00:00 … 2024-02-01 00:00, восемь символов по 94
     события, у POL/USDT ни одного (приложение A документа Task 0 межсекционного carry). PnL любого
     бэктеста после февраля 2024 — до фандинга. Загрузка — план funding/basis, Task 4.
   - Учёт фандинга исправлен (F0–F5, Task 1 плана funding/basis). Все walk-forward прогоны до
     исправления шли без фандинга, в том числе на январе 2024: вердикты CSM и MR получены без него.
   - Перед решением о реальном капитале нужна валидация модели фандинга на пересекающихся данных
     (план MR, Task 9).

   Источник — [раздел 0.2](../research/funding-carry/cycle-1/1-hypothesis-and-decision-rule.md#02-не-переиспользуется-без-изменений--вопреки-брифу)
   и [приложение A](../research/funding-carry/cycle-1/1-hypothesis-and-decision-rule.md#приложение-a-покрытие-данных--только-счётчики-и-метки-времени)
   документа `funding-carry/cycle-1/1-hypothesis-and-decision-rule.md`.

## Не блокирует

3. **Состав вселенной в бэктесте зависит от живого ответа API.** `PortfolioExecutor.open_position`
   молча отвергает символ без спецификации инструмента, а спецификации берутся из кэша
   `data/cache/bybit_instruments.json` (TTL 24 ч), при промахе — из текущего списка Bybit, причём
   testnet или mainnet — по флагу того, кто обновил кэш первым (п. 13). Расхождение 874 и 885
   спецификаций с AVAXUSDT только во втором — не изменение листинга за сутки: файл за 2026-09-25
   совпадает со списком testnet ([поправка 3](../research/mean-reversion/cycle-2/1-signal-definition.md#поправка-3-2026-09-27-кэш-спецификаций-2026-09-25--список-testnet-а-не-ответ-mainnet)). Одна и та же
   команда в разные дни может прогнать разную эффективную вселенную. Источник чисел —
   [поправка 2](../research/mean-reversion/cycle-2/1-signal-definition.md#поправка-2-2026-09-26-после-заморозки-регистрации-c0b0375-до-прогона-walk-forward-avax-не-делистингован--исключение-держится-на-данных-а-не-на-спецификации)
   в `cycle-2/1-signal-definition.md`.
   Отказ не виден и в журнале решений: бэктестер результат `open_position` не читает
   (`simulation/backtester.py:614–620`). Так же молча проходит отказ по глобальному
   `risk.max_open_positions` — ограничению исполнителя, закреплённому
   `tests/test_portfolio_constraints.py`, — и книга больше этого лимита урезается без следа.
   Источник — E2 в
   [разделе 0.2](../research/funding-carry/cycle-1/1-hypothesis-and-decision-rule.md#02-не-переиспользуется-без-изменений--вопреки-брифу)
   документа `funding-carry/cycle-1/1-hypothesis-and-decision-rule.md`.

4. **`PnLSummary.sharpe_ratio` — не тот ряд и не те единицы.** `PnLTracker.close_position`
   дописывает в `equity_history` запись с настенным временем и эквити без нереализованного PnL
   остальных позиций; эквити пишется на каждом тике (часовые доходности), а множитель —
   дневной `sqrt(365)`. Знак не искажается, величина — примерно годовой Sharpe, делённый на
   `sqrt(24)`. Вердикт цикла 2 MR поэтому считался по таблице `equity` БД прогона
   (`scripts/mr_decision_rule.py`). Источник —
   [раздел 8](../research/mean-reversion/cycle-2/3-preregistration.md#8-известные-свойства-и-ограничения--раскрытие-не-гейты)
   `cycle-2/3-preregistration.md`.

5. **time-stop сдвигается на интервал каденции при post-only входе.** `opened_at` пишется
   временем исполнения лимитной заявки, то есть не раньше чем через бар после тика решения.
   При `rebalance_hours > 1` проверка на тике `T + max_holding` видит возраст на бар меньше, и
   выход уезжает на следующий тик (так в v4 вышло 60 ч вместо 48). При каденции в один бар и
   рыночном входе не возникает. Механизм установлен чтением кода —
   [раздел 8](../research/mean-reversion/cycle-2/1-signal-definition.md#8-открытые-пункты-handoff--статус-в-этом-цикле)
   `cycle-2/1-signal-definition.md`.

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

10. **Свечи в `data/crypto_bot.db` — спотовые, а исполняются линейные перпетуалы.** Оба клиента
    загрузки создают рынки с `defaultType: spot` и помечают их `spot: True`
    (`data/exchange.py:72`, `:182`; `data/exchange_sync.py:42`, `:72`); таблица `candles` рынок
    не хранит, по самой БД его не различить. Вход, выход и отметка по рынку во всех бэктестах —
    по споту, а фандинг, спецификации и исполнение — перпетуала: изменение базиса перп − спот в
    PnL не попадает. Насколько это влияет на вердикты CSM и MR, не измерялось. Для межсекционного
    carry смещение связано с сигналом, поэтому этому варианту нужны перп-свечи. Для cash-and-carry
    спотовые свечи после проверки D1 — ценовой ряд спот-ноги, перп-нога идёт по перп-свечам.
    Источник — D1 в
    [разделе 0.2](../research/funding-carry/cycle-1/1-hypothesis-and-decision-rule.md#02-не-переиспользуется-без-изменений--вопреки-брифу)
    документа `funding-carry/cycle-1/1-hypothesis-and-decision-rule.md`.

11. **Допуск инструмента в бэктесте не знает даты листинга.** Кэш спецификаций хранит текущий
    список Bybit без `launchTime` (`data/instruments.py`), и бэктест допускает инструмент на любую
    дату истории, если он торгуется сегодня. Цикл 2 MR держал POL/USDT во вселенной с 2024-01-01
    ([раздел 5](../research/mean-reversion/cycle-2/1-signal-definition.md#5-данные-и-сплит-нового-цикла)
    `cycle-2/1-signal-definition.md`), а перпетуал POLUSDT запущен 2024-09-05 08:30 UTC
    ([поправка 1](../research/csm/closure.md#поправка-1-2026-09-26-846-funding-events--не-измерение)
    к итогу CSM, команда 2): до этой даты на train позиции открывались в несуществующем
    инструменте по спотовым ценам. Вердикт цикла 2 это не затрагивает: validation и test начинаются
    после запуска. Исправление — допуск `launchTime ≤ t` (план funding/basis, Task 3).

12. **SL/TP отключаются по имени стратегии.** `PortfolioExecutor` выключает ценовые стопы только
    для `mean_reversion_v0` (`simulation/portfolio_executor.py:92`); любая другая портфельная
    стратегия получает ATR-стопы, даже если её гипотеза их не предполагает. Исправление — атрибут
    рынка или группы ног, а не имя стратегии (план funding/basis, Task 5). Источник — E1 в
    [разделе 0.2](../research/funding-carry/cycle-1/1-hypothesis-and-decision-rule.md#02-не-переиспользуется-без-изменений--вопреки-брифу)
    документа `funding-carry/cycle-1/1-hypothesis-and-decision-rule.md`.

13. **Кэш спецификаций общий для testnet и mainnet, а тесты бэктестера зависят от него и от API
    Bybit.** В портфельном режиме `Backtester.run_async` строит кэш спецификаций
    (`data/instruments.py::build_instrument_cache`): он читает `data/cache/bybit_instruments.json`,
    а когда файлу больше 24 ч — запрашивает список Bybit и перезаписывает файл. Адрес API выбирает
    `exchange.sandbox`: по умолчанию в схеме `True` — testnet, в `config/settings.yaml` `false` —
    mainnet, `.env` флаг не задаёт. Тесты с `Settings()` по умолчанию загружают список testnet,
    прогоны через `load_settings` — mainnet, и в кэше оказывается список того, кто обновил его
    первым после истечения TTL, без отметки об источнике. 2026-09-27 полный прогон тестов записал в
    кэш список testnet через 5 с после истечения TTL — команда и вывод в
    [поправке 3](../research/mean-reversion/cycle-2/1-signal-definition.md#поправка-3-2026-09-27-кэш-спецификаций-2026-09-25--список-testnet-а-не-ответ-mainnet)
    к `cycle-2/1-signal-definition.md`.

    Исследовательский прогон, запущенный после тестов в пределах TTL, получит вселенную и шаги
    объёма testnet — без ошибки и без следа в журнале (п. 3). Production-прогон цикла 2 MR шёл на
    снимке mainnet `bybit_instruments_2026-09-26T0709Z.json`; `scripts/reproduce_mr_cycle2.py`
    закрепляет его по sha256. Синтетические тикеры тестов `A/USDT`, `B/USDT`, `C/USDT` допускаются,
    потому что такие перпетуалы есть в обоих списках; `D/USDT` и `E/USDT` нет ни в одном, их позиции
    молча отвергаются. Исправление — кэш спецификаций как фикстура тестов (так сделано в
    `tests/test_simulation/test_funding_accounting.py`), источник списка в самом кэше и снимок
    спецификаций для бэктеста (план funding/basis, Task 3). Источник — установлено при
    [Task 1](../research/funding-basis/plan.md#task-1--учёт-фандинга-f0f5--m--pr-a) плана funding/basis.

14. **`PnLTracker.mark_to_market_equity` между закрытиями повторно учитывает прошлое движение
    цены.** Метод прибавляет нереализованный PnL к `current_equity`, а `record_equity`
    перезаписывает `current_equity` уже размеченным значением, так что следующий вызов считает
    прежнее движение ещё раз; при закрытии `_update_equity` сбрасывает расхождение. Этим методом
    живые оркестраторы пишут таблицу `equity` (`orchestrator.py`, `orchestrator_portfolio.py`):
    эквити paper-режима между закрытиями неверно. Бэктест его не использует — эквити тика
    пересчитывается от начального капитала (`PnLTracker.mark_to_market`). Тесты
    `tests/test_simulation/test_equity.py` и `tests/test_backtest/test_equity_tracking.py`
    проверяют только эквити после закрытия и расхождения не видят. Источник — установлено при
    [Task 1](../research/funding-basis/plan.md#task-1--учёт-фандинга-f0f5--m--pr-a) плана funding/basis, проверено на синтетике.

15. **`trades.ts_ms` в бэктесте — время записи, а не время бара.** `TradeRepository.insert`
    ставит `ts_ms = int(time.time() * 1000)` (`storage/db.py`), поэтому журнал сделок бэктеста
    хранит момент запуска, и два прогона одной команды расходятся в этом столбце в каждой строке.
    Время симуляции — в `positions.opened_at_ms` / `closed_at_ms`, `decisions.ts_ms` и
    `equity.ts_ms`; любой анализ БД бэктеста по `trades.ts_ms` (оборот во времени, сделки
    сегмента) неверен. `scripts/reproduce_mr_cycle2.py` исключает столбец из сравнения. Источник —
    воспроизведение цикла 2 MR при [Task 1](../research/funding-basis/plan.md#task-1--учёт-фандинга-f0f5--m--pr-a) плана funding/basis.
