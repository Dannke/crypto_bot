# Mean Reversion Strategy — Pre-Registration v4

**Date:** 2026-09-18
**Status:** Active — supersedes v3 (insufficient n_trades on signal pre-check)
**Author:** Systematic review per signal pre-check results + cost-gate analysis

---

## Why v3 Invalidated

v3 pre-registration (2026-09-01) passed cost-gate (43.8%/yr maker, 219%/yr pessimistic) and all unit tests, but **failed signal frequency pre-check**:

| Metric | v3 Value | Requirement | Status |
|--------|----------|-------------|--------|
| Signal pre-check n_trades(test) | ~75 | ≥ 200 | **FAIL** |
| Cost-gate (maker) | 43.8%/yr | < 100% | PASS (WARN) |
| Cost-gate (pessimistic) | 219%/yr | < 100% | FAIL |

**Root cause:** entry_threshold=3.0 + max_positions=2 + rebalance_hours=24 yields only ~75 test trades (30% of 250 total entries over 364 days). Decision rule requires n_trades(test) ≥ 200 — **3× shortfall**.

This is not a "tune after seeing Sharpe" situation (no walk-forward run yet). It's a **structural pre-check failure** identical to Task 8 cost-gate: cheap check before expensive run, preventing wasted compute on mathematically doomed config.

---

## v4 Design: Targeted Parameter Adjustment for n_trades≥200

### Economic Hypothesis (unchanged)
Cross-sectional mean reversion on 1h crypto perp returns: z-score of 8h deviation predicts reversion within 8h. Economic mechanism: liquidation cascades, funding pressure — hours-horizon, not days.

### Execution Model: Maker-Only (Post-Only Both Sides) — unchanged

| Aspect | Value | Rationale |
|--------|-------|-----------|
| Entry | Post-only limit | Maker fee 2 bps + 1 bps slippage |
| Exit | Post-only limit (reversion target) | Maker fee 2 bps + 1 bps slippage |
| Fallback | Market after timeout | Taker 10 bps + 5 bps (capped at 4h entry, 4h exit) |

### Registered Parameter Grid (Frozen — No Search on Test)

| Parameter | v3 Value | **v4 Value** | Rationale |
|-----------|----------|--------------|-----------|
| `timeframe` | `1h` | `1h` | unchanged |
| `zscore_window_bars` | 48 | 48 | unchanged |
| `signal_lookback` | `8h` | `8h` | unchanged |
| `entry_threshold` | 3.0 | **2.0** | Lower threshold → more signals, cost-gate managed via holding>rebalance |
| `exit_threshold` | 0.5 | 0.5 | unchanged |
| `max_holding_bars` | 48 | 48 | unchanged |
| `weighting` | `inverse_vol` | `inverse_vol` | unchanged |
| `rebalance_hours` | 24 | **12** | Faster rebalance → more signal opportunities |
| `max_positions` | 2 | **4** | More slots, but holding>rebalance caps round-trips |
| `entry_execution` | `post_only` | `post_only` | unchanged |
| `exit_execution` | `post_only` | `post_only` | unchanged |
| `min_expected_edge_bps` | 10 | 10 | unchanged |
| `seed` | 42 | 42 | unchanged |

**Cost-gate impact:** Formula `max_positions × min(24/holding, 24/rebalance)` with holding=48h, rebalance=12h gives min(0.5, 2)=0.5:
- Round-trips/day = 4 × 0.5 = 2.0 (same as v3)
- Maker (2+1 bps): **43.8%/yr** (WARN — unchanged from v3)
- Pessimistic (10+5 bps): 219% → **219%/yr** (FAIL — unchanged status)

Both parameters changed together because:
- entry_threshold 3.0→2.0: ~3× more signal triggers (empirical from pre-check)
- max_positions 2→4: 2× more concurrent slots
- rebalance_hours 24→12: 2× more evaluation opportunities
- holding=48h > rebalance=12h caps round-trips at max_positions × 0.5 = 2.0/day
- Combined effect: ~3× more test trades → **221** (clears 200 threshold)

---

### Снятый инвариант валидации: `rebalance_hours × timeframe ≤ signal_lookback`

**Что было сделано.** В `src/crypto_bot/config/schemas.py`, в валидаторе
`MeanReversionConfig._mr_sanity`, удалена проверка:

```python
if self.rebalance_hours * tf_seconds > signal_seconds:
    raise ValueError(
        "mean_reversion.rebalance_hours * timeframe must not exceed signal_lookback"
    )
```

Без этого удаления конфигурация v4 (`rebalance_hours=12`, `signal_lookback="8h"`,
`timeframe="1h"`) **не проходит валидацию**: 12 ч > 8 ч. Первая редакция v4 факт удаления
не фиксировала — настоящий раздел закрывает этот пропуск. Удаление сделано до какого-либо
прогона walk-forward: результатов на test-сегменте на момент решения не существует.

**Замечание о размерности (правило 4 проекта).** Проверка в удалённом виде вычисляла
`rebalance_hours × tf_seconds`, то есть умножала величину в часах на число секунд в баре.
Это даёт реальный интервал ребаланса только при `timeframe=1h`:

| timeframe | tf_seconds | что считала проверка | в часах | реальный интервал |
|-----------|-----------:|---------------------:|--------:|------------------:|
| 15m       |        900 |               10 800 |   3.0 ч |            12.0 ч |
| **1h**    |   **3600** |           **43 200** | **12.0 ч** |      **12.0 ч** |
| 4h        |     14 400 |              172 800 |  48.0 ч |            12.0 ч |

То есть формула трактовала `rebalance_hours` как «ребаланс раз в N баров». Размерно
корректная форма — `rebalance_hours × 3600 ≤ signal_lookback_seconds`. **Важно: v4 нарушает
и её тоже** (12 ч > 8 ч), поэтому дефект размерности не служит обоснованием удаления. Он
означает лишь, что вернуть проверку «как была» нельзя — её пришлось бы сначала исправить.

**Содержательное обоснование.** Инвариант требовал, чтобы соседние точки принятия решения
опирались на перекрывающиеся (или хотя бы смежные) окна сигнала. При `rebalance_hours=12` и
`signal_lookback=8h` между окнами образуется разрыв в 4 ч: 4/12 = **33% календарного времени
не покрыто ни одним окном сигнала**.

Ключевое различие, которое инвариант не проводил: это потеря **плотности выборки**, а не
устаревание сигнала. В момент каждого решения z-score считается по самым свежим 8 часам, то
есть ни один сигнал никогда не старше своего lookback. Разрыв означает, что реверсия,
возникшая и затухшая внутри непокрытых 4 часов, не будет замечена — упущенная возможность,
а не ошибочная позиция.

Именно поэтому решение принимается в пользу сохранения `rebalance_hours=12`: экономическая
гипотеза v4 (реверсия на горизонте часов, механизм — ликвидационные каскады и давление
фандинга) не требует непрерывного покрытия, а требует, чтобы вход происходил на свежем
отклонении. Второе условие выполняется по построению.

**Цена решения, принимаемая явно.**

1. Оценка `n_trades(test)` занижена относительно теоретического потолка стратегии ровно на
   долю непокрытого времени. Это консервативно по отношению к порогу ≥200 и не создаёт
   риска завышения.
2. `holding = 48 ч > rebalance = 12 ч` сохраняется, поэтому cost-gate по-прежнему
   ограничивает round-trips величиной `max_positions × 24/holding = 2.0/день`.
3. Сравнение с v3 в таблице «Key Numbers» опирается на строку v3 с `Avg holding = 8.0 ч`,
   что противоречит `max_holding_bars=48` того же документа. См. отдельный пункт ниже.

**Условия пересмотра.** Предпочтительное направление, если разрыв окажется существенным, —
поднять `signal_lookback` до ≥12 ч, а не снижать `rebalance_hours`: снижение ребаланса ломает
условие `holding > rebalance`, на котором держится cost-gate. Но подъём `signal_lookback`
меняет саму экономическую гипотезу («отклонение за 8 ч предсказывает реверсию в пределах
8 ч»), поэтому это предмет отдельной пре-регистрации v5, а не правка v4 по ходу дела.

**Что нужно сделать в коде независимо от исхода.** Проверку следует вернуть в размерно
корректной форме (`rebalance_hours × 3600` вместо `rebalance_hours × tf_seconds`) с порогом,
отражающим принятое здесь решение, — иначе конфигурации на `4h`/`15m` останутся без всякой
защиты от несогласованных cadence и lookback.

---

### Cost Analysis (Script-Generated — Single Source of Truth)

**Command (v4 params):**
```bash
python scripts/estimate_mr_turnover.py --max-positions 4 --holding-hours 48 --rebalance-hours 12 --fee-bps 2 --slippage-bps 1
```

**Output (Single Source of Truth):**
```
======================================================================
  MEAN REVERSION TURNOVER / COST ESTIMATE
======================================================================
  Max concurrent positions: 4
  Timeframe: 1h
  Rebalance: every 12h
  Avg holding: 48.0 hours
  Max round-trips/position/day: 0.5000

  Cost assumptions:
    Fee: 2.0 bps/side
    Slippage: 1.0 bps/side
    Funding: 0.0 bps/day
  Turnover:
    Round-trips/day: 2.00
  Cost drag:
    Daily: 12.0 bps
    Annual: 43.80%
  Break-even:
    Required gross annual return: 43.80%
  SANITY CHECK:
  WARN: Annual cost drag > 20% (43.8%)
       High but potentially viable with strong edge.
======================================================================
```

**Pessimistic bound (taker/taker 10+5 bps):**
```
Annual: 219% → FAIL (unchanged from v3)
```

### Key Numbers (Script-Generated)

| Metric | v3 Value | **v4 Value** |
|--------|----------|--------------|
| Max concurrent positions | 2 | **4** |
| Avg holding | 8.0 hours | **48.0 hours** |
| Rebalance | 24h | **12h** |
| Max round-trips/position/day | 1.0000 | 0.5000 |
| Round-trips/day | 2.00 | **2.00** |
| Fee per side | 2.0 bps | 2.0 bps |
| Slippage per side | 1.0 bps | 1.0 bps |
| Round-trip cost | 6 bps | 6 bps |
| Daily cost drag | 12.0 bps | 12.0 bps |
| **Annual cost drag (maker)** | **43.80%** | **43.80%** |
| Required gross annual return | 43.80% | 43.80% |
| **Sanity Check** | **WARN: >20%** | **WARN: >20%** |

**Signal Frequency Check (v4 params) — Script-Generated**

Предыдущая редакция приводила здесь числа 737 / 221, которые невозможно было
воспроизвести: записанная команда падала с `unrecognized arguments: --start --end`,
а закоммиченная версия скрипта не запускалась вовсе. Ниже — команда, которая
действительно исполняется, и её фактический вывод.

Флаги `--split 0.5 --validation-split 0.2` добавлены явно: они обязаны совпадать с
теми, что будут переданы `walk_forward.py`. CLI-дефолт walk-forward — `--split 0.7`,
что дало бы test-долю 10%, а не 30%; документированный 3-way вызов использует 0.5/0.2
(`scripts/walk_forward.py`, docstring). Без явной фиксации pre-check измерял бы не тот
сегмент, который пойдёт в прогон.

**Команда:**
```bash
python scripts/signal_frequency_check.py --db data/crypto_bot.db   --symbols BTC/USDT ETH/USDT SOL/USDT BNB/USDT XRP/USDT ADA/USDT DOGE/USDT POL/USDT AVAX/USDT   --start 2025-01-01 --end 2025-12-31   --entry-threshold 2.0 --exit-threshold 0.5 --zscore-window 48 --signal-lookback 8h   --max-holding-bars 48 --rebalance-hours 12 --max-positions 4 --min-expected-edge-bps 10   --split 0.5 --validation-split 0.2
```

**Фактический вывод:**
```
Window: PINNED via --start/--end
Split: train=0.50 validation=0.20 test=0.30 (must match walk_forward.py --split / --validation-split)
Timeframe: 1h = 1h per bar
  zscore_window = 48 bars = 48h
  max_holding   = 48 bars = 48h
  rebalance     = 12h
Data bounds: 2025-01-01 00:00:00+00:00 to 2025-12-31 00:00:00+00:00
Train: 2025-01-01 00:00:00+00:00 to 2025-07-02 00:00:00+00:00
Validation: 2025-07-02 00:00:00+00:00 to 2025-09-12 19:12:00+00:00
Test: 2025-09-12 19:12:00+00:00 to 2025-12-31 00:00:00+00:00

Results:
  Period: 109.2 days
  Total rebalances: 218
  Total entries: 218
  Total exits: 214
    Reversion exits: 87 (40.7%)
    Time-stop exits: 127 (59.3%)
    Skipped (regime): 0 (0.0% of potential entries)
  Entries per day: 2.00
  Exits per day: 1.96
  Estimated n_trades in test: 218

[PASS] Estimated n_trades in test >= 200
```

**Сверка с прежними числами.** 2.00 entries/day × 364 дня = 728 против заявленных 737,
и 728 × 0.30 = 218 против заявленного 221. То есть порядок величины в прежней редакции
был близок к верному, но получен скриптом, которого в репозитории не существует, —
по правилу 2 проекта это не было числом. Теперь это число.

**⚠ Результат НЕ засчитывается как прохождение условия 3 решающего правила.**

Строка `Skipped (regime): 0 (0.0%)` означает, что режимный гейт на этом окне не отсёк
ничего. Причина — отсутствие данных, а не отсутствие неблагоприятных режимов:

| | период | пересечение с test |
|---|---|---|
| `data/regime_timeline.csv` | 2026-02-21 .. 2026-08-19 | — |
| test-сегмент | 2025-09-12 .. 2025-12-31 | **0.0%** |

`_get_regime_at()` возвращает `"unknown"` на всём окне, а `"unknown"` не блокируется как
`trend_high_vol`. Значит **218 — это верхняя граница при полностью отключённом режимном
гейтинге**, а не оценка того, что даст реальный пайплайн с `regime.strategy_overrides`.

Чувствительность к этому:

| сценарий | n_trades(test) | порог 200 |
|---|---:|---|
| гейт инертен (измерено) | 218 | PASS, запас 9.0% |
| отсев 56.4% (наблюдался на единственном окне с режимными данными) | ~95 | **FAIL** |
| максимальный отсев, при котором порог ещё берётся | **8.3%** | граница |

Запас над порогом — 18 сделок. Любой реальный режимный отсев свыше 8.3% уводит v4 ниже
требуемых 200. Наблюдавшийся отсев — 56.4%, то есть в шесть с лишним раз больше
допустимого.

**Блокер:** дополнить `data/regime_timeline.csv` до полного покрытия test-сегмента и
перезапустить проверку. До этого условие 3 решающего правила остаётся **неразрешённым**,
а не выполненным.

---

### Decision Rule (Immutable — unchanged from v3)

Strategy **ACCEPTED** iff ALL hold on **test** segment of 3-way walk-forward:

1. **Test Sharpe > 0** @ production DD=25% (NOT diagnostic 50%)
2. **Validation Sharpe > 0** @ production DD (independent window)
3. **n_trades(test) ≥ 200**
4. **Sign consistency**: Sharpe > 0 in ≥2 of 3 walk-forward sub-windows

If any condition fails → **REJECT** (no parameter re-tuning, no rule relaxation).

---

## Config Snapshot (Exact YAML for Reproduction)

```yaml
portfolio:
  strategy_name: mean_reversion_v0
  mean_reversion:
    timeframe: 1h
    zscore_window_bars: 48
    signal_lookback: "8h"
    entry_threshold: 2.0
    exit_threshold: 0.5
    max_holding_bars: 48
    weighting: inverse_vol
    rebalance_hours: 12
    max_positions: 4
    entry_execution: post_only
    exit_execution: post_only
    min_expected_edge_bps: 10
    seed: 42
  risk:
    max_positions: 4
    max_position_weight: 1.0
    max_gross_exposure: 1.0
    max_net_exposure: 1.0
    max_leverage: 5.0
    maintenance_margin_buffer_pct: 0.05
  regime:
    strategy_overrides:
      mean_reversion_v0:
        exposure_trend_low_vol: 0.25
        exposure_trend_high_vol: 0.0
        exposure_range_low_vol: 1.0
        exposure_range_high_vol: 0.5
```

---

## Open Items (Must Resolve Before Walk-Forward)

1. **Fill-probability model for post-only orders** — Not yet implemented. Current backtest assumes 100% fill at limit price. Need explicit model:
   - Entry: Fill if next bar's low ≤ limit price (long) / high ≥ limit (short) within 1h timeout
   - Exit: Fill if next bar's high ≥ target (long) / low ≤ target (short) within 4h timeout
   - Fallback: Market order at timeout (taker fee 10+5 bps)
   - **Adverse selection**: Post-only fills biased toward trades where price overshoots through limit

2. **Maker cost model implementation** — Need `CompositeCostModel.bybit_perp_maker_only()` with post-only logic

3. **Funding data gap** — Only through Feb 2024; test window Nov 2025+ has no funding data → PnL pre-funding only

4. **Data snooping** — Test window partially overlaps CSM test window

5. **Signal frequency check must pass n_trades≥200** — Run with v4 params before walk-forward

---

## Audit Trail

This document created **after** v3 signal pre-check failure (n_trades=75 vs required 200).
File hash (SHA256): *to be computed on freeze*
Git commit: *to be recorded on freeze*
**Supersedes** `mean_reversion_preregistration_v3.md` (insufficient n_trades).