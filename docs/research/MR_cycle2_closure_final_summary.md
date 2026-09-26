# Mean Reversion, цикл 2 — итог: REJECT по зарегистрированной decision rule

**Дата:** 2026-09-26
**Вердикт (production):** **REJECT** — условия 1, 2 и 4 не выполнены, условие 3 выполнено.
**Регистрация:** `docs/research/mr_cycle2_preregistration.md`, заморожена в `c0b0375`. После
заморозки менялся только документ решения (поправка 2, `4e49f19`, `589f692`) — конфиг, код,
скрипты и регистрация не менялись.
**Прогоны:** на `589f692`; production — 2026-09-26 07:28–08:22 UTC, diagnostic_widened —
08:23–09:08 UTC. БД прогонов лежат в `data/backtests/mr_cycle2_production/` и
`data/backtests/mr_cycle2_diagnostic_widened/` (каталог `data/` git не отслеживает).

---

## 1. Вердикт — вывод одной команды (регистрация, раздел 7.4)

```bash
PYTHONIOENCODING=utf-8 python scripts/mr_decision_rule.py --label production --validation-db data/backtests/mr_cycle2_production/validation.db --test-db data/backtests/mr_cycle2_production/test.db --test-start "2025-11-24 00:00" --test-end "2026-09-17 00:00"
```

Вывод (код возврата 1 = REJECT):

```
метка: production
validation БД: data/backtests/mr_cycle2_production/validation.db; тиков 4753 (2025-05-10 00:00 .. 2025-11-24 00:00)
test БД: data/backtests/mr_cycle2_production/test.db; тиков 7129 (2025-11-24 00:00 .. 2026-09-17 00:00)
test-сегмент: 2025-11-24 00:00 .. 2026-09-17 00:00
  под-окно 1: 2025-11-24 00:00 .. 2026-03-03 00:00  Sharpe = -0.0960
  под-окно 2: 2026-03-03 00:00 .. 2026-06-10 00:00  Sharpe = -0.4247
  под-окно 3: 2026-06-10 00:00 .. 2026-09-17 00:00  Sharpe = 0.0000
1. Sharpe(test) > 0                                   -0.1858  FAIL
2. Sharpe(validation) > 0                             -0.2337  FAIL
3. n_trades(test) >= 200                                  233  PASS
4. Sharpe > 0 в >= 2 из 3 под-окон                     0 из 3  FAIL
ВЕРДИКТ (production): REJECT
```

Третье под-окно даёт ровно 0.0000: аварийный стоп сработал на test раньше, чем оно началось, и
эквити в нём плоское. Это раскрытый заранее исход (регистрация, раздел 8, п. 12), а не повод
пересматривать правило.

**Отказ не вызван аварийным стопом.** Та же команда по прогону `diagnostic_widened` (стоп
отключён, справочно — в вердикте не участвует):

```bash
PYTHONIOENCODING=utf-8 python scripts/mr_decision_rule.py --label diagnostic_widened --validation-db data/backtests/mr_cycle2_diagnostic_widened/validation.db --test-db data/backtests/mr_cycle2_diagnostic_widened/test.db --test-start "2025-11-24 00:00" --test-end "2026-09-17 00:00"
```

```
метка: diagnostic_widened
validation БД: data/backtests/mr_cycle2_diagnostic_widened/validation.db; тиков 4753 (2025-05-10 00:00 .. 2025-11-24 00:00)
test БД: data/backtests/mr_cycle2_diagnostic_widened/test.db; тиков 7129 (2025-11-24 00:00 .. 2026-09-17 00:00)
test-сегмент: 2025-11-24 00:00 .. 2026-09-17 00:00
  под-окно 1: 2025-11-24 00:00 .. 2026-03-03 00:00  Sharpe = -0.0960
  под-окно 2: 2026-03-03 00:00 .. 2026-06-10 00:00  Sharpe = -0.1032
  под-окно 3: 2026-06-10 00:00 .. 2026-09-17 00:00  Sharpe = -0.4687
1. Sharpe(test) > 0                                   -0.2201  FAIL
2. Sharpe(validation) > 0                             -0.2153  FAIL
3. n_trades(test) >= 200                                  352  PASS
4. Sharpe > 0 в >= 2 из 3 под-окон                     0 из 3  FAIL
ВЕРДИКТ (diagnostic_widened): REJECT
```

Без стопа все три под-окна test отрицательные, validation и test — тоже.

---

## 2. Что вердикт означает и чего не означает

- **Спецификация цикла 2 — REJECTED по decision rule.** В отличие от v1–v4 (INVALIDATED
  структурным pre-check, гипотеза не тестировалась), здесь гипотеза протестирована так, как
  зарегистрирована: после 4-часового движения с `|z| >= 3.0` (валидированный `z`, гейты
  G1–G3 PASS) возврат цены в следующие 4 часа не окупил издержки taker/taker 20 bps на круг ни
  на validation, ни на test, ни в одном под-окне test.
- **Без пересмотра.** По правилу — без настройки параметров, без ослабления условий и без
  повторного прогона с другими параметрами на этих данных.
- **Test-сегмент 2025-11-24..2026-09-17 для MR израсходован.** Любая новая регистрация
  mean reversion — другой горизонт, рыночно-нейтральный остаток, maker-исполнение с моделью
  заполнения — на этом отрезке уже не будет чистым тестом: её test должен лежать в данных
  после 2026-09-17 или в иной, заранее зарегистрированной схеме оценки.
- **Что не установлено.** Отклонена одна спецификация. Конструкции, отвергнутые в решении цикла
  (раздел 3), не проверялись, и на израсходованных данных проверены быть не могут.

---

## 3. Прогоны — команды и вывод

Проверки раздела 7.1 перед запуском: дерево чистое; с заморозки менялся только документ решения;
`check_config_snapshot.py --strict-unregistered` — MISMATCH 0 из 65, UNREGISTERED 0, код 0; все 8
символов `tradable=True`, `LinearPerpetual Trading` на свежем кэше спецификаций; каталогов прогона
не было. Команды и вывод обновления кэша — поправка 2 к решению.

**7.2 — production:**

```bash
PYTHONIOENCODING=utf-8 python scripts/walk_forward.py --mode portfolio --timeframe 1h --three-way --split 0.5 --validation-split 0.2 --start 2024-01-01 --end 2026-09-17 --symbols BTC/USDT ETH/USDT SOL/USDT XRP/USDT ADA/USDT DOGE/USDT BNB/USDT POL/USDT --db-dir data/backtests/mr_cycle2_production
```

```
Loading candles for 8 symbols (1h) from data/crypto_bot.db ...
  Pinned window: start=2024-01-01 end=2026-09-17 (UTC, end exclusive)
  Mode: portfolio  Strategy: mean_reversion_v0
  Train window: 2024-01-01 00:00 -> 2025-05-10 00:00 (11880 bars)
  Validation window: 2025-05-10 00:00 -> 2025-11-24 00:00 (4752 bars)
  Test window:  2025-11-24 00:00 -> 2026-09-17 00:00 (7128 bars)
  Train:      87 trades, PnL=-22.51%, Sharpe=-0.20, WinRate=42.5%, MaxDD=25.24%
  Validation: 163 trades, PnL=-20.41%, Sharpe=-0.24, WinRate=47.9%, MaxDD=27.09%
  Test:       233 trades, PnL=-21.59%, Sharpe=-0.16, WinRate=53.2%, MaxDD=25.26%
  Hint:   looks consistent (train PnL -22.5%, test PnL -21.6%)
```

**7.3 — diagnostic_widened:**

```bash
PYTHONIOENCODING=utf-8 python scripts/walk_forward.py --mode portfolio --timeframe 1h --three-way --split 0.5 --validation-split 0.2 --start 2024-01-01 --end 2026-09-17 --symbols BTC/USDT ETH/USDT SOL/USDT XRP/USDT ADA/USDT DOGE/USDT BNB/USDT POL/USDT --db-dir data/backtests/mr_cycle2_diagnostic_widened --override risk__emergency_drawdown_pct=100.0
```

```
Loading candles for 8 symbols (1h) from data/crypto_bot.db ...
  Pinned window: start=2024-01-01 end=2026-09-17 (UTC, end exclusive)
  Mode: portfolio  Strategy: mean_reversion_v0
  Train window: 2024-01-01 00:00 -> 2025-05-10 00:00 (11880 bars)
  Validation window: 2025-05-10 00:00 -> 2025-11-24 00:00 (4752 bars)
  Test window:  2025-11-24 00:00 -> 2026-09-17 00:00 (7128 bars)
  Train:      575 trades, PnL=-70.04%, Sharpe=-0.25, WinRate=50.6%, MaxDD=72.36%
  Validation: 207 trades, PnL=-21.68%, Sharpe=-0.19, WinRate=48.3%, MaxDD=31.79%
  Test:       352 trades, PnL=-31.58%, Sharpe=-0.20, WinRate=51.1%, MaxDD=41.87%
  Hint:   looks consistent (train PnL -70.0%, test PnL -31.6%)
```

Числа `Sharpe=` в этих выводах — `PnLSummary.sharpe_ratio` по ряду со вставками
`PnLTracker.close_position` (регистрация, раздел 8, п. 6). Статистика вердикта — ряд тиков из БД,
раздел 1. Знаки совпадают.

---

## 4. Техническая валидность

Границы окон совпали с зарегистрированным сплитом: 11880 / 4752 / 7128 баров. Сделок на train в
`diagnostic_widened` — 575, ровно `n_train(3.0)` счёта по правилу 6.3 (регистрация, раздел 4):
скрипт счёта и walk-forward действительно идут одним путём. В БД на один тик больше, чем баров
(4753 и 7129): часы бэктестера включают оба конца окна (регистрация, раздел 8, п. 10).

Потерянные тики и аварийные стопы — по stderr прогонов. Команда для production (для
`diagnostic_widened` — та же с файлом `wf_diagnostic.err`); сами логи — временные файлы сессии,
в репозиторий не входят:

```bash
python -c "
import re, datetime as d
p = r'...\wf_production.err'
t = open(p, encoding='utf-8', errors='replace').read()
ts = sorted(int(m) for m in re.findall(r'pipeline failed at ts=(\d+)', t))
f = lambda m: d.datetime.fromtimestamp(m / 1000, d.UTC).strftime('%Y-%m-%d %H:%M')
print('pipeline failed ticks:', len(ts), (f(ts[0]) + ' .. ' + f(ts[-1])) if ts else '-')
halts = re.findall(r'emergency drawdown ([0-9.]+)% >= ([0-9.]+)%', t)
print('emergency halts:', halts)
print('Traceback count:', t.count('Traceback'))
"
```

production:

```
pipeline failed ticks: 176 2024-01-01 00:00 .. 2024-01-08 07:00
emergency halts: [('25.09', '25.00'), ('26.93', '25.00'), ('25.11', '25.00')]
Traceback count: 176
```

diagnostic_widened:

```
pipeline failed ticks: 176 2024-01-01 00:00 .. 2024-01-08 07:00
emergency halts: []
Traceback count: 176
```

Все потерянные тики — прогрев классификатора режимов в начале данных, ровно как
зарегистрировано (регистрация, раздел 8, п. 9); других исключений нет. В production аварийный
стоп сработал один раз в каждом окне. Оснований для технического перезапуска нет.

---

## 5. Дальше — не решается этим документом

План (`mean_reversion_plan.md`, раздел 9, «Если REJECT»): переход к приоритету №4
(funding/basis) или №1 (regime-aware как самостоятельная стратегия). Открытые пункты цикла, не
зависящие от вердикта: регистрация, раздел 9, и поправка 2 к решению (состав вселенной зависит от
живого ответа API).
