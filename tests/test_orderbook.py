"""Тесты разбора стакана. Все правила — docs/ESI.md §3 и §4.

Сети здесь нет вообще: на вход идут фикстуры из tests/fixtures.

О фикстурах: реальные ID в них — только station_id и system_id самих хабов
(из docs/DOMAIN.md §3 и примера ответа в docs/ESI.md §2). Всё «чужое» —
соседние станции и системы — заведомо синтетические номера 999xxxxx / 399xxxxx,
чтобы фикстура не выдавала себя за справочник игровых ID.
"""

import json
from pathlib import Path

import pytest

from app.core import catalog
from app.core.models import Hub, OrderSide
from app.services import orderbook

FIXTURES = Path(__file__).parent / "fixtures"

JITA = catalog.hub_by_key("jita")
AMARR = catalog.hub_by_key("amarr")


def load(name: str) -> list[dict]:
    """Читает фикстуру ордеров."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def jita_orders() -> list[dict]:
    return load("orders_jita_compressed.json")


@pytest.fixture
def amarr_orders() -> list[dict]:
    return load("orders_amarr_compressed.json")


# --- Отбор sell-ордеров (ESI §3.1) ---


def test_sell_takes_only_hub_station(jita_orders):
    """Sell-ордер из другой станции того же региона не попадает в выборку."""
    book = orderbook.sell_orders(jita_orders, JITA)
    assert all(o["location_id"] == JITA.station_id for o in book)
    assert 1200.0 not in [o["price"] for o in book]  # ордер чужой станции


def test_sell_sorted_ascending(jita_orders):
    book = orderbook.sell_orders(jita_orders, JITA)
    prices = [o["price"] for o in book]
    assert prices == sorted(prices)
    assert prices == [2750.0, 2810.5, 2900.0]


def test_sell_drops_zero_volume():
    orders = [
        {"is_buy_order": False, "location_id": JITA.station_id, "system_id": JITA.system_id,
         "price": 2700.0, "volume_remain": 0, "min_volume": 1, "range": "region"},
        {"is_buy_order": False, "location_id": JITA.station_id, "system_id": JITA.system_id,
         "price": 2800.0, "volume_remain": 10, "min_volume": 1, "range": "region"},
    ]
    book = orderbook.sell_orders(orders, JITA)
    assert [o["price"] for o in book] == [2800.0]


# --- Отбор buy-ордеров (ESI §3.2) — самое тонкое место ---


def test_buy_keeps_region_range_from_another_system(jita_orders):
    """Ордер с range=region из чужой системы конкурирует в хабе и должен попасть."""
    book = orderbook.buy_orders(jita_orders, JITA, needed=50_000)
    assert 2455.0 in [o["price"] for o in book]


def test_buy_keeps_station_orders(jita_orders):
    book = orderbook.buy_orders(jita_orders, JITA, needed=50_000)
    assert 2400.0 in [o["price"] for o in book]


def test_buy_skips_numeric_range_from_another_system(jita_orders):
    """Известная слепая зона: числовой радиус из соседней системы мы не видим."""
    book = orderbook.buy_orders(jita_orders, JITA, needed=50_000)
    assert 2600.0 not in [o["price"] for o in book]


def test_buy_respects_min_volume(jita_orders):
    """Ордер с min_volume больше нужного количества исполнить нельзя."""
    small = orderbook.buy_orders(jita_orders, JITA, needed=50_000)
    assert 2500.0 not in [o["price"] for o in small]

    large = orderbook.buy_orders(jita_orders, JITA, needed=60_000)
    assert 2500.0 in [o["price"] for o in large]


def test_buy_sorted_descending(jita_orders):
    book = orderbook.buy_orders(jita_orders, JITA, needed=50_000)
    prices = [o["price"] for o in book]
    assert prices == sorted(prices, reverse=True)
    assert prices == [2455.0, 2400.0]


def test_buy_keeps_solarsystem_range_in_hub_system(amarr_orders):
    """Ордер из системы хаба берётся независимо от радиуса."""
    book = orderbook.buy_orders(amarr_orders, AMARR, needed=10_000)
    assert 2075.0 in [o["price"] for o in book]


# --- system_id хаба ---


def test_hub_system_id_from_catalog(jita_orders):
    """У Jita system_id известен из docs/ESI.md §2 — стакан не нужен."""
    assert orderbook.hub_system_id(jita_orders, JITA) == 30000142


def test_hub_system_id_inferred_from_book(amarr_orders):
    """У остальных хабов system_id берётся из ордера на станции хаба."""
    assert AMARR.system_id is None  # выдумывать его нельзя
    assert orderbook.hub_system_id(amarr_orders, AMARR) == 39908494


def test_hub_system_id_unknown_without_station_order():
    """Ордера на станции хаба нет — определить систему неоткуда, и это не ошибка."""
    hub = Hub(key="x", name="X", region_id=1, station_id=42, system_id=None)
    orders = [{"is_buy_order": True, "location_id": 7, "system_id": 8,
               "price": 10.0, "volume_remain": 5, "min_volume": 1, "range": "region"}]
    assert orderbook.hub_system_id(orders, hub) is None
    # региональные ордера при этом видны по-прежнему
    assert len(orderbook.buy_orders(orders, hub, needed=5)) == 1


# --- Отсечение выбросов (ESI §4) ---


def test_outlier_dropped_by_median(jita_orders):
    """Цена 0.01 при медиане около 2780 отбрасывается."""
    book = orderbook.sell_orders(jita_orders, JITA)
    assert 0.01 not in [o["price"] for o in book]


def test_outlier_wall_dropped():
    """«Стена» по конской цене тоже уходит: отсечение симметричное."""
    orders = [
        {"price": 3000.0, "volume_remain": 10},
        {"price": 3100.0, "volume_remain": 10},
        {"price": 3200.0, "volume_remain": 10},
        {"price": 900_000.0, "volume_remain": 10},
    ]
    kept = [o["price"] for o in orderbook.drop_outliers(orders)]
    assert kept == [3000.0, 3100.0, 3200.0]


def test_outlier_needs_two_orders():
    """Книгу из одного ордера сравнивать не с чем — возвращаем как есть."""
    orders = [{"price": 0.01, "volume_remain": 10}]
    assert orderbook.drop_outliers(orders) == orders


def test_outlier_median_not_mean():
    """Медиана не утаскивается выбросом, среднее — утащилось бы.

    Среднее этой книги около 250 000, и по нему цена 3000 выглядела бы
    выбросом вниз. По медиане (3000) выбросом оказывается сам миллион.
    """
    orders = [
        {"price": 3000.0, "volume_remain": 10},
        {"price": 3000.0, "volume_remain": 10},
        {"price": 1_000_000.0, "volume_remain": 10},
    ]
    kept = [o["price"] for o in orderbook.drop_outliers(orders)]
    assert kept == [3000.0, 3000.0]


# --- VWAP (ESI §4) ---


def test_vwap_walks_the_book(jita_orders):
    """Не лучшая цена, а средневзвешенная по нужному объёму."""
    book = orderbook.sell_orders(jita_orders, JITA)
    price, filled = orderbook.vwap(book, needed=50_000)
    # 12 000 * 2750 + 30 000 * 2810.5 + 8 000 * 2900 = 140 515 000
    assert filled == 50_000
    assert price == pytest.approx(140_515_000 / 50_000)
    assert price != 2750.0  # именно не лучшая цена


def test_vwap_single_order_is_its_price():
    orders = [{"price": 100.0, "volume_remain": 5000}]
    price, filled = orderbook.vwap(orders, needed=100)
    assert (price, filled) == (100.0, 100)


def test_vwap_shallow_book_counts_what_there_is():
    """Глубины не хватило — цена считается по набранному, а не обнуляется."""
    orders = [
        {"price": 100.0, "volume_remain": 10},
        {"price": 200.0, "volume_remain": 10},
    ]
    price, filled = orderbook.vwap(orders, needed=100)
    assert filled == 20
    assert price == pytest.approx(150.0)


def test_vwap_empty_book():
    assert orderbook.vwap([], needed=100) == (None, 0)


def test_vwap_rejects_nonpositive_needed():
    with pytest.raises(ValueError):
        orderbook.vwap([], needed=0)


# --- quote: цена + глубина ---


def test_quote_sell_full_depth(jita_orders):
    q = orderbook.quote(jita_orders, JITA, OrderSide.SELL, needed=50_000)
    assert q.filled == 50_000
    assert q.available == 142_000  # 12 000 + 30 000 + 100 000
    assert not q.shallow
    assert q.price == pytest.approx(2810.3)


def test_quote_buy_is_shallow(jita_orders):
    """Buy-книга Jita в фикстуре мельче потребности — флаг обязан подняться."""
    q = orderbook.quote(jita_orders, JITA, OrderSide.BUY, needed=50_000)
    assert q.available == 35_000  # 15 000 + 20 000
    assert q.filled == 35_000
    assert q.shallow
    # 15 000 * 2455 + 20 000 * 2400 = 84 825 000
    assert q.price == pytest.approx(84_825_000 / 35_000)


def test_quote_empty_side_has_no_price():
    q = orderbook.quote([], JITA, OrderSide.SELL, needed=10)
    assert q.price is None
    assert q.available == 0
    assert q.shallow


def test_quote_available_counts_whole_book_not_just_walked(jita_orders):
    """available — вся пригодная книга: по нему сверяется потребность при пересчёте."""
    q = orderbook.quote(jita_orders, JITA, OrderSide.SELL, needed=100)
    assert q.filled == 100
    assert q.available == 142_000
