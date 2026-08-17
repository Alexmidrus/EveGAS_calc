"""Разбор стакана: фильтрация, отсечение выбросов, VWAP.

Модуль ничего не знает про HTTP. На вход — список словарей ровно в том виде,
в каком их отдаёт ESI, на выход — цифры. Поэтому он целиком покрывается
тестами на фикстурах, без единого сетевого запроса.

Все правила — docs/ESI.md §3 и §4. Самое тонкое место проекта: ошибка здесь
не видна глазом, она просто даёт неправильный совет.
"""

from dataclasses import dataclass
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
    """

    price: float | None
    filled: int
    available: int
    needed: int

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


def drop_outliers(orders: list[dict]) -> list[dict]:
    """Выбрасывает ордера, чья цена отличается от медианы книги больше чем в 100 раз.

    Отсечение по медиане, а не по среднему: среднее сам выброс и утаскивает.
    На книге из одного ордера сравнивать не с чем — возвращаем как есть.
    """
    if len(orders) < 2:
        return list(orders)
    med = median(float(o["price"]) for o in orders)
    if med <= 0:
        return list(orders)
    high = med * ORDERBOOK_OUTLIER_FACTOR
    low = med / ORDERBOOK_OUTLIER_FACTOR
    return [o for o in orders if low <= float(o["price"]) <= high]


def sell_orders(orders: list[dict], hub: Hub) -> list[dict]:
    """Sell-ордера целевого хаба, по возрастанию цены (ESI §3.1).

    Берём только ордера самой станции: поле range для sell смысла не имеет,
    а ордера в других станциях того же региона — это другой рынок.
    """
    sells = [
        o for o in _alive(orders, is_buy=False)
        if o.get("location_id") == hub.station_id
    ]
    sells = drop_outliers(sells)
    sells.sort(key=lambda o: float(o["price"]))
    return sells


def buy_orders(orders: list[dict], hub: Hub, needed: int) -> list[dict]:
    """Buy-ордера, конкурирующие в хабе, по убыванию цены (ESI §3.2).

    Берём все региональные ордера плюс любые ордера из системы хаба. Ордер,
    выставленный в соседней системе с радиусом region, реально конкурирует
    с тобой в Jita 4-4, и отфильтровать его по location_id значит занизить
    top buy и посоветовать выставиться ниже рынка.

    Известная слепая зона (осознанный компромисс, ESI §3.2): ордера с числовым
    радиусом (5, 10, 20...) из соседних систем не видны — для их учёта нужна
    карта прыжков из SDE, а SDE в рантайм мы намеренно не тянем.

    needed отсекает ордера, чей min_volume больше нужного количества: такой
    ордер физически не исполнить (ESI §4).
    """
    system_id = hub_system_id(orders, hub)
    buys = [
        o for o in _alive(orders, is_buy=True)
        if (
            o.get("range") == RANGE_REGION
            or o.get("location_id") == hub.station_id
            or (system_id is not None and o.get("system_id") == system_id)
        )
        and int(o.get("min_volume", 1)) <= needed
    ]
    buys = drop_outliers(buys)
    buys.sort(key=lambda o: float(o["price"]), reverse=True)
    return buys


def ladder_from_orders(
    orders: list[dict], hub: Hub, side: OrderSide
) -> list[tuple[str, int, int]]:
    """Очищенная лестница стакана для хранения в базе (ROADMAP, этап 7).

    Возвращает уровни (цена строкой, доступный объём, min_volume), уже
    отфильтрованные и отсортированные под свою сторону.

    Что сделано здесь и не переделывается при чтении: отбор по станции и
    радиусу, выброс мёртвых ордеров и выбросов по медиане, сортировка.
    Всё это от объёма пользователя не зависит.

    Что **не** сделано здесь: отсечение buy-ордеров по `min_volume` и сам VWAP.
    И то, и другое зависит от нужного количества, а сборщик его не знает —
    поэтому min_volume едет в базу третьим числом каждого уровня.

    Цена — строкой: во float 2750.10 хранится с хвостом, а нам эти копейки
    ещё считать.
    """
    if side is OrderSide.BUY:
        # needed=0 отключил бы фильтр по min_volume целиком, поэтому берём
        # сам фильтр отдельно: здесь нужен только отбор по радиусу и очистка
        system_id = hub_system_id(orders, hub)
        book = [
            o
            for o in _alive(orders, is_buy=True)
            if (
                o.get("range") == RANGE_REGION
                or o.get("location_id") == hub.station_id
                or (system_id is not None and o.get("system_id") == system_id)
            )
        ]
        book = drop_outliers(book)
        book.sort(key=lambda o: float(o["price"]), reverse=True)
    else:
        book = sell_orders(orders, hub)

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

    book = (
        buy_orders(orders, hub, needed)
        if side is OrderSide.BUY
        else sell_orders(orders, hub)
    )
    price, filled = vwap(book, needed)
    available = sum(int(o["volume_remain"]) for o in book)
    return Quote(price=price, filled=filled, available=available, needed=needed)
