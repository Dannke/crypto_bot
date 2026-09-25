#!/usr/bin/env python3
"""Сверка "Config Snapshot" из pre-registration с операционным config/settings.yaml.

Зачем. Снимок в документе — это заявление о том, какой конфигурацией получены
числа. Пока его не с чем было сверить механически, расхождение копилось молча:
на v4 разошлось шесть полей из двадцати одного, и каждое находили поодиночке,
случайно, целясь в другое. Один прогон этого скрипта заменяет такой поиск.

Сравнивается не текст с текстом, а заявленное значение с ФАКТИЧЕСКИМ — тем,
что видит код после load_settings(). Поле, отсутствующее в YAML, берёт дефолт
схемы, и именно дефолт может незаметно разойтись с зарегистрированным.

Проверяются оба направления:
  * заявлено в снимке, но фактически другое  -> MISMATCH;
  * есть в конфиге, но не заявлено в снимке  -> UNREGISTERED (незарегистри-
    рованный параметр — тот же класс дефекта, только наоборот).

Выход: 0 если расхождений нет, 1 если есть. Печатает построчную таблицу.

Пример:
    python scripts/check_config_snapshot.py \
        docs/research/mean_reversion_preregistration_v4.md
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from crypto_bot.config.settings import load_settings

# regime — поле верхнего уровня Settings. Снимок v4 клал его под portfolio:,
# где PortfolioConfig со extra="forbid" его отвергает; такая запись не
# является валидным патчем к конфигу и приводится к верному месту здесь.
TOP_LEVEL_MISPLACED = ("regime",)


def _extract_snapshot(doc: Path) -> dict[str, Any]:
    text = doc.read_text(encoding="utf-8")
    match = re.search(r"##\s*Config Snapshot.*?```ya?ml\n(.*?)```", text, re.S)
    if not match:
        raise SystemExit(f"{doc}: блок '## Config Snapshot' с YAML не найден")
    return yaml.safe_load(match.group(1)) or {}


def _normalise(snapshot: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Поднимает ошибочно вложенные секции на верхний уровень."""
    notes: list[str] = []
    out = dict(snapshot)
    portfolio = out.get("portfolio")
    if isinstance(portfolio, dict):
        portfolio = dict(portfolio)
        for key in TOP_LEVEL_MISPLACED:
            if key in portfolio:
                out[key] = portfolio.pop(key)
                notes.append(
                    f"снимок кладёт '{key}' под portfolio:, а это поле верхнего "
                    f"уровня Settings — сверено по верному месту"
                )
        out["portfolio"] = portfolio
    return out, notes


def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    """Разворачивает вложенный dict в плоские пути, кроме dict-значных полей."""
    flat: dict[str, Any] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        # strategy_overrides — сам по себе dict-значное поле, сравнивается целиком
        if isinstance(value, dict) and key != "strategy_overrides":
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def _effective(settings: Any, path: str) -> Any:
    node: Any = settings
    for part in path.split("."):
        # Поля-словари схемы (risk.take_profit_risk_multiple и т.п.) _flatten
        # разворачивает по ключам так же, как секции. getattr ключ словаря не
        # находит, и такое поле раньше было нельзя объявить без ложного MISMATCH.
        if isinstance(node, dict):
            if part not in node:
                return "<НЕТ ТАКОГО КЛЮЧА>"
            node = node[part]
            continue
        if not hasattr(node, part):
            return "<НЕТ ТАКОГО ПОЛЯ В СХЕМЕ>"
        node = getattr(node, part)
    return node


def _siblings_of_declared(settings: Any, declared: dict[str, Any]) -> dict[str, Any]:
    """Поля конфига, соседние с заявленными, но в снимке отсутствующие.

    Проверяются только те секции, где снимок объявляет хотя бы одно поле:
    если снимок не касается portfolio.csm вовсе, эта секция к данной
    регистрации не относится и её поля не являются незарегистрированными.
    А вот лишнее поле внутри portfolio.mean_reversion, который снимок
    описывает, — именно незарегистрированный параметр стратегии.
    """
    sections = {path.rsplit(".", 1)[0] for path in declared if "." in path}
    out: dict[str, Any] = {}
    for section in sections:
        node: Any = settings
        for part in section.split("."):
            node = getattr(node, part, None)
            if node is None:
                break
        if node is None or not hasattr(node, "model_dump"):
            continue
        for path, value in _flatten(node.model_dump(), section).items():
            if path not in declared:
                out[path] = value
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", help="pre-registration .md с блоком '## Config Snapshot'")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument(
        "--strict-unregistered", action="store_true",
        help="считать провалом и поля конфига, отсутствующие в снимке",
    )
    args = parser.parse_args()

    snapshot, notes = _normalise(_extract_snapshot(Path(args.document)))
    settings = load_settings(yaml_path=Path(args.config)).settings

    declared = _flatten(snapshot)
    width = max((len(p) for p in declared), default=20)

    print(f"снимок : {args.document}")
    print(f"конфиг : {args.config}")
    for note in notes:
        print(f"ПРИМЕЧАНИЕ: {note}")
    print()
    print(f"{'поле':<{width}}  {'заявлено':<24} {'фактически':<24} вердикт")
    print("-" * (width + 62))

    mismatches: list[str] = []
    for path, want in sorted(declared.items()):
        got = _effective(settings, path)
        same = str(want) == str(got)
        if not same:
            mismatches.append(path)
        w, g = str(want), str(got)
        print(f"{path:<{width}}  {w[:23]:<24} {g[:23]:<24} "
              f"{'совпало' if same else 'MISMATCH'}")

    unregistered = _siblings_of_declared(settings, declared)
    if unregistered:
        print()
        print("Соседние поля в описанных снимком секциях, но в снимке отсутствуют "
              "(UNREGISTERED):")
        for path in sorted(unregistered):
            print(f"  {path} = {unregistered[path]}")

    print()
    print(f"MISMATCH: {len(mismatches)} из {len(declared)}")
    print(f"UNREGISTERED: {len(unregistered)}")

    # Гейтом по умолчанию служит MISMATCH — это тот самый failure mode, который
    # на этом проекте сработал шесть раз подряд. UNREGISTERED сообщается всегда,
    # но валит прогон только по явному требованию: соседние поля бывают
    # легитимно вне регистрации, и глухой FAIL на них превратил бы проверку в
    # шум, который научатся игнорировать.
    failed = bool(mismatches) or (bool(unregistered) and args.strict_unregistered)
    print("\nВЕРДИКТ:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
