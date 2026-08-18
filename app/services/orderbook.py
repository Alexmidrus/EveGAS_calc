"""Разбор стакана: фильтрация, отсечение выбросов, VWAP.

Модуль ничего не знает про HTTP. На вход — список словарей ровно в том виде,
в каком их отдаёт ESI, на выход — цифры. Поэтому он целиком покрывается
тестами на фикстурах, без единого сетевого запроса.

Все правила — docs/ESI.md §3 и §4. Самое тонкое место проекта: ошибка здесь
не видна глазом, она просто даёт неправильный совет.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from app.core.constants import ORDERBOOK_OUTLIER_FACTOR
from app.core.models import Hub, OrderSide

# Ордер, дотягивающийся до хаба откуда угодно в регионе (ESI §3.2)
RANGE_REGION = "region"


@dataclass(frozen=True, slots=True)
class Quote:
    """Результат разбора одной стороны стакана по одному хабу.

    price     — средневзвешенная по нужному объёму; None, если книга пуста.
    filled    — сколько юнитов удалось набрать (не больше needed).
    available — сколько всего юнитов в пригодной части книги.
    needed    — сколько было нужно.
    dropped   — сколько уровней выброшено как выбросы (ESI §5.4). Фильтр меняет
                цифру, за которой пришёл человек, поэтому число обязано дойти
                до интерфейса, а не остаться внутри модуля.
    """

    price: float | None
    filled: int
    available: int
    needed: int
    dropped: int = 0

    @property
    def shallow(self) -> bool:
        """Глубины не хватило на нужный объём — вызывающий обязан это показать."""
        return self.filled < self.needed


def hub_system_id(orders: list[dict], hub: Hub) -> int | None:
    """system_id системы хаба: из справочника, иначе из самого стакана.

    В constants.HUBS system_id заполнен не у всех хабов, а выдумывать его нельзя.
    Но любой ордер, стоящий на станции хаба, приходит вместе с system_id своей
    системы — этого достаточно. Если такого ордера в выдаче нет, возвращаем None,
    и отбор buy-ордеров сужается (см. buy_orders).
    """
    if hub.system_id is not None:
        return hub.system_id
    for order in orders:
        if order.get("location_id") == hub.station_id and order.get("system_id") is not None:
            return int(order["system_id"])
    return None


def _alive(orders: list[dict], *, is_buy: bool) -> list[dict]:
    """Ордера нужной стороны с ненулевым остатком (ESI §4)."""
    return [
        o for o in orders
        if bool(o.get("is_buy_order")) is is_buy and int(o.get("volume_remain", 0)) > 0
    ]


def outlier_bounds(prices: Sequence[float]) -> tuple[float, float] | None:
    """Границы «не мусорной» цены по медиане книги: (низ, верх).

    Отсечение по медиане, а не по среднему: среднее сам выброс и утаскивает.
    На книге из одного ордера сравнивать не с чем, на нулевой медиане — тем
    более; в обоих случаях возвращается None и фильтр не применяется.

    Известный предел правила: опора взята из самой книги. Пока мусор
    в меньшинстве, медиана держится за настоящие ордера. Когда мусора больше
    половины — медиана переезжает к нему, и фильтр начинает выбрасывать
    настоящий ордер. Внешняя опора на историю сделок — ESI §5.4.
    """
    if len(prices) < 2:
        return None
    med = median(prices)
    if med <= 0:
        return None
    return med / ORDERBOOK_OUTLIER_FACTOR, med * ORDERBOOK_OUTLIER_FACTOR


def drop_outliers(orders: list[dict]) -> list[dict]:
    """Выбрасывает ордера, чья цена отличается от медианы книги больше чем в 100 раз."""
    bounds = outlier_bounds([float(o["price"]) for o in orders])
    if bounds is None:
        return list(orders)
    low, high = bounds
    return [o for o in orders if low <= float(o["price"]) <= high]


def drop_outlier_levels(
    levels: Sequence[tuple[Decimal, int, int]],
) -> list[tuple[Decimal, int, int]]:
    """То же правило, но на сохранённой лестнице.

    С этапа 11 отсечение делается при чтении из базы, а не при сборе: правило
    срабатывало до записи, и в мусорной книге настоящий ордер до базы просто
    не доезжал — чинить его потом было бы нечем (ESI §5.4).
    """
    bounds = outlier_bounds([float(price) for price, _volume, _min_volume in levels])
    if bounds is None:
        return list(levels)
    low, high = bounds
    return [level for level in levels if low <= float(level[0]) <= high]


def selected_sells(orders: list[dict], hub: Hub) -> list[dict]:
    """Sell-ордера целевого хаба, по возрастанию цены (ESI §3.1). Без очистки.

    Берём только ордера самой станции: поле range для sell смысла не имеет,
    а ордера в других станциях того же региона — это другой рынок.

    Отбор не зависит ни от объёма пользователя, ни от истории сделок, поэтому
    делается один раз в сборщике и в базу едет уже в этом виде.
    """
    sells = [
        o for o in _alive(orders, is_buy=False)
        if o.get("location_id") == hub.station_id
    ]
    sells.sort(key=lambda o: float(o["price"]))
    return sells


def selected_buys(orders: list[dict], hub: Hub) -> list[dict]:
    """Buy-ордера, конкурирующие в хабе, по убыванию цены (ESI §3.2). Без очистки.

    Берём все региональные ордера плюс любые ордера из системы хаба. Ордер,
    выставленный в соседней системе с радиусом region, реально конкурирует
    с тобой в Jita 4-4, и отфильтровать его по location_id значит занизить
    top buy и посоветовать выставиться ниже рынка.

    Известная слепая зона (осознанный компромисс, ESI §3.2): ордера с числовым
    радиусом (5, 10, 20...) из соседних систем не видны — для их учёта нужна
    карта прыжков из SDE, а SDE в рантайм мы намеренно не тянем.

    Отсечение по min_volume здесь не делается: оно зависит от нужного
    количества, а сборщик его не знает.
    """
    system_id = hub_system_id(orders, hub)
    buys = [
        o for o in _alive(orders, is_buy=True)
        if (
            o.get("range") == RANGE_REGION
            or o.get("location_id") == hub.station_id
            or (system_id is not None and o.get("system_id") == system_id)
        )
    ]
    buys.sort(key=lambda o: float(o["price"]), reverse=True)
    return buys


def sell_orders(orders: list[dict], hub: Hub) -> list[dict]:
    """Sell-ордера хаба, очищенные от выбросов: разбор сырого стакана целиком."""
    return drop_outliers(selected_sells(orders, hub))


def buy_orders(orders: list[dict], hub: Hub, needed: int) -> list[dict]:
    """Buy-ордера хаба под нужный объём, очищенные от выбросов (ESI §3.2, §4).

    needed отсекает ордера, чей min_volume больше нужного количества: такой
    ордер физически не исполнить (ESI §4).
    """
    buys = [
        o for o in selected_buys(orders, hub)
        if int(o.get("min_volume", 1)) <= needed
    ]
    return drop_outliers(buys)


def ladder_from_orders(
    orders: list[dict], hub: Hub, side: OrderSide
) -> list[tuple[str, int, int]]:
    """Лестница стакана для хранения в базе (ROADMAP, этапы 7 и 11).

    Возвращает уровни (цена строкой, доступный объём, min_volume), отобранные
    и отсортированные под свою сторону.

    Что сделано здесь и не переделывается при чтении: отбор по станции и
    радиусу, выброс мёртвых ордеров, сортировка. Всё это от объёма
    пользователя не зависит и от истории сделок тоже.

    Что **не** сделано здесь: отсечение выбросов, отсечение buy-ордеров по
    `min_volume` и сам VWAP.

    Выбросы с этапа 11 отсекаются при чтении (ESI §5.4). Раньше очистка шла
    здесь, до записи, и это ломало главный случай: если мусора в книге больше
    половины, медиана переезжает к нему и выбрасывает настоящий ордер — тот
    самый buy на 6000 при трёх скам-ордерах по 1 ISK. В базу он тогда просто
    не попадал, и чинить это при чтении было уже нечем. Теперь в базе лежит
    то, что реально отдал ESI.

    Цена — строкой: во float 2750.10 хранится с хвостом, а нам эти копейки
    ещё считать.
    """
    book = (
        selected_buys(orders, hub)
        if side is OrderSide.BUY
        else selected_sells(orders, hub)
    )

    return [
        (
            format(float(o["price"]), ".2f"),
            int(o["volume_remain"]),
            int(o.get("min_volume", 1)),
        )
        for o in book
    ]


def vwap(orders: list[dict], needed: int) -> tuple[float | None, int]:
    """Средневзвешенная цена по отсортированной книге на needed юнитов (ESI §4).

    Не лучшая цена: минимальный sell в Rens может быть на 12 юнитов при
    потребности в 50 000. Возвращает (цена, сколько удалось набрать). Если
    набрано меньше needed — цена считается по набранному, а вызывающий код
    обязан показать предупреждение о глубине.
    """
    if needed <= 0:
        raise ValueError(f"Нужное количество должно быть больше нуля, получено: {needed}")

    filled = 0
    cost = 0.0
    for order in orders:
        take = min(int(order["volume_remain"]), needed - filled)
        if take <= 0:
            continue
        cost += take * float(order["price"])
        filled += take
        if filled >= needed:
            break

    if filled == 0:
        return None, 0
    return cost / filled, filled


def quote(orders: list[dict], hub: Hub, side: OrderSide, needed: int) -> Quote:
    """Полный разбор одной стороны стакана: цена, набранный объём, глубина.

    available считается по всей пригодной книге, а не только по пройденной
    части: именно это число сравнивается с потребностью при пересчёте, когда
    пользователь поменяет N уже после подтяжки.
    """
    if needed <= 0:
        raise ValueError(f"Нужное количество должно быть больше нуля, получено: {needed}")

    selected = (
        [o for o in selected_buys(orders, hub) if int(o.get("min_volume", 1)) <= needed]
        if side is OrderSide.BUY
        else selected_sells(orders, hub)
    )
    book = drop_outliers(selected)
    price, filled = vwap(book, needed)
    available = sum(int(o["volume_remain"]) for o in book)
    return Quote(
        price=price,
        filled=filled,
        available=available,
        needed=needed,
        dropped=len(selected) - len(book),
    )


def quote_from_ladder(
    levels: Sequence[tuple[Decimal, int, int]], side: OrderSide, needed: int
) -> Quote:
    """Разбор сохранённой лестницы под фактический объём (ROADMAP, этапы 8 и 11).

    Обратная сторона ladder_from_orders. Сборщик сложил в базу то, что от
    объёма не зависит: отбор по станции и радиусу и сортировку. Здесь
    доделывается всё остальное.

    Порядок важен. Сначала отсечение выбросов, потом min_volume: медиана должна
    описывать рынок целиком, а min_volume — это про нашу исполнимость, и сужать
    им книгу до расчёта медианы значит считать опору по огрызку.

    Для buy отсекаются уровни с min_volume больше нужного количества: такой
    ордер физически не исполнить (ESI §4). Для sell min_volume не смотрим —
    в правилах разбора он относится только к buy.

    Цена возвращается float: ядро расчёта считает во float по правилу проекта,
    а Decimal нужен был, чтобы копейки пережили хранение.
    """
    if needed <= 0:
        raise ValueError(f"Нужное количество должно быть больше нуля, получено: {needed}")

    clean = drop_outlier_levels(levels)
    usable = [
        (price, volume)
        for price, volume, min_volume in clean
        if side is not OrderSide.BUY or min_volume <= needed
    ]

    filled = 0
    cost = 0.0
    for price, volume in usable:
        take = min(volume, needed - filled)
        if take <= 0:
            continue
        cost += take * float(price)
        filled += take
        if filled >= needed:
            break

    available = sum(volume for _price, volume in usable)
    return Quote(
        price=None if filled == 0 else cost / filled,
        filled=filled,
        available=available,
        needed=needed,
        dropped=len(levels) - len(clean),
    )
