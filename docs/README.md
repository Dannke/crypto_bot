# Документация crypto_bot

Точка входа. Всё, что здесь не упомянуто, — не документация проекта.

## С чего начать

1. Этот файл — состояние и карта.
2. [`architecture/overview.md`](architecture/overview.md) — как устроена система.
3. [`architecture/backlog.md`](architecture/backlog.md) — все известные открытые пункты.
4. Методология исследований — `.claude/skills/research-methodology/SKILL.md` (Claude подгружает
   её сам, когда задача исследовательская).

Для задачи по конкретной стратегии достаточно её папки в `research/`.

## Текущее состояние — 2026-09-26

| стратегия | вердикт | документ |
|---|---|---|
| CSM, cross-sectional momentum | **REJECTED** — Test Sharpe −0.63 на 874 сделках | [`research/csm/closure.md`](research/csm/closure.md) |
| Mean reversion, цикл 1 (регистрации v1–v4) | **INVALIDATED** — дефект статистики сигнала, гипотеза не тестировалась | [`research/mean-reversion/cycle-1/closure.md`](research/mean-reversion/cycle-1/closure.md) |
| Mean reversion, цикл 2 | **REJECTED** — Sharpe(test) −0.1858, Sharpe(validation) −0.2337 | [`research/mean-reversion/cycle-2/4-closure.md`](research/mean-reversion/cycle-2/4-closure.md) |

Test-сегмент 2025-11-24..2026-09-17 для mean reversion израсходован. Следующее направление не
выбрано; варианты — в [`research/mean-reversion/plan.md`](research/mean-reversion/plan.md),
раздел 9: funding/basis (приоритет №4) или regime-aware (№1).

## Структура

```
docs/
├── README.md                          ← вы здесь
├── architecture/
│   ├── overview.md                    ← устройство системы
│   └── backlog.md                     ← открытые пункты
└── research/
    ├── csm/
    │   └── closure.md
    └── mean-reversion/
        ├── plan.md                    ← план шага MR: задачи, шаблон decision rule, риски
        ├── cycle-1/
        │   └── closure.md             ← итог линии v1–v4
        └── cycle-2/
            ├── 1-signal-definition.md ← решение до измерений, поправки 1–2
            ├── 2-signal-validation.md ← гейты G1–G3, частота сигнала
            ├── 3-preregistration.md   ← замороженная регистрация
            └── 4-closure.md           ← вердикт
```

## Соглашения

- **Язык** — русский; код, идентификаторы и docstrings — английский (`CLAUDE.md`).
- **Имена** — латиница, строчные, через дефис. Контекст задаёт папка (стратегия, цикл), имя —
  роль документа. Даты в имена не пишутся — они в шапке.
- **Шапка** каждого документа — дата и статус.
- **Исследование стратегии** — `research/<стратегия>/`, каждый цикл — `cycle-<N>/`, файлы по
  порядку шагов: `1-` решение до измерений, `2-` измерение, `3-preregistration.md`,
  `4-closure.md`. Каждый шаг — отдельный коммит: порядок «решение до измерения» проверяется по git.
- **Числа** — вывод команды из того же документа или ссылка на датированный документ, где он
  так оформлен (правило provenance в skill).
- **Закрытые документы не переписываются.** Исправление — датированная поправка поверх. При
  переносе файла в них обновляются только полные пути `docs/...`; упоминания коротким именем
  сверяются с таблицей ниже.
- **Отработавшие документы удаляются**, а не копятся: заменённые версии регистраций,
  промежуточные статусы, handoff'ы между сессиями. Долговечное из них переносится в регистрацию,
  итог цикла или бэклог. История остаётся в git.
- **Ссылки проверяются тестом** `tests/test_docs_links.py`: полные пути `docs/...` во всём
  репозитории и относительные markdown-ссылки внутри `docs/`.

## Перемещённые и удалённые документы — 2026-09-26

Любой удалённый файл доступен в истории: `git show 6e88fe8:docs/<путь из таблицы>`
(`6e88fe8` — `main` до реструктуризации). Пути ниже — относительно `docs/`.

| было | стало | почему |
|---|---|---|
| `architecture/audit.md` | `architecture/overview.md` + `architecture/backlog.md` | устройство и открытые пункты разделены; устаревшее удалено: копия конфига CSM, статус тестов на 2026-08-25, дубль вердикта CSM |
| `architecture/CSM_closure_final_summary.md` | `research/csm/closure.md` | это исследование, а не архитектура |
| `architecture/R0_R8_implementation_summary.md` | удалён | отчёт о завершённой фазе; суть — в `overview.md`, раздел 4 |
| `research/mean_reversion_plan.md` | `research/mean-reversion/plan.md` | |
| `research/MR_closure_final_summary.md` | `research/mean-reversion/cycle-1/closure.md` | |
| `research/mean_reversion_preregistration.md`, `_v2`, `_v3`, `_v4` | удалены | регистрации закрытой линии v1–v4; итог — `cycle-1/closure.md` |
| `research/MR_implementation_status.md` | удалён | промежуточный статус времён v3 |
| `research/handoff_mr_v1v4_to_task0.md` | удалён | handoff, принятый циклом 2; его входы цитируются в `cycle-2/1-signal-definition.md` |
| `research/session_handoff_2026-09-21.md` | удалён | handoff, принятый сессией 2026-09-21 |
| `research/mr_cycle2_signal_definition.md` | `research/mean-reversion/cycle-2/1-signal-definition.md` | |
| `research/mr_cycle2_signal_validation.md` | `research/mean-reversion/cycle-2/2-signal-validation.md` | |
| `research/mr_cycle2_preregistration.md` | `research/mean-reversion/cycle-2/3-preregistration.md` | |
| `research/MR_cycle2_closure_final_summary.md` | `research/mean-reversion/cycle-2/4-closure.md` | |

Корневой `audit.md` удалён: он был устаревшей копией `architecture/audit.md`.

Упоминаются в старых документах, но в репозитории не было никогда: `tasks_summary.md`,
`handoff_csm_to_mean_reversion.md`, `method-and-working-style.md`, `plan_stage2.md`.
