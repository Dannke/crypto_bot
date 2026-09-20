---
name: db-migration
description: Add a new sequential SQLite schema migration (new column, new table, or a new value in an existing CHECK-constrained enum column) following this project's migration convention. User-invoked only — this changes the DB schema, not something Claude should trigger on its own.
disable-model-invocation: true
---

# db-migration

Добавляет новую последовательную миграцию схемы для `src/crypto_bot/storage/` — по конвенции,
уже используемой в проекте (`schema_meta.schema_version`, `_migrate_vN` в `db.py`,
`migrations.sql`). См. `CLAUDE.md`: "новое значение enum-подобного поля требует новой миграции,
не только Python-логики".

## Шаг 1 — определить следующий номер версии

```bash
grep -oE "schema_version', '[0-9]+" src/crypto_bot/storage/migrations.sql | tail -1
grep -oE "_migrate_v[0-9]+" src/crypto_bot/storage/db.py | sort -Vu | tail -1
```

Новый номер = `max(эти два) + 1`. Если они расходятся — **стоп**, сначала почини несовпадение
(вероятно, кто-то забыл вызвать `_migrate_vN()` из `_migrate()` — частый источник тихого бага:
функция миграции определена, но не вызывается, и `CHECK`-констрейнт на существующих БД не
применяется, хотя на свежей БД `migrations.sql` уже содержит новое значение).

## Шаг 2 — три места, которые нужно обновить синхронно

1. **`src/crypto_bot/storage/migrations.sql`** — базовое определение таблицы (для новых БД с нуля)
   должно уже включать новое значение/колонку в `CHECK`, плюс комментарий-блок в конце файла с
   `-- vN: <что и зачем>` и `INSERT INTO schema_meta ... schema_version', 'N'`.

2. **`src/crypto_bot/storage/db.py`** — добавить метод `_migrate_vN(self)`:
   - guard: `if existing and int(existing["value"]) >= N: return`
   - для нового значения в `CHECK` — SQLite не умеет `ALTER TABLE ... CHECK`, нужен паттерн
     "создать `<table>_new` с новым CHECK → скопировать данные → `DROP` старую → `RENAME`"
     (см. `_migrate_v10` как образец).
   - для новой колонки без CHECK-изменения — достаточно `ALTER TABLE ... ADD COLUMN`.
   - **обязательно** добавить вызов `self._migrate_vN()` в `_migrate()` — самый частый способ
     сломать это: функция определена, но не вызвана (актуальный пример такого бага сейчас есть
     в рабочей копии — `_migrate_v10` не вызывается из `_migrate()`, проверь `grep -n
     "_migrate_v10()" src/crypto_bot/storage/db.py`, если пусто — баг).

3. **`src/crypto_bot/core/types.py`** (или где объявлен `Literal`/enum для этого поля) — Python-сторона
   должна совпадать с новым набором значений в `CHECK`, не только наоборот.

## Шаг 3 — тест на upgrade path, не только на создание с нуля

Добавь/расширь тест в `tests/test_storage.py`: создать БД на версии N-1 (старый `CHECK`), запустить
`_migrate()`, убедиться что теперь можно вставить новое значение и что `schema_version` стал `N`.
Тест "создать БД с нуля и вставить новое значение" **не проверяет** путь апгрейда существующей
базы — это ровно то место, где `_migrate_v10`-баг остался незамеченным.

## Шаг 4 — верификация

```bash
python -m pytest tests/test_storage.py -q
ruff check src/crypto_bot/storage/
```

Затем передай диффу на ревью subagent'у `migration-reviewer` перед коммитом — он сверяет
последовательность версий и синхронность `migrations.sql` / `db.py` / `core/types.py`
механически, а не на глаз.
