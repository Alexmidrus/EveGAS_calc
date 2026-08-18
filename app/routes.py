"""HTTP-эндпоинты, разбор формы расчёта и чтение собранных цен.

К ESI отсюда не ходят: цены приезжают из базы, куда их складывает сборщик
по расписанию (app/jobs/collect.py). Пользователь не может инициировать
обращение к внешнему API ни одним действием — это жёсткое правило проекта.
"""

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from flask import Blueprint, current_app, render_template, request, session

from app.core import calculator, catalog
from app.core.constants import GDE_MAX_LEVEL, GDE_MIN_LEVEL
from app.core.models import (
    COLLATERAL_PCT_DEFAULT,
    COLLATERAL_PCT_MAX,
    CalcInput,
    Gas,
    GasForm,
    Hub,
    HubDepth,
    HubPrices,
    OrderSide,
    StructureType,
    WarningCode,
)
from app.auth.views import current_character, settings_or_none
from app.formatting import fmt_number
from app.db import utcnow
from app.services import prices, user_settings

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
    # В форме проценты, в ядре доля — отсюда умножение на 100
    "collateral_pct": COLLATERAL_PCT_DEFAULT * 100,
    "collateral_pct_max": COLLATERAL_PCT_MAX * 100,
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

    auto — значение подставлено из базы и показывается приглушённо. При ручной
    правке JS снимает пометку и стирает depth: глубина относилась к той цене,
    которой больше нет. Пометка ездит скрытым полем, чтобы сервер знал, какие
    ячейки можно пересчитать под новый объём, а какие трогать нельзя.
    """

    value: str = ""
    auto: bool = False
    depth: int | None = None


@dataclass(frozen=True, slots=True)
class HubRow:
    """Строка сетки цен: хаб, ставка доставки и четыре ячейки."""

    hub: Hub
    rate: str = ""
    cells: dict[str, PriceCell] = field(default_factory=dict)


def _needed_by_form(gas: Gas, n_units: int, structure: StructureType, gde_level: int) -> dict[GasForm, int]:
    """Сколько юнитов каждой формы нужно купить. Под этот объём и считается цена."""
    eta = calculator.decompression_efficiency(structure, gde_level)
    return {
        GasForm.RAW: n_units,
        GasForm.COMPRESSED: calculator.required_compressed_qty(n_units, eta),
    }


def _known_type_ids(gas: Gas) -> dict[GasForm, int]:
    """ID форм газа, которые известны. Неизвестный ID не выдумывается."""
    pairs = ((GasForm.RAW, gas.raw_type_id), (GasForm.COMPRESSED, gas.compressed_type_id))
    return {form: int(type_id) for form, type_id in pairs if type_id is not None}


def _load_book(gas: Gas, needed: Mapping[GasForm, int]) -> prices.PriceBook:
    """Цены из базы по выбранному газу под конкретный объём."""
    return prices.load_price_book(
        current_app.extensions["db_engine"],
        catalog.hubs(),
        _known_type_ids(gas),
        needed,
    )


def _history_row_key(hub_key: str, form: GasForm, side: OrderSide) -> str:
    """Ключ строки таблицы результата. Строкой — по ней ищет шаблон."""
    return f"{hub_key}|{form.value}|{side.value}"


def _history_rows(gas: Gas, scenarios: Sequence[object]) -> dict[str, dict[str, object]]:
    """Числа и пометки по истории для каждой строки результата (ESI §5.5).

    Пометки ничего не пересчитывают: ручной ввод остаётся мастером, решение
    принимает человек. Наше дело — чтобы он видел, торгуют по этой цене или нет
    и получится ли по ней набрать нужный объём.
    """
    stats_by_pair = prices.load_history_stats(
        current_app.extensions["db_engine"], catalog.hubs(), _known_type_ids(gas)
    )
    rows: dict[str, dict[str, object]] = {}
    for scenario in scenarios:
        stats = stats_by_pair.get((scenario.hub_key, scenario.form))
        if stats is None or not stats.usable:
            continue
        rows[_history_row_key(scenario.hub_key, scenario.form, scenario.side)] = {
            "reference": stats.reference,
            "lowest": stats.lowest,
            "highest": stats.highest,
            "daily_volume": stats.daily_volume,
            "window_volume": stats.volume,
            "window_days": stats.window_days,
            "last_day": stats.last_day,
            # Потребность именно этой строки против оборота именно этой формы:
            # у сжатого и сырого обороты разные (ESI §5.5)
            # Заимствованный свод показывается с оговоркой, а не выдаётся
            # за свой: оборота этого региона мы не знаем (ESI §5.5)
            "borrowed": stats.borrowed,
            "unconfirmed": stats.unconfirmed(scenario.price),
            "short_of_volume": stats.short_of_volume(scenario.qty),
            "slow_for_volume": stats.slow_for_volume(scenario.qty),
        }
    return rows


def _grid_notes(gas: Gas, book: prices.PriceBook) -> list[str]:
    """Что сказать пользователю про происхождение и свежесть цен.

    Молчать нельзя ни про один из случаев: пустая база, устаревшие данные
    и хаб без среза выглядят на экране одинаково — пустой ячейкой.
    """
    notes: list[str] = []
    missing_forms = [
        FORM_LABELS[form]
        for form in (GasForm.RAW, GasForm.COMPRESSED)
        if form not in _known_type_ids(gas)
    ]
    if missing_forms:
        notes.append(
            f"{gas.name}: в data/gases.json не заполнен type_id "
            f"({', '.join(missing_forms)}) — эти колонки заполняются только вручную."
        )

    if book.error is not None:
        notes.append(
            f"База недоступна ({book.error}). Расчёт по ценам, введённым вручную, "
            f"работает как обычно."
        )
        return notes

    if book.empty:
        notes.append(
            "Цены ещё не собраны. Сбор идёт по расписанию отдельной задачей "
            "(python -m app.jobs.collect); до первого запуска сетку можно "
            "заполнить руками."
        )
        return notes

    age = book.age()
    if age is not None:
        notes.append(f"Цены из базы, собраны {_humanize_age(age)} назад.")
        max_age = timedelta(minutes=int(current_app.config["PRICE_MAX_AGE_MINUTES"]))
        if book.is_stale(max_age):
            notes.append(
                f"Данные устарели: старше {_humanize_age(max_age)}. "
                f"Похоже, сбор цен не отработал — проверьте задачу в cron."
            )

    names = {hub.key: hub.name for hub in catalog.hubs()}
    illiquid = sorted(
        f"{names.get(hub_key, hub_key)} · {FORM_LABELS[form]} {side.value}"
        for (hub_key, form, side), stored in book.quotes.items()
        if stored.no_liquid_orders
    )
    if illiquid:
        notes.append(
            "Ликвидных ордеров нет: "
            + ", ".join(illiquid)
            + ". Стакан там есть, но все ордера вне рынка — по таким ценам не торгуют."
        )

    dropped = sum(stored.dropped for stored in book.quotes.values())
    if dropped:
        notes.append(
            f"Отброшено ордеров вне рынка: {dropped}. Цена считается по тем, "
            f"по которым реально идут сделки."
        )

    if book.missing_hubs:
        notes.append(
            "Данных нет по хабам: "
            + ", ".join(names.get(key, key) for key in book.missing_hubs)
            + ". Эти строки не участвуют в расчёте — ноль вместо цены был бы враньём."
        )
    return notes


def _humanize_age(age: timedelta) -> str:
    """«12 мин», «1 ч 30 мин», «2 сут» — без секунд и дробей.

    Остаток минут не отбрасывается: порог в 90 минут, показанный как «1 ч»,
    выглядит опечаткой и заставляет лезть в конфиг.
    """
    minutes = int(age.total_seconds() // 60)
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {rest} мин" if rest else f"{hours} ч"
    days, rest_hours = divmod(hours, 24)
    return f"{days} сут {rest_hours} ч" if rest_hours else f"{days} сут"


def _build_grid(
    form: Mapping[str, str], book: prices.PriceBook
) -> list[HubRow]:
    """Сетка цен: значения из базы, поверх них — ручной ввод.

    Ручной ввод всегда главнее. Ячейка считается ручной, пока в форме нет
    пометки auto: её ставит сервер при подстановке из базы и снимает JS,
    как только пользователь начал печатать.
    """
    rows: list[HubRow] = []
    for hub in catalog.hubs():
        cells: dict[str, PriceCell] = {}
        for suffix, _label in PRICE_COLUMNS:
            field_name = f"{hub.key}_{suffix}"
            typed = form.get(field_name, "").strip()
            was_auto = form.get(f"{field_name}_auto") == "1"
            if typed and not was_auto:
                cells[suffix] = PriceCell(value=typed, auto=False, depth=None)
                continue

            gas_form_value, side_value = suffix.rsplit("_", 1)
            stored = book.get(hub.key, GasForm(gas_form_value), OrderSide(side_value))
            if stored is not None and stored.price is not None:
                cells[suffix] = PriceCell(
                    value=fmt_number(round(stored.price, 2)),
                    auto=True,
                    depth=stored.quote.available,
                )
            else:
                cells[suffix] = PriceCell()
        rows.append(HubRow(hub=hub, rate=form.get(f"{hub.key}_rate", "").strip(), cells=cells))
    return rows


def _render_grid(grid: list[HubRow], notes: list[str]) -> str:
    """Фрагмент сетки — ответ на GET /price-grid."""
    return render_template(
        "partials/prices.html",
        grid=grid,
        price_columns=PRICE_COLUMNS,
        grid_notes=notes or None,
    )



def _for_template(saved: Mapping[str, str]) -> dict[str, object]:
    """Сохранённые настройки в том виде, в каком их ждёт шаблон.

    Хранилище говорит именами полей формы и строками — так их присылает
    браузер. Шаблон местами говорит иначе: уровень навыка он сравнивает
    с числом из ``range()``, а брокерский процент берёт из ключа
    ``broker_pct``. Без перевода «4» == 4 ложно и sell_only не находится:
    настройка не восстанавливается, и ошибку не видно — поле просто
    показывает умолчание."""
    values: dict[str, object] = dict(saved)

    if (level := saved.get("gde_level")) is not None:
        try:
            values["gde_level"] = int(level)
        except ValueError:
            values.pop("gde_level")  # в базе мусор — пусть будет умолчание

    if (fee := saved.get("broker_fee")) is not None:
        values["broker_pct"] = fee

    return values

@bp.get("/")
def index() -> str:
    """Страница с формой: параметры расчёта и сетка цен из базы."""
    gases_by_family: dict[str, list] = {}
    for gas in catalog.gases():
        gases_by_family.setdefault(gas.family, []).append(gas)

    # Настройки вошедшего перебивают умолчания. У анонима их нет — он живёт
    # в localStorage, и это по-прежнему основной режим работы.
    who = current_character()
    saved = (
        user_settings.load(current_app.extensions["db_engine"], who[0])
        if who is not None
        else user_settings.StoredSettings()
    )
    values = dict(DEFAULTS) | _for_template(saved.values)

    try:
        gas = catalog.gas_by_key(str(values["gas"]))
        structure = StructureType(str(values["structure"]))
    except (KeyError, ValueError):
        # Сохранённое значение устарело: газ переименовали или тип структуры
        # исчез. Подставляем умолчание, а не роняем страницу.
        gas = catalog.gas_by_key(DEFAULTS["gas"])
        structure = StructureType(DEFAULTS["structure"])
        values = dict(DEFAULTS)

    needed = _needed_by_form(
        gas, int(values["n_units"]), structure, int(values["gde_level"])
    )
    book = _load_book(gas, needed)

    return render_template(
        "index.html",
        character=who,
        sso_enabled=settings_or_none() is not None,
        offer_import=session.pop("offer_settings_import", False),
        gases_by_family=gases_by_family,
        family_labels=FAMILY_LABELS,
        structure_labels=STRUCTURE_LABELS,
        gde_levels=range(GDE_MIN_LEVEL, GDE_MAX_LEVEL + 1),
        hubs=catalog.hubs(),
        price_columns=PRICE_COLUMNS,
        grid=_build_grid({f"{key}_rate": rate for key, rate in saved.freight_rates.items()}, book),
        grid_notes=_grid_notes(gas, book) or None,
        defaults=values,
        volume_labels={gas.key: _volume_label(gas) for gas in catalog.gases()},
        eta_map_json=json.dumps(_eta_percent_map()),
        initial_eta_pct=_eta_percent_map()[structure.value][int(values["gde_level"])],
    )


@bp.post("/price-grid")
def price_grid() -> str:
    """Перерисовка сетки под новый газ или объём (HTMX).

    Вешается только на поля, влияющие на потребность: газ, количество,
    структура, навык. На ввод в самих ячейках не вешается намеренно —
    иначе ответ затирал бы то, что пользователь печатает прямо сейчас.
    """
    form = request.form
    errors: list[str] = []
    gas = _parse_gas(form, errors)
    n_units = _parse_n_units(form, errors)
    structure = _parse_structure(form, errors)
    gde_level = _parse_gde_level(form, errors)

    if errors or gas is None or structure is None:
        # Форму чинит пользователь, а сетку в этот момент трогать нельзя:
        # возвращаем то, что уже введено, и молчим про цены
        return _render_grid(_build_grid(form, prices.PriceBook(quotes={})), [])

    needed = _needed_by_form(gas, n_units, structure, gde_level)
    book = _load_book(gas, needed)
    return _render_grid(_build_grid(form, book), _grid_notes(gas, book))


@bp.get("/healthz")
def healthz() -> tuple[dict, int]:
    """Состояние приложения для мониторинга и для проверки после развёртывания."""
    engine = current_app.extensions["db_engine"]
    payload: dict[str, object] = {"profile": current_app.config["APP_ENV"]}
    status = 200

    try:
        last_run = prices.last_successful_run(engine)
        last_history = prices.last_successful_run(engine, kind="history")
        payload["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 — здесь важен факт недоступности, а не тип
        payload["database"] = f"недоступна: {exc}"
        return payload, 503

    # История — вспомогательный сбор: он отсекает мусор, но без него приложение
    # считает по прежним правилам. Поэтому её возраст показывается, а статус
    # ответа не роняет: суточный сбор и не обязан быть свежее суток.
    payload["last_history_collection"] = last_history.isoformat() if last_history else None
    if last_history is not None:
        payload["history_age_hours"] = int((utcnow() - last_history).total_seconds() // 3600)

    payload["last_collection"] = last_run.isoformat() if last_run else None
    if last_run is None:
        payload["prices"] = "сбор ещё ни разу не отработал"
        return payload, status

    age = utcnow() - last_run
    payload["collection_age_minutes"] = int(age.total_seconds() // 60)
    max_age = timedelta(minutes=int(current_app.config["PRICE_MAX_AGE_MINUTES"]))
    if age > max_age:
        payload["prices"] = "устарели"
        status = 503
    else:
        payload["prices"] = "ok"
    return payload, status


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

    collateral_pct = 0.0
    raw = form.get("collateral_pct", "").strip()
    if not raw:
        errors.append("«Обеспечение»: поле пустое.")
    else:
        try:
            pct = _to_float(raw)
            if not 0 <= pct <= COLLATERAL_PCT_MAX * 100:
                errors.append(
                    f"«Обеспечение»: число от 0 до {COLLATERAL_PCT_MAX * 100:g} (в процентах)."
                )
            else:
                collateral_pct = pct / 100  # в форме проценты, в ядре — доля
        except ValueError:
            errors.append(f"«Обеспечение»: не похоже на число: {raw!r}.")

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
        collateral_pct=collateral_pct,
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

    history_rows = _history_rows(inp.gas, result.scenarios)
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
        history_rows=history_rows,
        has_borrowed=any(row["borrowed"] for row in history_rows.values()),
        has_unconfirmed=any(row["unconfirmed"] for row in history_rows.values()),
        has_volume_warning=any(
            row["short_of_volume"] or row["slow_for_volume"] for row in history_rows.values()
        ),
        history_last_day=max(
            (row["last_day"] for row in history_rows.values() if row["last_day"]),
            default=None,
        ),
        warning_codes=WarningCode,
    )


@bp.get("/api/gases")
def api_gases() -> dict:
    """JSON-справочник газов для клиентского JS (SPEC §9).

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
