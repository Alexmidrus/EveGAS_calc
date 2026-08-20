"""HTTP-эндпоинты, разбор формы расчёта и чтение собранных цен.

К ESI отсюда не ходят: цены приезжают из базы, куда их складывает сборщик
по расписанию (app/jobs/collect.py). Пользователь не может инициировать
обращение к внешнему API ни одним действием — это жёсткое правило проекта.
"""

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from flask import Blueprint, current_app, render_template, request, session

from app.core import calculator, catalog
from app.core.constants import GDE_MAX_LEVEL, GDE_MIN_LEVEL, PRICE_OUTLIER_FACTOR
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
from app.formatting import (
    bar_width,
    fmt_compact,
    fmt_number,
    fmt_percent,
    fmt_share,
    share_percents,
    sparkline_change_pct,
    sparkline_points,
)
from app.db import utcnow
from app.services import prices, server_status, user_settings

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
# Подписи формы в сетке и в таблице результата. Отдельно от FORM_LABELS:
# там подписи идут внутри фразы («сжатый, buy»), здесь — заголовком колонки
# и бейджем, и там макет говорит по-английски.
GRID_FORM_LABELS = {
    GasForm.RAW: "Сырой",
    GasForm.COMPRESSED: "Compressed",
}


def price_groups() -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Ценовые колонки, сгруппированные по форме — для двухэтажной шапки сетки.

    Считается из PRICE_COLUMNS, а не выписывается рядом: две независимые
    таблицы разъедутся молча, и шапка начнёт врать про то, что под ней.
    """
    grouped: dict[GasForm, list[tuple[str, str]]] = {}
    for suffix, _label in PRICE_COLUMNS:
        form_value, side_value = suffix.rsplit("_", 1)
        grouped.setdefault(GasForm(form_value), []).append((suffix, side_value))
    return tuple(
        (GRID_FORM_LABELS[form], tuple(columns)) for form, columns in grouped.items()
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
            "series": stats.series,
            "unconfirmed": stats.unconfirmed(scenario.price),
            "short_of_volume": stats.short_of_volume(scenario.qty),
            "slow_for_volume": stats.slow_for_volume(scenario.qty),
        }
    return rows


def _history_last_day(gas: Gas) -> date | None:
    """Последний день истории сделок по этому газу — для чипа в шапке.

    Тот же свод, что показывает таблица результата, но нужен он уже при
    открытии страницы: чип «история ДД.ММ.ГГГГ» стоит в шапке, а не в ответе
    /calculate. Недоступная база здесь молчит — как и везде, где история
    вспомогательная: чип просто не рисуется.
    """
    stats = prices.load_history_stats(
        current_app.extensions["db_engine"], catalog.hubs(), _known_type_ids(gas)
    )
    return max(
        (value.last_day for value in stats.values() if value.last_day is not None),
        default=None,
    )


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
        price_groups=price_groups(),
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

# --- Подготовка чисел для шаблона (SPEC §10.3) ---
#
# Всё, что в макете написано в style="…" и зависит от данных — ширина полоски,
# цвет по порогу, доли в раскладке стоимости — считается здесь. В шаблоне
# арифметики быть не должно: там её не видно и не проверить тестом.

# Порог отставания от лучшего, за которым Δ красится тревожным цветом, %
DELTA_WARN_PCT = 8.0
DELTA_BAD_PCT = 25.0
# Ниже этой доли размаха ISK/юнит строка ещё читается как «рядом с лучшей»
ISK_NEAR_SHARE = 0.35
# Полоска ISK/юнит: лучшая строка во всю ширину, худшая — в остаток
ISK_BAR_MIN_PCT = 6.0
ISK_BAR_SPAN_PCT = 76.0
# Огрызок полоски ликвидности: нулевая ширина читалась бы как «данных нет»
LIQ_BAR_MIN_PCT = 4.0
# Портреты персонажей. Единственный внешний адрес, который приложение отдаёт
# браузеру (CLAUDE.md, «Внешние запросы»): сама картинка ходит с машины
# пользователя к CCP, приложение за ней не ходит и её не кэширует.
PORTRAIT_BASE_URL = "https://images.evetech.net"
# Кружок в шапке 24 px; берём вдвое больше — под экраны с двойной плотностью.
# Сервис отдаёт только фиксированный ряд размеров, 64 — ближайший подходящий.
PORTRAIT_SIZE = 64
# Насколько цена должна уйти за неделю, чтобы спарклайн назвал это движением.
# Ниже порога линия рисуется нейтральным цветом: недельный шум в полпроцента —
# не тренд, и красить его в «дешевеет» значит обещать то, чего мы не знаем.
SPARK_FLAT_PCT = 2.0


def portrait_url(character_id: int, size: int = PORTRAIT_SIZE) -> str:
    """Адрес портрета персонажа на сервере изображений CCP.

    Публичный сервис, авторизации не требует: портрет персонажа — открытые
    данные, и scope у нас всё равно пустой.
    """
    return f"{PORTRAIT_BASE_URL}/characters/{character_id}/portrait?size={size}"


def scenario_slots() -> int:
    """Сколько строк вообще могло получиться: хабы × ценовые колонки.

    Знаменатель карточки «Сценариев в расчёте». Константы здесь быть не может:
    шестой хаб появится — двадцатка соврёт, и никто этого не заметит.
    """
    return len(catalog.hubs()) * len(PRICE_COLUMNS)


def _low_volume_notes(scenario, history: Mapping[str, object] | None) -> list[str]:
    """Расшифровка пометки «маленький оборот» — по одной фразе на причину.

    Причин три, и они про разное: недельный оборот, суточный оборот и глубина
    стакана. Склеивать их в одну формулировку нельзя — человек не поймёт,
    ждать ему несколько дней или искать другой хаб.
    """
    notes: list[str] = []
    if history is not None and history["short_of_volume"]:
        notes.append(
            f"За неделю в регионе продано {fmt_compact(history['window_volume'])} юнитов — "
            f"меньше, чем нужно ({fmt_number(scenario.qty)}). Столько не набрать."
        )
    elif history is not None and history["slow_for_volume"]:
        notes.append(
            f"В сутки в регионе продают {fmt_compact(history['daily_volume'])} юнитов, "
            f"а нужно {fmt_number(scenario.qty)} — набирать придётся несколько дней."
        )
    if WarningCode.SHALLOW_BOOK in scenario.warnings and scenario.available_qty is not None:
        notes.append(
            f"В стакане сейчас {fmt_number(scenario.available_qty)} юнитов из "
            f"{fmt_number(scenario.qty)} — остальное придётся добирать дороже "
            f"или в другом хабе."
        )
    return notes


def _row_view(
    scenario,
    index: int,
    history: Mapping[str, object] | None,
    isk_span: tuple[float, float],
    max_daily: float,
) -> dict[str, object]:
    """Одна строка таблицы результата в том виде, в каком её рисует шаблон."""
    min_isk, max_isk = isk_span
    spread = max_isk - min_isk
    is_best = index == 0
    # Доля строки в размахе ISK/юнит: 0 у лучшей, 1 у худшей
    rel = (scenario.isk_per_unit - min_isk) / spread if spread > 0 else 0.0

    # Стоимость газа с брокерской комиссией: то, что осталось от итога
    # за вычетом обеих частей доставки (DOMAIN §3)
    gas_cost = scenario.total - scenario.freight_volume - scenario.collateral_fee
    gas_pct, freight_pct, coll_pct = share_percents(
        (gas_cost, scenario.freight_volume, scenario.collateral_fee), scenario.total
    )

    low_volume = _low_volume_notes(scenario, history)
    unconfirmed = bool(history is not None and history["unconfirmed"])
    outlier = WarningCode.PRICE_OUTLIER in scenario.warnings

    if history is not None and history["short_of_volume"]:
        liquidity_tone, liquidity_note = "bad", "столько не набрать"
    elif history is not None and history["slow_for_volume"]:
        liquidity_tone, liquidity_note = "warn", "набирать несколько дней"
    else:
        # Глубина стакана — не про оборот, и её число стоит в колонке «Купить».
        # Дублировать его здесь значит сказать одно и то же двумя цифрами.
        liquidity_tone, liquidity_note = "ok", ""

    # Спарклайн «7 дней»: форма движения дневных средних по паре «регион + тип».
    # Сторону история не разделяет, поэтому у sell и buy одного хаба линия одна
    # и та же — это свойство данных ESI (§5.2), а не недосмотр.
    series: tuple[float, ...] = tuple(history["series"]) if history is not None else ()
    spark_points = sparkline_points(series)
    spark_change = sparkline_change_pct(series)

    if not spark_points or spark_change is None:
        # Линии нет — молчим числом, но объясняем словом: пустая ячейка без
        # подсказки читается как поломка. Причины две, и путать их нельзя:
        # заимствованный свод — это «своей истории у хаба нет вовсе», а не
        # «сделок было мало».
        spark_tone = "flat"
        if history is not None and history["borrowed"]:
            spark_title = (
                "Недельной линии нет: своей истории по этому хабу нет, "
                "коридор цен взят по другим хабам — чужое движение здесь "
                "рисовать нечестно"
            )
        else:
            spark_title = (
                "Недельной линии нет: в истории по этому хабу меньше двух дней "
                "с реальными сделками"
            )
    else:
        # Приложение покупает газ, а не продаёт его: дешевеющая цена — хорошая
        # новость, и зелёный тут именно за падение. В макете цвета стоят
        # наоборот, но макет не источник смысла (CLAUDE.md).
        if spark_change <= -SPARK_FLAT_PCT:
            spark_tone, spark_verdict = "good", "дешевеет"
        elif spark_change >= SPARK_FLAT_PCT:
            spark_tone, spark_verdict = "bad", "дорожает"
        else:
            spark_tone, spark_verdict = "flat", "без движения"
        spark_title = (
            f"{len(series)} дн.: {fmt_number(series[0])} → {fmt_number(series[-1])} ISK, "
            f"{'+' if spark_change > 0 else ''}{fmt_percent(spark_change, 1)}"
            f" — {spark_verdict}. Дневные средние сделок по региону, обе стороны вместе"
        )

    if scenario.delta_pct is None:
        delta_tone = "muted"
    elif scenario.delta_pct > DELTA_BAD_PCT:
        delta_tone = "bad"
    elif scenario.delta_pct > DELTA_WARN_PCT:
        delta_tone = "warn"
    else:
        delta_tone = "plain"

    return {
        "scenario": scenario,
        "rank": index + 1,
        "is_best": is_best,
        "is_odd": index % 2 == 1,
        "history": history,
        # Полоска ISK/юнит: чем дешевле, тем длиннее — лучшая строка самая длинная
        "isk_bar": bar_width(100.0 - rel * ISK_BAR_SPAN_PCT, floor=ISK_BAR_MIN_PCT),
        "isk_tone": "best" if is_best else ("near" if rel < ISK_NEAR_SHARE else "far"),
        "delta_tone": delta_tone,
        "gas_cost": gas_cost,
        "gas_pct": gas_pct,
        "freight_pct": freight_pct,
        "collateral_pct": coll_pct,
        "stack_label": (
            f"газ {fmt_share(gas_pct)}% · доставка {fmt_share(freight_pct)}%"
            f" · страховка {fmt_share(coll_pct)}%"
        ),
        "liquidity_bar": bar_width(
            (history["daily_volume"] / max_daily * 100) if history and max_daily else 0.0,
            floor=LIQ_BAR_MIN_PCT,
        ),
        "liquidity_tone": liquidity_tone,
        "liquidity_note": liquidity_note,
        "spark_points": spark_points,
        "spark_tone": spark_tone,
        "spark_title": spark_title,
        # Три значка макета поверх пяти внутренних пометок (ROADMAP 12.5)
        "flag_no_trades": unconfirmed,
        "flag_anomaly": outlier,
        "flag_low_volume": bool(low_volume),
        "title_no_trades": (
            "Сделок по такой цене не было: за неделю по ней не торговали — "
            "ордер, скорее всего, не исполнится."
            if unconfirmed
            else ""
        ),
        "title_anomaly": (
            f"Аномальная цена: отличается от медианы по остальным хабам больше чем "
            f"в {fmt_number(PRICE_OUTLIER_FACTOR)} раза. Проверьте, не опечатка ли."
            if outlier
            else ""
        ),
        "title_low_volume": " ".join(low_volume),
    }


def _row_views(
    scenarios: Sequence[object], history_rows: Mapping[str, Mapping[str, object]]
) -> list[dict[str, object]]:
    """Таблица результата целиком: ширины полосок считаются от общего размаха."""
    if not scenarios:
        return []
    # Сценарии уже отсортированы по ISK/юнит по возрастанию (calculator)
    isk_span = (scenarios[0].isk_per_unit, scenarios[-1].isk_per_unit)
    matched = [
        history_rows.get(_history_row_key(s.hub_key, s.form, s.side)) for s in scenarios
    ]
    max_daily = max(
        (float(row["daily_volume"]) for row in matched if row is not None), default=0.0
    )
    return [
        _row_view(scenario, index, history, isk_span, max_daily)
        for index, (scenario, history) in enumerate(zip(scenarios, matched))
    ]


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
    age = book.age()

    return render_template(
        "index.html",
        character=who,
        gas=gas,
        # Чипы состояния данных в шапке. Возраст цен и дата истории живут
        # и в заметках сетки, и в сносках результата, но шапке нужны сами
        # значения, а не предложения вокруг них (ROADMAP 12.2)
        price_age=_humanize_age(age) if age is not None else None,
        history_day=_history_last_day(gas),
        # Состояние Tranquility снимает сборщик, страница читает из базы:
        # пользователь до ESI не дотягивается по определению (CLAUDE.md)
        server=server_status.load(current_app.extensions["db_engine"]),
        # None, если портреты выключены в конфиге или никто не вошёл: тогда
        # в шапке остаётся буква в кружке, а внешних адресов у страницы нет
        portrait=(
            portrait_url(who[0])
            if who is not None and current_app.config["CHARACTER_PORTRAITS"]
            else None
        ),
        scenario_slots=scenario_slots(),
        sso_enabled=settings_or_none() is not None,
        offer_import=session.pop("offer_settings_import", False),
        gases_by_family=gases_by_family,
        family_labels=FAMILY_LABELS,
        structure_labels=STRUCTURE_LABELS,
        gde_levels=range(GDE_MIN_LEVEL, GDE_MAX_LEVEL + 1),
        hubs=catalog.hubs(),
        price_columns=PRICE_COLUMNS,
        price_groups=price_groups(),
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
        return render_template(
            "partials/results.html", errors=errors, scenario_slots=scenario_slots()
        )

    try:
        result = calculator.build_scenarios(inp, prices)
    except ValueError as exc:
        # Подстраховка: ядро валидирует жёстче формы. Ошибку показываем, не глотаем.
        return render_template(
            "partials/results.html", errors=[str(exc)], scenario_slots=scenario_slots()
        )

    history_rows = _history_rows(inp.gas, result.scenarios)
    return render_template(
        "partials/results.html",
        errors=None,
        result=result,
        inp=inp,
        rows=_row_views(result.scenarios, history_rows),
        scenario_slots=scenario_slots(),
        hub_names={hub.key: hub.name for hub in catalog.hubs()},
        form_labels=FORM_LABELS,
        grid_form_labels=GRID_FORM_LABELS,
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
