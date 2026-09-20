---
name: research-methodology
description: Use whenever working on strategy research for this project — writing or reviewing a pre-registration document, a decision rule, a cost/turnover sanity gate, a signal-frequency check, a walk-forward run, or a strategy closure/status document (CSM, Mean Reversion, or any future strategy such as funding/basis or pairs). Also use when asked to verify, audit, or sanity-check any Sharpe/PnL/cost-drag number produced during this project's research.
---

# Методология исследования стратегий — извлечено из истории CSM и Mean Reversion

Этот проект уже один раз прошёл полный цикл от красивого, но фальшивого числа (+0.20 Sharpe на CSM)
до честного вердикта (−0.63) — и это стоило семи диагностических раундов. Твоя задача — не повторять
этот путь на следующей стратегии.

## Pre-registration — обязателен до первой строки кода
- Гипотеза, decision rule и параметрическая сетка фиксируются в датированном документе
  (`docs/research/<strategy>_preregistration.md`) **до** запуска первого теста на test-сегменте.
- Decision rule — буквальная конъюнкция условий (например: Test Sharpe > 0 при production DD, AND
  Validation Sharpe > 0, AND n_trades(test) ≥ порог, AND согласованность знака в k из m под-окон).
  Никаких "примерно" или мест под заполнение — числа фиксированы.
- Минимальный порог по числу сделок: исторически на этом проекте n=13–58 давали неинтерпретируемый
  шум, n=874 — весомый результат. Дефолтный консервативный порог для нового решения — не менее 200,
  если нет причины для другого числа, обоснованной заранее.

## Версионирование pre-registration (v1, v2, v3...)
- Новая версия создаётся только при явной, зафиксированной причине инвалидации предыдущей —
  и причина должна возникнуть **до** того, как виден любой результат на test-сегменте (провал
  cost-gate, найденная арифметическая ошибка — это ДО; неудобный Sharpe после прогона — это ПОСЛЕ
  и не даёт права на новую версию).
- Документ не должен содержать внутренних противоречий вида "no parameters tuned" рядом с
  "cadence must be validated", или "wait, let me recalc" — это признак того, что документ ещё
  черновик, не готовая к заморозке регистрация.
- Explicit `--override` флаги в командах запуска walk-forward не должны появляться "из ниоткуда" —
  каждый должен быть обоснован в самом pre-registration документе, не только в команде запуска.

## Cost/turnover sanity gate — до дорогого прогона
- Формула обязана учитывать **оба** ограничителя оборота: `round_trips/day = max_positions ×
  min(24/holding_hours, 24/rebalance_hours)` — забытый `rebalance_hours` (или забытый `min`) —
  известный, повторявшийся баг в этом проекте, завышающий издержки в разы.
- 365 дней/год для крипто (торгуется 24/7), не 252 (биржевая конвенция для акций).
- Единственный источник истины — реальный stdout скрипта с реальной командой. Никаких чисел,
  пересчитанных в prose и вписанных в markdown-таблицу без сопровождающего вывода.
- Разделяй optimistic (maker/100%-fill) и pessimistic (taker/taker) границы явно, decision rule
  должен применяться к pessimistic, если explicit fill-probability модель ещё не реализована.

## Signal-frequency pre-check — до дорогого прогона
- Должен честно симулировать операционный каденс стратегии (rebalance_hours, max_positions,
  time-stop, exit-логика), а не считать "сырые" пересечения порога независимо по каждому символу —
  тот же класс ошибки, что в cost-gate (забытые ограничители оборота), воспроизводился здесь дважды.
- Time-stop/holding period считать по разнице timestamp'ов (`(current_ms - entry_ms) / 3_600_000`),
  никогда по счётчику итераций цикла — спутать "номер шага ребалансировки" с "возраст в часах" —
  конкретный баг, который уже дважды находили в этом проекте (в pre-check скрипте и едва не
  пропустили в боевом классе стратегии).
- Regime-gating (если у стратегии есть `strategy_overrides` в конфиге) обязан учитываться в
  pre-check — иначе оценка n_trades может быть искусственно завышена относительно того, что реально
  попадёт в БД при полном пайплайне (стратегия → regime fusion → risk engine → executor).
- Период проверки — реальный test-сегмент будущего walk-forward split, не произвольный
  calendar-диапазон.

## Circuit breaker / drawdown — отчётность
- Каждая строка отчёта walk-forward маркируется явно: `production` (действующий
  `emergency_drawdown_pct` из `config/settings.yaml`) или `diagnostic_widened` (искусственно
  расширенный DD для чистого чтения эджа без остановки CB). Вердикт decision rule — только по
  `production`. Никогда не смешивать оба в одной таблице без пометки в каждой строке.

## Регрессия — перед любым "готово"
- `pytest tests/ -q` и `ruff check src/` целиком, без курированных подмножеств, перед любым
  заявлением "тесты зелёные" или "regression clean".
- Любой failure, названный "pre-existing" — подтверждён сравнением (git stash изменений → прогон
  на чистом коммите → тот же failure там же), не предположением.

## Формат закрытия (closure) документа
- Файл называется "closure"/"final" только если внутри есть реальный вердикт по decision rule
  (число, не "awaiting verdict"). Промежуточный статус — отдельный файл ("implementation_status"),
  не переиспользовать имя "closure" для незавершённой работы.
