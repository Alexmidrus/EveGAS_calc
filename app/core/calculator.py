"""Чистая математика расчёта. Все формулы — из docs/DOMAIN.md.

Правило проекта: модуль не импортирует ничего, кроме stdlib и app.core.models.
Игровые константы приходят через реэкспорт в models.py.
"""

import math
from collections.abc import Mapping
from dataclasses import replace
from fractions import Fraction
from statistics import median

from app.core.models import (
    COMPRESSION_VOLUME_RATIO,
    ETA_BASE,
    GDE_BONUS_PER_LEVEL,
    GDE_MAX_LEVEL,
    GDE_MIN_LEVEL,
    PRICE_OUTLIER_FACTOR,
    STRUCTURE_BONUS,
    Breakeven,
    CalcInput,
    CalcResult,
    GasForm,
    HubPrices,
    OrderSide,
    Scenario,
    StructureType,
    Summary,
    WarningCode,
)

# Сколько десятичных знаков eta считаем значащими (см. _eta_as_fraction).
_ETA_DECIMALS = 6

# Порядок сценариев внутри хаба: (форма, сторона, имя поля в HubPrices)
_SCENARIO_FIELDS: tuple[tuple[GasForm, OrderSide, str], ...] = (
    (GasForm.RAW, OrderSide.SELL, "raw_sell"),
    (GasForm.RAW, OrderSide.BUY, "raw_buy"),
    (GasForm.COMPRESSED, OrderSide.SELL, "compressed_sell"),
    (GasForm.COMPRESSED, OrderSide.BUY, "compressed_buy"),
)


def decompression_efficiency(structure: StructureType | str, gde_level: int) -> float:
    """eta = 0.80 + бонус структуры + 0.01 за уровень GDE (DOMAIN §1).

    Сумма во float оставляет мусорные хвосты (0.80 + 0.10 + 0.05 даёт
    0.9500000000000001), поэтому результат округляется до 4 знаков —
    все допустимые eta кратны 0.01, округление ничего не искажает.
    """
    structure = StructureType(structure)
    if isinstance(gde_level, bool) or not isinstance(gde_level, int):
        raise ValueError(f"Уровень GDE должен быть целым числом, получено: {gde_level!r}")
    if not GDE_MIN_LEVEL <= gde_level <= GDE_MAX_LEVEL:
        raise ValueError(
            f"Уровень GDE должен быть в диапазоне "
            f"{GDE_MIN_LEVEL}..{GDE_MAX_LEVEL}, получено: {gde_level}"
        )
    return round(ETA_BASE + STRUCTURE_BONUS[structure.value] + GDE_BONUS_PER_LEVEL * gde_level, 4)


def _eta_as_fraction(eta: float) -> Fraction:
    """Точное представление eta как десятичной дроби.

    Во float 0.95 хранится чуть ниже честных 95/100, из-за чего 95 / 0.95
    даёт 100.0000000000000047 и ceil врёт на единицу. Все осмысленные eta —
    короткие десятичные дроби, поэтому переводим во Fraction с точностью
    до _ETA_DECIMALS знаков и дальше считаем точно.
    """
    if not 0.0 < eta <= 1.0:
        raise ValueError(f"eta должна быть в диапазоне (0, 1], получено: {eta!r}")
    scale = 10 ** _ETA_DECIMALS
    return Fraction(round(eta * scale), scale)


def required_compressed_qty(n: int, eta: float) -> int:
    """Сколько сжатого газа купить, чтобы после разжатия получить N сырого.

    compressed_qty = ceil(N / eta), DOMAIN §1. Арифметика точная, через Fraction.
    """
    if isinstance(n, bool) or not isinstance(n, int):
        raise ValueError(f"N должно быть целым числом, получено: {n!r}")
    if n <= 0:
        raise ValueError(f"N должно быть больше нуля, получено: {n}")
    return math.ceil(n / _eta_as_fraction(eta))


def decompressed_output(compressed_qty: int, eta: float) -> int:
    """Сколько сырого газа выйдет из разжатия партии сжатого.

    raw_out = floor(compressed_qty * eta), DOMAIN §1: дробные юниты сгорают.
    """
    if isinstance(compressed_qty, bool) or not isinstance(compressed_qty, int):
        raise ValueError(f"Количество должно быть целым числом, получено: {compressed_qty!r}")
    if compressed_qty < 0:
        raise ValueError(f"Количество не может быть отрицательным: {compressed_qty}")
    return math.floor(compressed_qty * _eta_as_fraction(eta))


def breakeven_compressed_price(eta: float, p_raw: float, volume: float, rate: float) -> float:
    """Цена сжатого, ниже которой он выгоднее сырого в том же хабе.

    P_be = eta * (P_raw + V * r) - V * r / 10 (DOMAIN §4),
    без учёта брокера и обеспечения. volume — объём юнита сырого газа.
    """
    if not 0.0 < eta <= 1.0:
        raise ValueError(f"eta должна быть в диапазоне (0, 1], получено: {eta!r}")
    if p_raw <= 0:
        raise ValueError(f"Цена сырого должна быть больше нуля, получено: {p_raw}")
    if volume <= 0:
        raise ValueError(f"Объём юнита должен быть больше нуля, получено: {volume}")
    if rate < 0:
        raise ValueError(f"Ставка доставки не может быть отрицательной: {rate}")
    return eta * (p_raw + volume * rate) - volume * rate / COMPRESSION_VOLUME_RATIO


def _outlier_keys(
    active: Mapping[str, HubPrices],
) -> set[tuple[str, GasForm, OrderSide]]:
    """Сценарии, чья цена отличается от медианы по остальным хабам сильнее,
    чем в PRICE_OUTLIER_FACTOR раз (SPEC §6). Сравнение — внутри одной
    комбинации «форма × сторона», хабы без ставки доставки не участвуют.
    """
    result: set[tuple[str, GasForm, OrderSide]] = set()
    for form, side, field_name in _SCENARIO_FIELDS:
        entries = [
            (hub_key, price)
            for hub_key, prices in active.items()
            if (price := getattr(prices, field_name)) is not None
        ]
        if len(entries) < 2:
            continue  # сравнивать не с чем
        for hub_key, price in entries:
            others = [p for h, p in entries if h != hub_key]
            med = median(others)
            if med <= 0:
                continue  # цены валидируются положительными, это подстраховка
            if price > med * PRICE_OUTLIER_FACTOR or price < med / PRICE_OUTLIER_FACTOR:
                result.add((hub_key, form, side))
    return result


def build_scenarios(inp: CalcInput, prices_by_hub: Mapping[str, HubPrices]) -> CalcResult:
    """Собирает и сортирует все сценарии закупки (DOMAIN §4).

    Порядок хабов — порядок ключей prices_by_hub (задаётся вызывающим кодом
    по constants.HUBS). Пустая цена — сценарий пропускается; пустая ставка
    доставки — хаб выпадает целиком и попадает в skipped_hubs.

    При inp.sell_only buy-сценарии не строятся вовсе: сводка и «лучший вариант»
    считаются по тому, что осталось. Точки безубыточности фильтр не трогает —
    они и так считаются по sell-ценам.
    """
    if inp.broker_fee < 0:
        raise ValueError(f"Брокерская комиссия не может быть отрицательной: {inp.broker_fee}")
    if inp.collateral_pct < 0:
        raise ValueError(f"Обеспечение не может быть отрицательным: {inp.collateral_pct}")

    eta = decompression_efficiency(inp.structure, inp.gde_level)
    compressed_qty = required_compressed_qty(inp.n_units, eta)

    active: dict[str, HubPrices] = {}
    skipped: list[str] = []
    for hub_key, prices in prices_by_hub.items():
        if prices.freight_rate is None:
            skipped.append(hub_key)
        else:
            if prices.freight_rate < 0:
                raise ValueError(
                    f"Ставка доставки для {hub_key} не может быть отрицательной: "
                    f"{prices.freight_rate}"
                )
            active[hub_key] = prices

    outliers = _outlier_keys(active)

    rows: list[Scenario] = []
    for hub_key, prices in active.items():
        rate = prices.freight_rate
        assert rate is not None  # отфильтровано выше
        for form, side, field_name in _SCENARIO_FIELDS:
            if inp.sell_only and side is OrderSide.BUY:
                continue  # фильтр «только sell» (SPEC §5.4)
            price = getattr(prices, field_name)
            if price is None:
                continue
            if price <= 0:
                raise ValueError(
                    f"Цена {field_name} для {hub_key} должна быть больше нуля: {price}"
                )
            if form is GasForm.RAW:
                qty, unit_volume = inp.n_units, inp.gas.volume_raw
            else:
                qty, unit_volume = compressed_qty, inp.gas.volume_compressed
            available = getattr(prices.depth, field_name) if prices.depth is not None else None
            if available is not None and available < 0:
                raise ValueError(
                    f"Глубина стакана {field_name} для {hub_key} не может быть "
                    f"отрицательной: {available}"
                )
            codes: list[WarningCode] = []
            if (hub_key, form, side) in outliers:
                codes.append(WarningCode.PRICE_OUTLIER)
            if available is not None and available < qty:
                codes.append(WarningCode.SHALLOW_BOOK)
            volume_m3 = qty * unit_volume
            purchase = qty * price
            broker = purchase * inp.broker_fee if side is OrderSide.BUY else 0.0
            # Обеспечение — процент от стоимости газа, без брокерской комиссии:
            # комиссия платится бирже и в трюме не едет (DOMAIN §4).
            collateral_fee = purchase * inp.collateral_pct
            freight_volume = volume_m3 * rate
            freight = freight_volume + collateral_fee
            total = purchase + broker + freight
            rows.append(
                Scenario(
                    hub_key=hub_key,
                    form=form,
                    side=side,
                    price=price,
                    qty=qty,
                    volume_m3=volume_m3,
                    purchase=purchase,
                    broker=broker,
                    freight_volume=freight_volume,
                    collateral_fee=collateral_fee,
                    freight=freight,
                    total=total,
                    isk_per_unit=total / inp.n_units,
                    available_qty=available,
                    warnings=tuple(codes),
                )
            )

    rows.sort(key=lambda s: s.isk_per_unit)  # сортировка устойчива: при равенстве — порядок хабов

    summary: Summary | None = None
    if rows:
        best = rows[0]
        rows[1:] = [
            replace(s, delta_pct=(s.isk_per_unit / best.isk_per_unit - 1) * 100)
            for s in rows[1:]
        ]
        worst = rows[-1]
        has_compressed = any(s.form is GasForm.COMPRESSED for s in rows)
        loss_units = compressed_qty - inp.n_units if has_compressed else None
        loss_pct = (
            (compressed_qty - inp.n_units) / compressed_qty * 100 if has_compressed else None
        )
        summary = Summary(
            best=best,
            worst=worst,
            savings_vs_worst=worst.total - best.total,
            loss_units=loss_units,
            loss_pct=loss_pct,
        )

    breakevens = tuple(
        Breakeven(
            hub_key=hub_key,
            price=breakeven_compressed_price(
                eta, prices.raw_sell, inp.gas.volume_raw, prices.freight_rate
            ),
        )
        for hub_key, prices in active.items()
        if prices.raw_sell is not None and prices.compressed_sell is not None
    )

    return CalcResult(
        eta=eta,
        compressed_qty=compressed_qty,
        scenarios=tuple(rows),
        summary=summary,
        breakevens=breakevens,
        skipped_hubs=tuple(skipped),
    )
