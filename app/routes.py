"""HTTP-эндпоинты, разбор формы расчёта и подтяжка цен из ESI."""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from flask import Blueprint, current_app, render_template, request

from app.core import calculator, catalog
from app.core.constants import GDE_MAX_LEVEL, GDE_MIN_LEVEL
from app.core.models import (
    CalcInput,
    CollateralMode,
    Gas,
    GasForm,
    Hub,
    HubDepth,
    HubPrices,
    OrderSide,
    StructureType,
    WarningCode,
)
from app.formatting import fmt_number
from app.services import esi, orderbook

bp = Blueprint("main", __name__)

# --- Тексты интерфейса (не игровые константы) ---

FAMILY_LABELS = {
    "fullerite": "Фуллерены",
    "mykoserocin": "Mykoserocin",
    "cytoserocin": "Cytoserocin",
}
STRUCTURE_LABELS = {
    StructureType.UPWELL: "Upwell (без бонуса)",
    StructureType.ATHANOR: "Athanor",
    StructureType.TATARA: "Tatara",
}
FORM_LABELS = {
    GasForm.RAW: "сырой",
    GasForm.COMPRESSED: "сжатый",
}
SIDE_LABELS = {
    OrderSide.SELL: "sell",
    OrderSide.BUY: "buy",
}
# Ценовые колонки сетки: (суффикс имени поля, подпись в интерфейсе и ошибках)
PRICE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("raw_sell", "Сырой sell"),
    ("raw_buy", "Сырой buy"),
    ("compressed_sell", "Сжатый sell"),
    ("compressed_buy", "Сжатый buy"),
)

# Значения по умолчанию — SPEC §3
DEFAULTS = {
    "gas": "fullerite_c320",
    "n_units": 10_000,
    "structure": StructureType.ATHANOR.value,
    "gde_level": 5,
    "broker_pct": 1.5,
}

# Числа из формы: пробелы-разрядники (обычный, NBSP, узкий NBSP) и запятая допустимы
_NUM_CLEAN = str.maketrans({" ": None, "\u00a0": None, "\u202f": None, ",": "."})


def _to_float(raw: str) -> float:
    """Разбирает число из строки формы. Непонятный ввод — ValueError."""
    value = float(raw.translate(_NUM_CLEAN))
    if not math.isfinite(value):
        raise ValueError(raw)
    return value


def _parse_gas(form: Mapping[str, str], errors: list[str]) -> Gas | None:
    """Газ по ключу из формы."""
    gas_key = form.get("gas", "").strip()
    if not gas_key:
        errors.append("Не выбран газ.")
        return None
    try:
        return catalog.gas_by_key(gas_key)
    except KeyError:
        errors.append(f"Неизвестный газ: {gas_key!r}.")
        return None


def _parse_n_units(form: Mapping[str, str], errors: list[str]) -> int:
    """«Нужно юнитов сырого»: целое больше нуля."""
    raw = form.get("n_units", "").strip()
    if not raw:
        errors.append("«Нужно юнитов сырого»: поле пустое.")
        return 0
    try:
        value = _to_float(raw)
        if value != int(value):
            raise ValueError(raw)
        n_units = int(value)
    except ValueError:
        errors.append(f"«Нужно юнитов сырого»: не похоже на целое число: {raw!r}.")
        return 0
    if n_units <= 0:
        errors.append("«Нужно юнитов сырого»: число должно быть больше нуля.")
        return 0
    return n_units


def _parse_structure(form: Mapping[str, str], errors: list[str]) -> StructureType | None:
    """Тип структуры разжатия."""
    raw = form.get("structure", "").strip()
    try:
        return StructureType(raw)
    except ValueError:
        errors.append(f"Неизвестный тип структуры: {raw!r}.")
        return None


def _parse_gde_level(form: Mapping[str, str], errors: list[str]) -> int:
    """Уровень навыка Gas Decompression Efficiency, 0..5."""
    raw = form.get("gde_level", "").strip()
    try:
        level = int(raw)
        if not GDE_MIN_LEVEL <= level <= GDE_MAX_LEVEL:
            raise ValueError(raw)
        return level
    except ValueError:
        errors.append(
            f"«Навык GDE»: целое число от {GDE_MIN_LEVEL} до {GDE_MAX_LEVEL}, "
            f"получено: {raw!r}."
        )
        return 0


def _eta_percent_map() -> dict[str, dict[int, int]]:
    """Проценты разжатия для всех комбинаций структура × навык.

    Считается тем же ядром, что и результат, — у живой подписи в интерфейсе
    нет собственной математики.
    """
    return {
        structure.value: {
            level: round(calculator.decompression_efficiency(structure, level) * 100)
            for level in range(GDE_MIN_LEVEL, GDE_MAX_LEVEL + 1)
        }
        for structure in StructureType
    }


def _volume_label(gas) -> str:
    """Подпись объёмов под селектом газа: «5 м³ сырой / 0.5 м³ сжатый»."""
    return (
        f"{fmt_number(gas.volume_raw)} м³ сырой / "
        f"{fmt_number(gas.volume_compressed)} м³ сжатый"
    )


@dataclass(frozen=True, slots=True)
class PriceCell:
    """Одна ценовая ячейка сетки.

    fetched — цена пришла из ESI и показывается приглушённо; при ручной правке
    JS снимает пометку и стирает depth, потому что глубина относится к цене,
    которой больше нет.
    """

    value: str = ""
    fetched: bool = False
    depth: int | None = None


@dataclass(frozen=True, slots=True)
class HubRow:
    """Строка сетки цен: хаб, ставка доставки и четыре ячейки."""

    hub: Hub
    rate: str = ""
    cells: dict[str, PriceCell] = field(default_factory=dict)


def _empty_grid() -> list[HubRow]:
    """Пустая сетка для первой отрисовки страницы."""
    return [
        HubRow(hub=hub, cells={suffix: PriceCell() for suffix, _ in PRICE_COLUMNS})
        for hub in catalog.hubs()
    ]


def _grid_from_form(form: Mapping[str, str]) -> list[HubRow]:
    """Сетка из того, что уже введено в форме.

    Подтяжка перерисовывает сетку целиком, поэтому введённое руками надо
    вернуть на место: ставки доставки ESI не знает вовсе, а цены, по которым
    данные не пришли, обязаны остаться как были.
    """
    return [
        HubRow(
            hub=hub,
            rate=form.get(f"{hub.key}_rate", "").strip(),
            cells={
                suffix: PriceCell(value=form.get(f"{hub.key}_{suffix}", "").strip())
                for suffix, _ in PRICE_COLUMNS
            },
        )
        for hub in catalog.hubs()
    ]


@bp.get("/")
def index() -> str:
    """Страница с формой: параметры расчёта и сетка цен."""
    gases_by_family: dict[str, list] = {}
    for gas in catalog.gases():
        gases_by_family.setdefault(gas.family, []).append(gas)

    return render_template(
        "index.html",
        gases_by_family=gases_by_family,
        family_labels=FAMILY_LABELS,
        structure_labels=STRUCTURE_LABELS,
        gde_levels=range(GDE_MIN_LEVEL, GDE_MAX_LEVEL + 1),
        hubs=catalog.hubs(),
        price_columns=PRICE_COLUMNS,
        grid=_empty_grid(),
        fetch_notes=None,
        defaults=DEFAULTS,
        volume_labels={gas.key: _volume_label(gas) for gas in catalog.gases()},
        eta_map_json=json.dumps(_eta_percent_map()),
        initial_eta_pct=_eta_percent_map()[DEFAULTS["structure"]][DEFAULTS["gde_level"]],
    )


def _parse_form(form) -> tuple[CalcInput | None, dict[str, HubPrices], list[str]]:
    """Разбирает форму. Возвращает (вход, цены, ошибки); ошибки копятся все сразу."""
    errors: list[str] = []

    gas = _parse_gas(form, errors)
    n_units = _parse_n_units(form, errors)
    structure = _parse_structure(form, errors)
    gde_level = _parse_gde_level(form, errors)

    broker_fee = 0.0
    raw = form.get("broker_fee", "").strip()
    if not raw:
        errors.append("«Брокерская комиссия»: поле пустое.")
    else:
        try:
            pct = _to_float(raw)
            if not 0 <= pct <= 5:
                errors.append("«Брокерская комиссия»: число от 0 до 5 (в процентах).")
            else:
                broker_fee = pct / 100  # в форме проценты, в ядре — доля
        except ValueError:
            errors.append(f"«Брокерская комиссия»: не похоже на число: {raw!r}.")

    collateral_mode = CollateralMode.MANUAL
    raw = form.get("collateral_mode", CollateralMode.MANUAL.value).strip()
    try:
        collateral_mode = CollateralMode(raw)
    except ValueError:
        errors.append(f"Неизвестный режим обеспечения: {raw!r}.")

    collateral_manual = 0.0
    raw = form.get("collateral_manual", "").strip()
    if raw:
        try:
            collateral_manual = _to_float(raw)
            if collateral_manual < 0:
                errors.append("«Сумма обеспечения»: не может быть отрицательной.")
        except ValueError:
            errors.append(f"«Сумма обеспечения»: не похоже на число: {raw!r}.")

    include_collateral_fee = form.get("include_collateral") is not None
    sell_only = form.get("sell_only") is not None

    prices: dict[str, HubPrices] = {}
    for hub in catalog.hubs():
        rate = None
        raw = form.get(f"{hub.key}_rate", "").strip()
        if raw:
            try:
                rate = _to_float(raw)
                if rate < 0:
                    errors.append(f"{hub.name}, «Доставка»: ставка не может быть отрицательной.")
                    rate = None
            except ValueError:
                errors.append(f"{hub.name}, «Доставка»: не похоже на число: {raw!r}.")

        cells: dict[str, float] = {}
        depths: dict[str, int] = {}
        for suffix, label in PRICE_COLUMNS:
            raw = form.get(f"{hub.key}_{suffix}", "").strip()
            if not raw:
                continue
            try:
                price = _to_float(raw)
                if price <= 0:
                    errors.append(f"{hub.name}, «{label}»: цена должна быть больше нуля.")
                else:
                    cells[suffix] = price
            except ValueError:
                errors.append(f"{hub.name}, «{label}»: не похоже на число: {raw!r}.")
                continue

            # Глубина известна только для подтянутых цен и приезжает скрытым полем.
            # Мусор в нём — не повод ронять расчёт: просто считаем глубину неизвестной.
            depth_raw = form.get(f"{hub.key}_{suffix}_depth", "").strip()
            if depth_raw:
                try:
                    depth = int(depth_raw)
                except ValueError:
                    continue
                if depth >= 0:
                    depths[suffix] = depth

        prices[hub.key] = HubPrices(
            freight_rate=rate,
            depth=HubDepth(**depths) if depths else None,
            **cells,
        )

    if errors:
        return None, {}, errors

    assert gas is not None and structure is not None  # errors пуст — всё разобрано
    inp = CalcInput(
        gas=gas,
        n_units=n_units,
        structure=structure,
        gde_level=gde_level,
        broker_fee=broker_fee,
        collateral_mode=collateral_mode,
        collateral_manual=collateral_manual,
        include_collateral_fee=include_collateral_fee,
        sell_only=sell_only,
    )
    return inp, prices, errors


@bp.post("/calculate")
def calculate() -> str:
    """Принимает форму, возвращает HTML-фрагмент блока результата (HTMX)."""
    inp, prices, errors = _parse_form(request.form)
    if errors:
        return render_template("partials/results.html", errors=errors)

    try:
        result = calculator.build_scenarios(inp, prices)
    except ValueError as exc:
        # Подстраховка: ядро валидирует жёстче формы. Ошибку показываем, не глотаем.
        return render_template("partials/results.html", errors=[str(exc)])

    return render_template(
        "partials/results.html",
        errors=None,
        result=result,
        inp=inp,
        hub_names={hub.key: hub.name for hub in catalog.hubs()},
        form_labels=FORM_LABELS,
        has_outliers=any(
            WarningCode.PRICE_OUTLIER in scenario.warnings
            for scenario in result.scenarios
        ),
        has_shallow=any(
            WarningCode.SHALLOW_BOOK in scenario.warnings
            for scenario in result.scenarios
        ),
        warning_codes=WarningCode,
    )


@bp.get("/api/gases")
def api_gases() -> dict:
    """JSON-справочник газов для клиентского JS (SPEC §8).

    Отдаёт ровно то, что знает сервер, включая незаполненные raw_type_id:
    null здесь — это честное «неизвестно», а не повод что-то подставить.
    """
    return {
        "gases": [
            {
                "key": gas.key,
                "name": gas.name,
                "family": gas.family,
                "family_label": FAMILY_LABELS.get(gas.family, gas.family),
                "volume_raw": gas.volume_raw,
                "volume_compressed": gas.volume_compressed,
                "raw_type_id": gas.raw_type_id,
                "compressed_type_id": gas.compressed_type_id,
            }
            for gas in catalog.gases()
        ],
        "hubs": [
            {
                "key": hub.key,
                "name": hub.name,
                "region_id": hub.region_id,
                "station_id": hub.station_id,
                "system_id": hub.system_id,
            }
            for hub in catalog.hubs()
        ],
    }


# --- Подтяжка цен из ESI (SPEC §4, docs/ESI.md) ---


def _render_grid(grid: list[HubRow], notes: list[str] | None) -> str:
    """Отдаёт фрагмент сетки цен — ответ на POST /fetch-prices."""
    return render_template(
        "partials/prices.html",
        grid=grid,
        price_columns=PRICE_COLUMNS,
        fetch_notes=notes or None,
    )


def _price_cache() -> esi.TTLCache:
    """Кэш ответов ESI, общий на процесс. Заводится в create_app."""
    return current_app.extensions["gascalc_esi_cache"]


@bp.post("/fetch-prices")
async def fetch_prices() -> str:
    """Тянет цены из ESI и возвращает перерисованную сетку (HTMX).

    Async здесь ради одного: до десяти независимых запросов уходят разом.
    Подтяжка — вспомогательная функция: что бы ни случилось, форма возвращается
    целой, с сохранённым ручным вводом и внятным объяснением под сеткой.
    """
    form = request.form
    grid = _grid_from_form(form)

    errors: list[str] = []
    gas = _parse_gas(form, errors)
    n_units = _parse_n_units(form, errors)
    structure = _parse_structure(form, errors)
    gde_level = _parse_gde_level(form, errors)
    if errors or gas is None or structure is None:
        return _render_grid(grid, [f"Цены не подтянуты. {e}" for e in errors])

    try:
        settings = esi.EsiSettings.from_config(current_app.config)
    except ValueError as exc:
        return _render_grid(grid, [f"Цены не подтянуты: {exc}."])

    eta = calculator.decompression_efficiency(structure, gde_level)
    needed = {
        GasForm.RAW: n_units,
        GasForm.COMPRESSED: calculator.required_compressed_qty(n_units, eta),
    }

    notes: list[str] = []
    known_types: dict[GasForm, int] = {}
    for gas_form, type_id in (
        (GasForm.RAW, gas.raw_type_id),
        (GasForm.COMPRESSED, gas.compressed_type_id),
    ):
        if type_id is None:
            # Выдумывать ID запрещено, поэтому колонки просто не заполняются —
            # и об этом надо сказать прямо, а не оставить пустоту без объяснения.
            notes.append(
                f"{gas.name}, {FORM_LABELS[gas_form]}: type_id не заполнен "
                f"в data/gases.json — эти две колонки подтянуть неоткуда, "
                f"заполните вручную."
            )
        else:
            known_types[gas_form] = type_id

    if not known_types:
        return _render_grid(grid, notes)

    pairs = [
        (hub.region_id, type_id)
        for hub in catalog.hubs()
        for type_id in known_types.values()
    ]
    results = await esi.fetch_many(pairs, settings, cache=_price_cache())

    empty_books: list[str] = []
    filled = 0
    rows: list[HubRow] = []
    for row in grid:
        cells = dict(row.cells)
        for gas_form, type_id in known_types.items():
            answer = results[(row.hub.region_id, type_id)]
            if not answer.ok:
                notes.append(
                    f"{row.hub.name}, {FORM_LABELS[gas_form]}: данные не получены — "
                    f"{answer.error}. Введённое вручную сохранено."
                )
                continue
            for side in OrderSide:
                assert answer.orders is not None  # проверено выше через answer.ok
                book = orderbook.quote(answer.orders, row.hub, side, needed[gas_form])
                suffix = f"{gas_form.value}_{side.value}"
                if book.price is None:
                    empty_books.append(
                        f"{row.hub.name} ({FORM_LABELS[gas_form]} {SIDE_LABELS[side]})"
                    )
                    continue
                cells[suffix] = PriceCell(
                    value=fmt_number(round(book.price, 2)),
                    fetched=True,
                    depth=book.available,
                )
                filled += 1
        rows.append(HubRow(hub=row.hub, rate=row.rate, cells=cells))

    if empty_books:
        notes.append("Подходящих ордеров в стакане нет: " + ", ".join(empty_books) + ".")
    if filled:
        notes.insert(
            0,
            f"Подтянуто цен: {filled}. Это средневзвешенная цена на нужный объём "
            f"({fmt_number(needed[GasForm.RAW])} юнитов сырого или "
            f"{fmt_number(needed[GasForm.COMPRESSED])} сжатого), а не лучшая цена в стакане.",
        )

    return _render_grid(rows, notes)
