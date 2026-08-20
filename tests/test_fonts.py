"""Проверки шрифтов интерфейса (SPEC §10.4).

Ловят два расхождения, которые на экране видно, а в тестах до сих пор не было:

* ссылка `@font-face` ведёт на файл, которого нет в репозитории, — страница
  молча падает на системный шрифт;
* у семейства нет кириллицы. Ровно это случилось в 0.3.0 с `Archivo`: латиница
  набиралась макетным шрифтом, а весь русский текст интерфейса — системным.
  Интерфейс русский, поэтому кириллица у текстового семейства обязательна.

Проверяется сам `style.css`, без браузера: и то и другое видно в разметке.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
STYLE = STATIC / "style.css"

# Начало диапазона основной кириллицы. Достаточно одной точки: подмножества
# Google Fonts всегда включают её целиком, а нам важен факт наличия алфавита.
CYRILLIC_PROBE = 0x0410  # «А»

WOFF2_MAGIC = b"wOF2"


@pytest.fixture(scope="module")
def css() -> str:
    return STYLE.read_text(encoding="utf-8")


def _font_faces(css: str) -> list[dict[str, str]]:
    """Разбирает блоки @font-face в список словарей со свойствами."""
    faces = []
    for block in re.findall(r"@font-face\s*\{(.*?)\}", css, re.S):
        props = dict(
            (m.group(1).strip(), m.group(2).strip())
            for m in re.finditer(r"([a-z-]+)\s*:\s*([^;]+);", block)
        )
        faces.append(props)
    return faces


def _family(value: str) -> str:
    """Первое семейство из стека, без кавычек."""
    return value.split(",")[0].strip().strip('"\'')


def _covers(unicode_range: str, code_point: int) -> bool:
    """Попадает ли символ в unicode-range из @font-face."""
    for part in unicode_range.split(","):
        part = part.strip()
        if "-" in part:
            low, high = part.split("-", 1)
            if int(low.lstrip("Uu+"), 16) <= code_point <= int(high, 16):
                return True
        elif int(part.lstrip("Uu+"), 16) == code_point:
            return True
    return False


def test_font_faces_found(css: str) -> None:
    """Если разбор сломался, остальные проверки станут пустыми и зелёными."""
    assert len(_font_faces(css)) >= 2


def test_every_font_file_exists_and_is_woff2(css: str) -> None:
    """Каждый src ведёт на настоящий woff2, лежащий в репозитории."""
    urls = re.findall(r'url\(\s*"([^"]+)"\s*\)', css)
    assert urls, "в style.css нет ни одной ссылки на файл шрифта"

    for url in urls:
        path = STATIC / url
        assert path.is_file(), f"{url} — файла нет в app/static/"
        head = path.read_bytes()[:4]
        assert head == WOFF2_MAGIC, f"{url} — не woff2 (сигнатура {head!r})"


def test_no_external_font_requests(css: str) -> None:
    """§10.1: страница обязана открываться без интернета."""
    assert "http://" not in css and "https://" not in css, (
        "в style.css есть ссылка на сторонний хост — "
        "шрифты раздаются только со своего"
    )


@pytest.mark.parametrize("token", ["--font-text", "--font-num"])
def test_family_has_cyrillic(css: str, token: str) -> None:
    """У обоих семейств есть подмножество с кириллицей.

    Интерфейс русский: без кириллицы браузер молча берёт системный шрифт,
    и оформление применяется к меньшей части экрана.
    """
    declared = re.search(rf"{token}\s*:\s*([^;]+);", css)
    assert declared, f"токен {token} не найден в style.css"
    family = _family(declared.group(1))

    ranges = [
        face["unicode-range"]
        for face in _font_faces(css)
        if _family(face.get("font-family", "")) == family and "unicode-range" in face
    ]
    assert ranges, f"у семейства {family} нет ни одного @font-face с unicode-range"

    assert any(_covers(r, CYRILLIC_PROBE) for r in ranges), (
        f"у семейства {family} нет кириллицы: русский текст интерфейса "
        f"будет набран системным шрифтом"
    )


@pytest.mark.parametrize("token", ["--font-text", "--font-num"])
def test_family_has_fallback(css: str, token: str) -> None:
    """Запасное семейство обязательно: шрифт может не загрузиться."""
    declared = re.search(rf"{token}\s*:\s*([^;]+);", css)
    assert declared
    assert len(declared.group(1).split(",")) >= 2, (
        f"у {token} нет запасного семейства"
    )
