"""Структуры данных ядра расчёта.

Только stdlib и константы проекта. calculator.py по правилам проекта
импортирует лишь stdlib и этот модуль, поэтому игровые константы
реэкспортируются отсюда. Источник истины по числам — constants.py.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.core.constants import (
    COLLATERAL_FEE_RATE,
    COMPRESSION_QUANTITY_RATIO,
    COMPRESSION_VOLUME_RATIO,
    ETA_BASE,
    GDE_BONUS_PER_LEVEL,
    GDE_MAX_LEVEL,
    GDE_MIN_LEVEL,
    PRICE_OUTLIER_FACTOR,
    STRUCTURE_BONUS,
)

__all__ = [
    # реэкспорт констант — для calculator.py
    "COLLATERAL_FEE_RATE",
    "COMPRESSION_QUANTITY_RATIO",
    "COMPRESSION_VOLUME_RATIO",
    "ETA_BASE",
    "GDE_BONUS_PER_LEVEL",
    "GDE_MAX_LEVEL",
    "GDE_MIN_LEVEL",
    "PRICE_OUTLIER_FACTOR",
    "STRUCTURE_BONUS",
    # перечисления
    "CollateralMode",
    "GasForm",
    "OrderSide",
    "StructureType",
    "WarningCode",
    # структуры
    "Breakeven",
    "CalcInput",
    "CalcResult",
    "Gas",
    "Hub",
    "HubDepth",
    "HubPrices",
    "Scenario",
    "Summary",
]


class StructureType(StrEnum):
    """Тип структуры, на которой выполняется разжатие."""

    UPWELL = "upwell"
    ATHANOR = "athanor"
    TATARA = "tatara"


# Сверка: каждому типу структуры должен соответствовать бонус в constants.py
if set(STRUCTURE_BONUS) != {s.value for s in StructureType}:
    raise RuntimeError(
        "STRUCTURE_BONUS в constants.py не совпадает со StructureType в models.py"
    )


class GasForm(StrEnum):
    """Форма газа: сырой или сжатый."""

    RAW = "raw"
    COMPRESSED = "compressed"


class OrderSide(StrEnum):
    """Сторона стакана, по которой покупаем."""

    SELL = "sell"
    BUY = "buy"


class CollateralMode(StrEnum):
    """Режим обеспечения: ручная сумма или равная стоимости закупки."""

    MANUAL = "manual"
    AUTO = "auto"


class WarningCode(StrEnum):
    """Машинные коды предупреждений; тексты для пользователя — забота шаблонов."""

    PRICE_OUTLIER = "price_outlier"  # цена отличается от медианы других хабов > чем в 3 раза
    SHALLOW_BOOK = "shallow_book"    # в стакане меньше юнитов, чем нужно купить


@dataclass(frozen=True, slots=True)
class Gas:
    """Один тип газа из data/gases.json."""

    key: str
    name: str
    family: str
    volume_raw: float          # м³ за юнит сырого
    volume_compressed: float   # м³ за юнит сжатого
    raw_type_id: int | None    # None, пока не заполнен из SDE — не выдумывать
    compressed_type_id: int | None


@dataclass(frozen=True, slots=True)
class Hub:
    """Трейд-хаб из constants.HUBS.

    system_id известен не для всех хабов и может быть None — выдумывать его
    запрещено. Разбор стакана умеет обходиться без него (см. orderbook.py).
    """

    key: str
    name: str
    region_id: int
    station_id: int
    system_id: int | None = None


@dataclass(frozen=True, slots=True)
class HubDepth:
    """Глубина стакана по одному хабу: сколько юнитов реально доступно.

    Имена полей совпадают с HubPrices — по ним же идёт разбор сценариев.
    Заполняется только при подтяжке из ESI; при ручном вводе остаётся None,
    и предупреждения о глубине не показываются вовсе (SPEC §6).
    """

    raw_sell: int | None = None
    raw_buy: int | None = None
    compressed_sell: int | None = None
    compressed_buy: int | None = None


@dataclass(frozen=True, slots=True)
class HubPrices:
    """Ввод по одному хабу: ставка доставки и четыре цены. Любое поле может быть None."""

    freight_rate: float | None = None  # ISK/м³; None → хаб целиком выпадает из расчёта
    raw_sell: float | None = None
    raw_buy: float | None = None
    compressed_sell: float | None = None
    compressed_buy: float | None = None
    depth: HubDepth | None = None      # известна только для цен из ESI


@dataclass(frozen=True, slots=True)
class CalcInput:
    """Параметры расчёта (блок 1 интерфейса).

    include_collateral_fee — «включать обеспечение в итог» из SPEC. Управляет
    включением сбора 0.5% в итоговую сумму; сам залог расходом не считается
    никогда (это возвратные деньги), а сбор всегда считается и показывается.
    Семантика выверена по контрольному примеру DOMAIN.md §5.
    """

    gas: Gas
    n_units: int               # нужно юнитов сырого газа, N
    structure: StructureType
    gde_level: int             # 0..5
    broker_fee: float          # доля, например 0.015
    collateral_mode: CollateralMode = CollateralMode.MANUAL
    collateral_manual: float = 0.0   # ISK, используется только в ручном режиме
    include_collateral_fee: bool = False  # по умолчанию выкл (SPEC §3)
    # «Только sell» (SPEC §5.4): buy-ордер — это заявка, а не покупка. Когда газ
    # нужен сегодня, половина строк — шум. По умолчанию выключен.
    sell_only: bool = False


@dataclass(frozen=True, slots=True)
class Scenario:
    """Одна строка результата со всеми промежуточными числами.

    freight_volume — чистая доставка (объём × ставка), как в колонке «Доставка»
    контрольного примера DOMAIN §5. freight — она же плюс сбор за обеспечение,
    как описано в SPEC §5.2. Шаблон сам решит, что показывать.
    """

    hub_key: str
    form: GasForm
    side: OrderSide
    price: float           # цена за юнит, как введена
    qty: int               # сколько юнитов покупаем (для сжатого — с запасом)
    volume_m3: float       # объём груза
    purchase: float        # qty * price
    broker: float          # брокерская комиссия (0 для sell)
    freight_volume: float  # volume_m3 * ставка хаба
    collateral: float      # сумма обеспечения C этого сценария
    collateral_fee: float  # C * 0.005
    freight: float         # freight_volume + collateral_fee
    total: float           # итог с учётом флага include_collateral_fee
    isk_per_unit: float    # total / N — главная метрика
    delta_pct: float | None = None       # отставание от лучшего, %; None у первой строки
    available_qty: int | None = None     # глубина стакана; None — если она неизвестна
    warnings: tuple[WarningCode, ...] = ()


@dataclass(frozen=True, slots=True)
class Summary:
    """Сводка над таблицей (SPEC §5.1)."""

    best: Scenario
    worst: Scenario
    savings_vs_worst: float      # ISK экономии лучшего против худшего на всей партии
    loss_units: int | None       # сгорит юнитов при разжатии; None, если сжатых сценариев нет
    loss_pct: float | None       # то же в процентах от купленного сжатого


@dataclass(frozen=True, slots=True)
class Breakeven:
    """Точка безубыточности сжатого газа для одного хаба (DOMAIN §4)."""

    hub_key: str
    price: float  # сжатый выгоднее сырого, пока стоит дешевле этой цены


@dataclass(frozen=True, slots=True)
class CalcResult:
    """Полный результат расчёта."""

    eta: float                        # эффективность разжатия
    compressed_qty: int               # ceil(N / eta) — сколько сжатого пришлось бы купить
    scenarios: tuple[Scenario, ...]   # отсортированы по isk_per_unit по возрастанию
    summary: Summary | None           # None, если ни одного сценария не вышло
    breakevens: tuple[Breakeven, ...]
    skipped_hubs: tuple[str, ...]     # хабы, выпавшие из-за незаданной ставки доставки
