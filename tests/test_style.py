"""Проверки самого `style.css` — мёртвые правила (SPEC §10.3).

Ловит расхождение, которое на экране видно, а в тестах не ловилось ничем:
правило написано, выглядит рабочим и не действует, потому что его перебивает
базовое правило той же таблицы.

Так было в 0.3.0 сразу в семи местах. `.results-table td` — это класс плюс тег,
специфичность (0,1,1); `.cell-rank`, `.cell-spark`, `.cell-delta` — один класс,
(0,1,0). Свойство, заданное в обоих, побеждает у базового правила независимо
от порядка в файле: колонка «#» и Δ прижимались к краю вместо своих 10 px,
а широкие ячейки не переносили содержимое, хотя `white-space: normal` в файле
стоял. То же самое в сетке цен: `.price-grid th` съедал цвет и выключку
у заголовков `sell`, `buy` и групп — они рисовались серыми вместо цветных.

Правило простое: если свойство задано базовым правилом, модификатор обязан
писаться через него — `.results-table td.cell-rank`, а не `.cell-rank`.
Браузера для этой проверки не нужно, всё видно в файле.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STYLE = Path(__file__).resolve().parent.parent / "app" / "static" / "style.css"

# Базовое правило и приставка классов, которые попадают на те же элементы.
# Приставка — соглашение об именовании: класс ячейки таблицы результата
# начинается с `cell-`, класс сетки цен — с `price-grid__`.
BASES = [
    (".results-table td", r"\.cell-[\w-]+"),
    (".price-grid th", r"\.price-grid__[\w-]+"),
    (".price-grid td", r"\.price-grid__[\w-]+"),
]


@pytest.fixture(scope="module")
def rules() -> list[tuple[str, set[str]]]:
    """Разбирает файл в список (селектор, множество свойств).

    Разбор нарочно грубый: медиазапросы и @font-face внутрь не разворачиваются,
    комментарии выкидываются. Для проверки специфичности этого достаточно.
    """
    css = STYLE.read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    parsed = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        selector = " ".join(selector.split())
        if selector.startswith("@"):
            continue
        props = {
            declaration.split(":", 1)[0].strip()
            for declaration in body.split(";")
            if ":" in declaration
        }
        for one in selector.split(","):
            parsed.append((one.strip(), props))
    return parsed


def _properties_of(rules: list[tuple[str, set[str]]], selector: str) -> set[str]:
    props: set[str] = set()
    for one, declared in rules:
        if one == selector:
            props |= declared
    return props


@pytest.mark.parametrize("base, modifier_pattern", BASES)
def test_modifiers_are_scoped_through_the_base_rule(rules, base, modifier_pattern):
    """Модификатор не пытается перебить базовое правило одним классом."""
    base_props = _properties_of(rules, base)
    assert base_props, f"базовое правило {base} исчезло — проверку надо чинить"

    dead = []
    for selector, props in rules:
        if not re.fullmatch(modifier_pattern, selector):
            continue  # составной селектор — специфичности хватает
        clash = props & base_props
        if clash:
            dead.append(f"{selector} задаёт {sorted(clash)}, но их перебьёт {base}")
    assert not dead, "мёртвые правила:\n" + "\n".join(dead)
