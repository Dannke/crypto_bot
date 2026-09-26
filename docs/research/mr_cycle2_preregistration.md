# Mean Reversion, цикл 2 — Pre-registration

**Дата:** 2026-09-26
**Статус:** **заморожен** 2026-09-26 после вердиктов `preregistration-guardian` (все пункты
PASS) и `cost-gate-auditor` (MATCH) — раздел «Аудит перед заморозкой». Walk-forward **не
запускался**; решение о запуске принимается отдельно. `config/settings.yaml` закоммичен вместе
с кандидатом этого документа (`21186ba`) и после аудита не менялся.
**Стратегия:** `mean_reversion_v0` (`MeanReversionStrategy`). Это новый документ нового цикла,
а не редакция v1–v4 (closure, «Если направление MR будет выбрано снова»).
**Порядок цикла в git:** решение `7815f3b` → поправка 1 `8ddb752` → реализация `4d6bb8d` →
измерение `8f84e31` → инструменты счёта `87326b7` → этот документ.

Каждое число ниже — либо вывод команды, приведённой в этом же документе, либо ссылка на
датированный документ цикла, где оно так оформлено.

---

## 1. Гипотеза

На вселенной из 8 символов (BTC, ETH, SOL, XRP, ADA, DOGE, BNB, POL — `/USDT`, perpetual, 1h)
после необычного 4-часового движения — `|z| >= 3.0`, где `z` — определение цикла 2 —
цена в следующие 4 часа частично возвращается. Достаточно, чтобы стратегия, входящая против
движения и выходящая через 4 часа, имела положительный Sharpe после пессимистичных издержек
(taker / taker) и production risk-лимитов.

Механизмы, ради которых гипотеза проверяется: ликвидационные каскады и давление фандинга на
горизонте часов (`mean_reversion_plan.md`, раздел 1). Ни один не гарантирует эджа; это
проверка, а не установленный факт.

---

## 2. Decision rule — буквально

Стратегия **ACCEPTED** тогда и только тогда, когда на **production**-прогоне
(`risk.emergency_drawdown_pct = 25.0`) выполнены все четыре условия:

1. `Sharpe(test) > 0`;
2. `Sharpe(validation) > 0`;
3. `n_trades(test) >= 200`;
4. `Sharpe > 0` не менее чем в **2 из 3** под-окон test-сегмента.

Иначе — **REJECT**. Без повторной настройки параметров, без ослабления правила, без повторного
прогона на тех же данных с другими параметрами.

Определения, без которых правило не буквально:

- **Sharpe** — `crypto_bot.simulation.pnl.sharpe_from_returns(equity_returns(E))`, где `E` —
  эквити из таблицы `equity` БД сегмента, одна mark-to-market точка на тик, по возрастанию
  `ts_ms`. Та же функция даёт `PnLSummary.sharpe_ratio`, но `walk_forward.py` печатает его по
  другому ряду — со вставками `PnLTracker.close_position` (раздел 8, п. 6); вердикт считается
  только по ряду тиков из БД.
- **Единицы Sharpe** — часовые доходности эквити, умноженные на `sqrt(365)` (дневной
  множитель): не годовой Sharpe, а примерно годовой, делённый на `sqrt(24)`. Правило использует
  только знаки, поэтому вердикт от этого не зависит.
- **`n_trades(test)`** — число позиций со `status = 'closed'` в БД test-сегмента (то же
  множество, что `PnLSummary.total_trades`).
- **Под-окна** — три последовательных равных календарных части test-сегмента
  `[2025-11-24 00:00, 2026-09-17 00:00]`; внутренние границы округляются вниз до часа от
  начала; последняя часть включает правую границу. Доходность между соседними тиками
  относится к части более позднего тика (`subwindow_sharpes`).

Вердикт — вывод одной команды (раздел 7.4), без ручного пересчёта. Прогон
`diagnostic_widened` печатается с меткой и в вердикте не участвует.

**Отличие от предварительной формулировки решения (п. 6.5), принятое до любого результата
на validation и test.** Решение считало условия 1–2 по `PnLSummary.sharpe_ratio`, а под-окна —
отдельным скриптом `mr_subwindow_sharpe.py`. При реализации выяснилось, что `PnLSummary` берёт
Sharpe по ряду со вставками `PnLTracker.close_position` (раздел 8, п. 6), а ряд тиков в БД
прогона таких вставок не содержит. Если бы условия 1–2 считались по одному ряду, а условие 4 —
по другому, правило оценивало бы разные статистики. Поэтому все Sharpe-условия считаются по
ряду тиков из БД одной командой (`scripts/mr_decision_rule.py`, коммит `4d6bb8d`), а
отдельного `mr_subwindow_sharpe.py` нет.

---

## 3. Сигнал, данные, сплит — ссылкой

- **Определение `z`:** `mr_cycle2_signal_definition.md`, раздел 1:
  `z = ln(P_t / P_{t-4}) / (sqrt(4) * RMS 168 однобарных лог-доходностей, кончающихся на t-4)`.
  Обоснование и отвергнутые альтернативы — там же, разделы 2–3.
- **Статистическая валидация:** `mr_cycle2_signal_validation.md` — G1, G2, G3 **PASS**.
- **Вселенная и окно:** поправка 1 к решению — 8 символов, окно `[2024-01-01, 2026-09-17)`.
- **Сплит** (50 / 20 / 30, `calendar_split` на закреплённых свечах BTC/USDT; вывод скана в
  документе измерения):

| сегмент | начало | конец | часов | суток |
|---|---|---|---:|---:|
| train | 2024-01-01 00:00 | 2025-05-10 00:00 | 11880 | 495.00 |
| validation | 2025-05-10 00:00 | 2025-11-24 00:00 | 4752 | 198.00 |
| test | 2025-11-24 00:00 | 2026-09-17 00:00 | 7128 | 297.00 |

---

## 4. Порог входа — правило 6.3 решения, применённое механически

Правило (решение, п. 6.3): `K = {2.0, 2.5, 3.0}`; `n_train(k)` — число закрытых сделок
реального `Backtester` на train-сегменте при регистрируемой конфигурации, кроме
`entry_threshold = k` и `risk.emergency_drawdown_pct = 100.0` (diagnostic_widened);
`N_proj(k) = n_train(k) * onsets_test(k) / onsets_train(k)`; `k* = max{k : N_proj(k) >= 300}`,
проверка от 3.0 вниз до первого успеха.

Сырые начала при `k = 3.0` — из документа измерения (раздел 4): train 847, test 581.

**Команда** (коммит `87326b7`). Скрипт прогоняет train-сегмент той же функцией окна, что и
`walk_forward.py`, и печатает только счётчики — PnL, эквити и Sharpe не выводятся:

```bash
PYTHONIOENCODING=utf-8 python scripts/count_mr_trades.py --entry-threshold 3.0 --onsets-train 847 --onsets-test 581
```

**Вывод** (код возврата 0):

```
=== Прогон: реальный Backtester, train-сегмент, только счётчики ===
конфиг: config/settings.yaml; переопределено: entry_threshold=3.0, risk.emergency_drawdown_pct=100.0 (diagnostic_widened)
символы: 8; окно [2024-01-01, 2026-09-17); split=0.5, validation_split=0.2
train: 2024-01-01 00:00 .. 2025-05-10 00:00 = 11880 ч = 495.00 сут
сетка: h=4h, W=168, exit_threshold=None, max_holding_bars=4, rebalance_hours=1, weighting=equal, execution=market/market

=== Результат (счётчики, без PnL) ===
n_train = PnLSummary.total_trades = 575
закрытых позиций в БД прогона = 575; открытых на конец = 1
входов всего = 576; в сутки = 1.16
по сторонам: {'SHORT': 318, 'LONG': 258}
по символам: {'ADA/USDT': 63, 'BNB/USDT': 78, 'BTC/USDT': 93, 'DOGE/USDT': 76, 'ETH/USDT': 80, 'POL/USDT': 51, 'SOL/USDT': 51, 'XRP/USDT': 84}
closed_by: {'time_stop': 575}
удержание закрытых, часов: {4.0: 575}
пик одновременно открытых позиций = 4
отказы риск-движка по причинам (записи decisions): {'portfolio:REJECT_MAX_POSITIONS': 1}
тиков, потерянных пайплайном целиком (исключение в тике): 176; 2024-01-01 00:00 .. 2024-01-08 07:00

=== Правило 6.3: проекция на test ===
N_proj = 575 * 581 / 847 = 394.4
N_proj >= 300: PASS
входов в сутки на test (проекция) = 394.4 / 297.00 = 1.33
```

**Итог: `k* = 3.0`.** `N_proj(3.0) = 394.4 >= 300`: первая же проверенная точка удовлетворяет
правилу, поэтому `k = 2.5` и `k = 2.0` не прогонялись — правило останавливается на первом
успехе, и чисел о них не существует.

**Детерминизм.** Та же команда, выполненная раньше на коммите `4d6bb8d` — до того, как скрипт
начал печатать потерянные тики, — дала те же счётчики; её вывод — приложение A, он отличается
только отсутствием строки о потерянных тиках.

**Что ещё показал счёт — механика, без PnL:**

- все 575 закрытий — `time_stop`, и каждая позиция удерживалась ровно 4.0 ч: при каденции
  1 бар и рыночном входе time-stop точен;
- пик книги — 4 позиции; риск-движок отказал по `max_positions` один раз за весь train;
- все 176 потерянных пайплайном тиков — прогрев классификатора режимов в самом начале данных
  (раздел 8, п. 9);
- по сторонам: 258 LONG и 318 SHORT.

**Что означает порог 3.0 в частоте.** На train `P(|z| >= 3.0)` = 1.890% символо-часов — 7.00x
нормального (документ измерения, раздел 3). «3σ» здесь — утверждение о масштабе (три оценённых
стандартных отклонения 4-часовой доходности), а не о гауссовой редкости. Средний `|r_4h|` в
момент начала — 558.2 bps.

---

## 5. Сетка — полностью

Каждое поле, которое читает код на пути `walk_forward.py` → `Backtester` → стратегия →
`PortfolioRiskEngine` → `PortfolioExecutor`, и где оно зафиксировано. «Реш.» — документ
решения цикла.

### 5.1 `portfolio.mean_reversion`

| поле | значение | единицы | где зафиксировано | основание |
|---|---|---|---|---|
| `timeframe` | `1h` | — | реш. §1 | |
| `zscore_window_bars` | 168 | баров = 168 ч = 7 сут | реш. §2.3 | неделя: сезонная нейтральность, точность |
| `signal_lookback` | `4h` | 4 бара = 4 ч | реш. §3.4 | окно вмещает каскад целиком |
| `entry_threshold` | 3.0 | σ-единиц `z` | раздел 4 | правило 6.3 |
| `exit_threshold` | `null` | — | реш. §4.2 | выход по `\|z\|` не меряет возврат цены |
| `max_holding_bars` | 4 | бара = 4 ч | реш. §4.2 | горизонт возврата = горизонт сигнала |
| `weighting` | `equal` | — | реш. §4.5 | без скрытого окна волатильности |
| `rebalance_hours` | 1 | ч | реш. §4.1 | свежесть входа, точный time-stop |
| `max_positions` | 4 | позиций | реш. §4.6 | поле не читается стратегией (xfail); = реальному ограничителю |
| `long_percentile` / `short_percentile` | 0.80 / 0.20 | доли ранга | реш. §4.6 | `ceil(0.2 * 8) = 2` кандидата на сторону за тик |
| `entry_execution` / `exit_execution` | `market` / `market` | — | реш. §4.3 | пессимистичные издержки без модели заполнения |
| `min_expected_edge_bps` | 0 | bps | реш. §4.2 | фильтр определён через уровень выхода по `\|z\|` |
| `seed` | 42 | — | реш. §4.7 | ветка MR в фабрике поле не передаёт |

### 5.2 `portfolio` и `portfolio.risk`

| поле | значение | основание |
|---|---|---|
| `strategy_name` | `mean_reversion_v0` | |
| `volatility_sizing` | `false` | бэктестер передаёт риск-движку `sizing=None`, веса не перемасштабируются |
| `risk.max_positions` | 4 | реш. §4.6: одна полная выборка тика (2 + 2) |
| `risk.max_position_weight` | 1.0 | обязательно: одиночная позиция получает вес 1.0, а `_apply_max_position_weight` такие интенты **отвергает**, не урезает |
| `risk.max_gross_exposure` / `risk.max_net_exposure` | 1.0 / 1.0 | веса интента нормированы к сумме 1.0 — на интенте не связывают по построению; фактическую книгу не ограничивают (раздел 8, п. 7) |
| `risk.max_leverage` / `risk.maintenance_margin_buffer_pct` | 5.0 / 0.05 | маржинальная проверка по интенту с брутто 1.0 — не связывает; значения конфига не менялись |
| `risk.max_correlation`, `max_correlated_positions`, `enable_correlation_filter`, `correlation_lookback_bars` | 0.7, 2, true, 168 | **не читаются**: фабрика берёт эти поля из глобального `risk` (`build_portfolio_risk_engine`); объявлены для полноты снимка |
| `csm.*` | как в конфиге | к `mean_reversion_v0` не относятся |
| `rebalance_persist`, `regime_cadence_hours` | true, 1 | бэктестер не читает (`regime_cadence_hours` — только живой оркестратор) |

### 5.3 Глобальный `risk`

| поле | значение | как действует на MR в бэктесте |
|---|---|---|
| `emergency_drawdown_pct` | 25.0 | **production DD** (план, раздел 0); стоп липкий |
| `max_open_positions` | 5 | кап исполнителя; не связывает при `portfolio.risk.max_positions = 4` |
| `max_correlation`, `max_correlated_positions`, `enable_correlation_filter`, `correlation_lookback_bars` | 0.7, 2, true, 168 | фильтр **инертен** для MR: для market-стратегий бэктестер передаёт `cross_section = None`, а `_apply_correlation_filter` при `features is None` возвращает кандидатов без изменений (`portfolio/risk.py:431`) |
| `take_profit_risk_multiple`, `max_stop_distance_pct` | 2.0 по таймфреймам, 3.0 | уровни SL/TP считаются и пишутся в БД (`NOT NULL`), но как выходы для MR отключены (`_sltp_exits_enabled = not mr_is_active`) |
| `risk_per_trade_pct`, `max_daily_drawdown_pct`, `max_open_unrealized_drawdown_pct`, `equity_currency` | 1.0, 3.0, 3.0, USDT | портфельный путь не использует |

### 5.4 `regime` и `timeframes`

| поле | значение | основание |
|---|---|---|
| `regime.strategy_overrides.mean_reversion_v0.*` | все 1.0 | реш. §4.4: гейтинг нейтральный, явно |
| прочие поля `regime` | как в конфиге | на экспозицию MR не влияют (все множители 1.0); влияют только тем, что классификатору нужен прогрев (раздел 8, п. 9) |
| `timeframes.candles_per_tf` | 400 | не меньше `W + h + 1 = 173` (`validate_portfolio_csm`); срез истории в бэктестере отдельно зашит тем же числом 400 (`HistoricalCandleSource.slice`) |
| `timeframes.primary` | `[15m, 1h, 4h]` | кандидатный слой; портфельный прогон идёт на `--timeframe 1h` |

### 5.5 Параметры, зашитые в код

| что | значение | где |
|---|---|---|
| издержки market/market | `bybit_perp_default()`: `SimpleFeeModel()` 0.1% на сторону, проскальзывание 0 (`spread_pct = 0.0`) → **20 bps** round-trip | `simulation/walk_forward.py:227–237`, `simulation/backtester.py:603` |
| исполнение входа и выхода | по цене закрытия бара тика | `simulation/backtester.py`, `_run_portfolio_tick` / `_rebalance_positions` |
| начальный капитал | 10 000 | `PnLTracker.initial_equity` |
| модель фандинга | включена (`--enable-funding` по умолчанию), данных после 2024-02-01 нет | раздел 8, п. 3 |

---

## 6. Cost-гейт — пессимистичный

Стоимость round-trip — 20 bps: 10 bps комиссии на сторону, проскальзывание 0 (раздел 5.5).
Входов в сутки — проекция правила 6.3 на test, 1.33 (вывод раздела 4). Средний модуль движения
при входе — средний `|r_4h|` по **началам** `k = 3.0` на train, 558.2 bps (документ измерения,
раздел 4), как и предписывает критерий решения (п. 6.4(в)). Фактические входы — подмножество
начал (575 сделок из 847 начал на train), среднее по ним скрипт счёта не печатает.

**Команда:**

```bash
python scripts/estimate_mr_turnover.py --max-positions 4 --holding-hours 4 --rebalance-hours 1 --fee-bps 10 --slippage-bps 0 --entries-per-day 1.33 --mean-entry-move-bps 558.2 --max-breakeven-fraction 0.25
```

**Вывод** (код возврата 0):

```
======================================================================
  MEAN REVERSION TURNOVER / COST ESTIMATE
======================================================================
  Max concurrent positions: 4
  Timeframe: 1h
  Rebalance: every 1h
  Avg holding: 4.0 hours
  Max round-trips/position/day: 6.0000

  Cost assumptions:
    Fee: 10.0 bps/side
    Slippage: 0.0 bps/side
    Funding: 0.0 bps/day

  Turnover:
    Round-trips/day: 24.00

  Cost drag:
    Daily: 480.0 bps
    Annual: 1752.00%

  Break-even:
    Required gross annual return: 1752.00%

  SANITY CHECK:
  FAIL: Annual cost drag > 100% (1752.0%)
       Strategy CANNOT be profitable after costs with these parameters.
       Fix: increase holding period, reduce max_positions, or reduce cadence.
======================================================================
  SIGNAL-LIMITED BOUND:
    Entries/day (signal): 1.33
    Round-trips/day = min(24.00, 1.33) = 1.33
    Daily: 26.6 bps
    Annual: 97.09%
======================================================================
  PER-TRADE BREAKEVEN:
    Round-trip cost: 20.0 bps
    Mean |move| at entry: 558.2 bps
    rho_req = 20.0 / 558.2 = 3.58% of the entry move must revert
    PASS: rho_req <= 25%
======================================================================
```

**Вердикт гейта по критерию решения (п. 6.4(в)): PASS.** Для безубыточности в среднем должно
возвращаться 3.58% движения, на котором был вход; граница — 25%.

Как читать три числа:

- **(а) Граница ёмкости — FAIL, 1752.00%/год.** Формула skill `max_positions *
  min(24/holding, 24/rebalance)` = 24 round-trip в сутки предполагает, что все 4 слота заняты
  непрерывно и оборачиваются каждые 4 ч. Решение (п. 6.4(а)) заранее объявило эту строку не
  вердиктом гейта: оборот стратегии, входящей по порогу, ограничен частотой сигнала. Но это не
  опечатка: при таком обороте стратегия действительно была бы безнадёжна.
- **(б) Граница по сигналу — 1.33 round-trip в сутки, 26.6 bps в сутки, 97.09%/год.** В
  конвенции скрипта каждый round-trip стоит 20 bps **всего капитала**, то есть позиция =
  100% капитала. У одиночной позиции так и есть (раздел 8, п. 7), при нескольких
  одновременных — меньше. Планка высокая: в той же конвенции стратегии нужно порядка 97%
  годовых брутто только на безубыточность.
- **(в) На сделку — 3.58%.** То же требование, выраженное через сделку.

Единицы знаменателя. 558.2 bps — модуль **логарифмической** 4-часовой доходности, а издержки —
доля номинала. Для падений простой модуль движения меньше логарифмического, для ростов — больше;
при сопоставимом числе тех и других (448 LONG и 399 SHORT начал на train, документ измерения)
расхождение — второго порядка малости и вердикт не меняет (замечание `cost-gate-auditor`).

Гейт не утверждает, что возврат будет. Он утверждает, что гипотезе не нужен неправдоподобно
сильный возврат, чтобы окупить издержки. Ответ даёт только walk-forward.

---

## 7. Команды прогона — в этом порядке

### 7.1 Проверки непосредственно перед прогоном

Рабочее дерево чистое, `HEAD` — коммит заморозки. Каталогов
`data/backtests/mr_cycle2_production` и `data/backtests/mr_cycle2_diagnostic_widened` до прогона
не существует: бэктестер открывает существующую БД и дописывает в неё таблицу `equity`
(скрипт вердикта в этом случае остановится на дублях тиков, но прогон будет потерян).
Снимок конфига сверяется механически:

```bash
python scripts/check_config_snapshot.py docs/research/mr_cycle2_preregistration.md --strict-unregistered
```

Все 8 символов по-прежнему исполнимы для бэктестера (кэш спецификаций живёт 24 ч и
перезапрашивается с биржи; команда — приложение B документа решения): каждая из восьми строк
`BTC, ETH, SOL, XRP, ADA, DOGE, BNB, POL` обязана быть `LinearPerpetual Trading`. Иначе прогон
не запускается: символ без спецификации отвергался бы молча (поправка 1).

### 7.2 Production — единственный прогон, по которому выносится вердикт

```bash
python scripts/walk_forward.py --mode portfolio --timeframe 1h --three-way --split 0.5 --validation-split 0.2 --start 2024-01-01 --end 2026-09-17 --symbols BTC/USDT ETH/USDT SOL/USDT XRP/USDT ADA/USDT DOGE/USDT BNB/USDT POL/USDT --db-dir data/backtests/mr_cycle2_production
```

`--symbols` обязателен: по умолчанию `walk_forward.py` берёт все 9 символов
`MARKET_QUOTE_VOLUME`, включая AVAX. `--max-leverage` и `--maintenance-margin-buffer` не
передаются: `_run_single_window` их не использует, действуют значения `portfolio.risk`.

### 7.3 diagnostic_widened — справочно, не для вердикта

```bash
python scripts/walk_forward.py --mode portfolio --timeframe 1h --three-way --split 0.5 --validation-split 0.2 --start 2024-01-01 --end 2026-09-17 --symbols BTC/USDT ETH/USDT SOL/USDT XRP/USDT ADA/USDT DOGE/USDT BNB/USDT POL/USDT --db-dir data/backtests/mr_cycle2_diagnostic_widened --override risk__emergency_drawdown_pct=100.0
```

Единственный `--override` цикла, обоснование: план (Task 11) требует читать эдж и без
остановки аварийным стопом, с явной меткой. 100.0 — максимум схемы (`le=100`): стоп
срабатывает только при нулевом капитале. То же значение использовано в правиле 6.3 для счёта
сделок на train.

### 7.4 Вердикт

```bash
python scripts/mr_decision_rule.py --label production --validation-db data/backtests/mr_cycle2_production/validation.db --test-db data/backtests/mr_cycle2_production/test.db --test-start "2025-11-24 00:00" --test-end "2026-09-17 00:00"
```

Та же команда с `--label diagnostic_widened` и путями `mr_cycle2_diagnostic_widened` печатает
справочные числа. Вердикт — строка `ВЕРДИКТ (production): ...` первой команды.

---

## 8. Известные свойства и ограничения — раскрытие, не гейты

1. **Тяжёлые хвосты и сезонность `z`** (документ измерения, раздел 3): `P(|z|>=2)` = 5.721%,
   17:00 UTC — 10.605% против 3.509% в 13:00. Часть сигналов — обычная волатильность
   американской сессии, а не дислокация.
2. **Survivorship.** AVAX исключён, потому что его нет в сегодняшнем списке инструментов
   (поправка 1); влияние не измерено.
3. **Фандинг.** В `funding_rates` данные только до февраля 2024 (план, раздел 0, п. 3):
   PnL validation и test — до фандинга.
4. **Спецификации инструментов** берутся с биржи на момент запуска (TTL 24 ч): округление
   объёма и минимальный номинал — по сегодняшним правилам площадки, а не историческим.
5. **Единицы Sharpe** — раздел 2.
6. **`PnLSummary.sharpe_ratio` из вывода walk-forward — не статистика вердикта.**
   `PnLTracker.close_position` дописывает в `equity_history` запись с настенным временем и
   эквити без нереализованного PnL остальных позиций; `get_summary` считает Sharpe по ряду со
   вставками. Вердикт — по ряду тиков из БД.
7. **Размер позиции зависит от пути.** Веса нормируются по выбранной на тике книге, а
   удерживаемые позиции не ресайзятся: одиночная позиция — 100% капитала, при четырёх
   последовательных входах брутто — `1 + 1/2 + 1/3 + 1/4` капитала.
8. **Кластеризация.** Сигналы часто приходят несколькими символами в одном часе (документ
   измерения, раздел 4). `n_trades >= 200` — это не 200 независимых наблюдений.
9. **Прогрев.** Классификатор режимов падает, пока у него нет `vol_lookback_bars + trend_period`
   = 168 + 14 баров истории (`portfolio/regime.py:152`), а затем ещё несколько тиков
   (`Could not compute vol percentile`): 176 тиков
   пайплайна, 2024-01-01 00:00 .. 2024-01-08 07:00, теряются целиком (раздел 4, вывод счёта).
   `z` не определён, пока у символа нет `W + h + 1 = 173` закрытых баров: в скане это 11714
   символо-часов с `z` из 11880 часов train на символ (документ измерения, приложение).
   Касается только начала train; у validation и test история до начала сегмента полная.
10. **Границы окон.** Часы бэктестера включают оба конца окна, поэтому тик на границе
    сегментов попадает в оба соседних прогона — по одному тику на границу.
11. **Просмотренные окна** (решение, раздел 5): train v4 с PnL неверно откалиброванной
    стратегии лежит внутри нового train + validation; test-окно CSM почти совпадает с новым
    test.
12. **Липкий аварийный стоп.** Если на test сработает production-стоп 25%, торговля
    прекращается до конца сегмента, и условие 3 может не выполниться — это REJECT по правилу.

---

## 9. Открытые пункты — не блокируют прогон

1. `time_stop` 60 ч вместо 48 ч (handoff, п. 1) — механизм найден (решение, раздел 8); при
   каденции 1 бар и рыночном входе не возникает, что подтверждено счётом на train: удержание
   всех закрытых позиций — ровно 4 ч (раздел 4). Общая починка для каденции > 1 бара с
   post-only — отдельная задача.
2. `mean_reversion.max_positions` декоративен (`xfail(strict=True)`).
3. `PnLTracker.close_position` и единицы Sharpe (раздел 8, пп. 5–6) — отдельная задача: правка
   изменит числа прошлых отчётов, в этот цикл не входит.
4. `--max-leverage` / `--maintenance-margin-buffer` в `walk_forward.py` мертвы.
5. Нет `check_positions` в живом portfolio-оркестраторе — блокер paper-trading, к MR не специфичен.

---

## Аудит перед заморозкой

Кандидат — коммит `21186ba`; оба аудита — 2026-09-26, файлы аудиторами не менялись.

**`preregistration-guardian` — все пункты PASS, «можно замораживать».** Шаг 0:
`python scripts/check_config_snapshot.py docs/research/mr_cycle2_preregistration.md` и тот же
вызов с `--strict-unregistered` — `MISMATCH: 0 из 65`, `UNREGISTERED: 0`, `ВЕРДИКТ: PASS`, код
возврата 0. Проверено отдельно и подтверждено: применение правила 6.3 совпадает с его текстом в
решении буквально, а значение `entry_threshold` коммитом кандидата не менялось — только
комментарий; поправка 1 не читает цен и обоснована кодом (fail-closed в
`PortfolioExecutor.open_position`, отсутствие `AVAXUSDT` в кэше спецификаций); decision rule
буквальна и совпадает со `scripts/mr_decision_rule.py`; единственный `--override` обоснован;
pre-check не расходует test (бэктестер гонялся только на train); в ветке
`mean_reversion_v0` фабрики нет обходных литералов; нейтральный режимный гейтинг доезжает до
множителя; каждое число имеет источник. Напоминание аудитора: проверки раздела 7.1 зависят от
времени (кэш спецификаций — 24 ч) и выполняются заново непосредственно перед прогоном.

**`cost-gate-auditor` — MATCH.** Команда раздела 6 выполнена заново, её вывод совпал с блоком
документа построчно; 20 bps round-trip подтверждены по коду (`bybit_perp_default()`,
`is_maker=False` на входе и выходе, `spread_pct = 0.0`); скан перезапущен, его вывод совпал с
приложением документа измерения побайтно (558.2 bps, начала 847 и 581); ручной пересчёт всех
промежуточных чисел — без расхождений; объявление строки ёмкости «не вердиктом» skill не
противоречит. Два методологических замечания без влияния на вердикт учтены в разделе 6:
знаменатель `rho_req` — лог-доходность; 558.2 bps — среднее по началам, а не по фактическим
входам.

**Регрессия на `21186ba`.** Полный прогон без исключений (addopts проекта добавляют `-ra -q`):

```bash
python -m pytest tests/
```

```
664 passed, 19 skipped, 1 xfailed in 498.45s (0:08:18)
```

Все 19 пропусков — `tests/test_backtest/test_cross_validate.py:160`: тест сам пропускается, когда
в БД нет данных после `FIX_CUTOFF_MS`; `xfail` — `strict`-тест декоративного
`mean_reversion.max_positions`. `ruff check src/` — `All checks passed!`.

---

## Приложение A. Первый прогон счёта, коммит `4d6bb8d`

Та же команда, что в разделе 4, до коммита `87326b7`, который добавил строку о потерянных
тиках. Код возврата 0.

```
=== Прогон: реальный Backtester, train-сегмент, только счётчики ===
конфиг: config/settings.yaml; переопределено: entry_threshold=3.0, risk.emergency_drawdown_pct=100.0 (diagnostic_widened)
символы: 8; окно [2024-01-01, 2026-09-17); split=0.5, validation_split=0.2
train: 2024-01-01 00:00 .. 2025-05-10 00:00 = 11880 ч = 495.00 сут
сетка: h=4h, W=168, exit_threshold=None, max_holding_bars=4, rebalance_hours=1, weighting=equal, execution=market/market

=== Результат (счётчики, без PnL) ===
n_train = PnLSummary.total_trades = 575
закрытых позиций в БД прогона = 575; открытых на конец = 1
входов всего = 576; в сутки = 1.16
по сторонам: {'SHORT': 318, 'LONG': 258}
по символам: {'ADA/USDT': 63, 'BNB/USDT': 78, 'BTC/USDT': 93, 'DOGE/USDT': 76, 'ETH/USDT': 80, 'POL/USDT': 51, 'SOL/USDT': 51, 'XRP/USDT': 84}
closed_by: {'time_stop': 575}
удержание закрытых, часов: {4.0: 575}
пик одновременно открытых позиций = 4
отказы риск-движка по причинам (записи decisions): {'portfolio:REJECT_MAX_POSITIONS': 1}

=== Правило 6.3: проекция на test ===
N_proj = 575 * 581 / 847 = 394.4
N_proj >= 300: PASS
входов в сутки на test (проекция) = 394.4 / 297.00 = 1.33
```

---

## Config Snapshot

```yaml
portfolio:
  strategy_name: mean_reversion_v0
  volatility_sizing: false
  rebalance_persist: true
  regime_cadence_hours: 1
  risk:
    max_positions: 4
    max_position_weight: 1.0
    max_gross_exposure: 1.0
    max_net_exposure: 1.0
    max_leverage: 5.0
    maintenance_margin_buffer_pct: 0.05
    max_correlation: 0.7
    max_correlated_positions: 2
    enable_correlation_filter: true
    correlation_lookback_bars: 168
  csm:
    timeframe: 1h
    lookbacks: ["24h"]
    long_percentile: 0.9
    short_percentile: null
    weighting: equal
    rebalance_hours: 24
    seed: null
  mean_reversion:
    timeframe: 1h
    zscore_window_bars: 168
    signal_lookback: "4h"
    entry_threshold: 3.0
    exit_threshold: null
    max_holding_bars: 4
    weighting: equal
    rebalance_hours: 1
    max_positions: 4
    long_percentile: 0.8
    short_percentile: 0.2
    seed: 42
    entry_execution: market
    exit_execution: market
    min_expected_edge_bps: 0
risk:
  equity_currency: USDT
  risk_per_trade_pct: 1.0
  take_profit_risk_multiple:
    15m: 2.0
    1h: 2.0
    4h: 2.0
  max_stop_distance_pct: 3.0
  max_open_positions: 5
  max_daily_drawdown_pct: 3.0
  emergency_drawdown_pct: 25.0
  max_open_unrealized_drawdown_pct: 3.0
  max_correlation: 0.7
  max_correlated_positions: 2
  enable_correlation_filter: true
  correlation_lookback_bars: 168
regime:
  enabled: true
  reference: universe_basket
  trend_indicator: adx
  trend_period: 14
  trend_threshold: 25.0
  vol_lookback_bars: 168
  vol_percentile_high: 0.75
  hysteresis_min_dwell_bars: 6
  exposure_trend_low_vol: 1.0
  exposure_trend_high_vol: 0.5
  exposure_range_low_vol: 0.25
  exposure_range_high_vol: 0.0
  strategy_overrides:
    mean_reversion_v0:
      exposure_trend_low_vol: 1.0
      exposure_trend_high_vol: 1.0
      exposure_range_low_vol: 1.0
      exposure_range_high_vol: 1.0
timeframes:
  primary: ["15m", "1h", "4h"]
  candles_per_tf: 400
```
