"""Чтение цен из базы и сетка на странице (ROADMAP, этап 8).

Сети здесь нет и быть не может: приложение к ESI больше не ходит вообще.
База — SQLite в памяти, срезы кладутся руками.
"""

import re
from datetime import timedelta
from decimal import Decimal

import pytest

from app import create_app
from app.core import catalog
from app.core.models import GasForm, OrderSide
from app.db import (
    Base,
    CollectionRun,
    MarketHistory,
    MarketSnapshot,
    dump_ladder,
    session_scope,
    utcnow,
)
from app.services.orderbook import quote_from_ladder
from app.services.prices import load_price_book

C320 = catalog.gas_by_key("fullerite_c320")
HUBS = catalog.hubs()
TYPE_IDS = {GasForm.RAW: C320.raw_type_id, GasForm.COMPRESSED: C320.compressed_type_id}
NEEDED = {GasForm.RAW: 50_000, GasForm.COMPRESSED: 56_180}

CONTROL_FORM = {
    "gas": "fullerite_c320",
    "n_units": "50000",
    "structure": "athanor",
    "gde_level": "5",
    "broker_fee": "1.5",
    "collateral_pct": "0.5",
}


@pytest.fixture
def app():
    application = create_app(
        {
            "APP_ENV": "dev",
            "DATABASE_URL": "sqlite:///:memory:",
            "PRICE_MAX_AGE_MINUTES": 90,
            "SECRET_KEY": "тестовый ключ, длины хватает с запасом",
            "TESTING": True,
        }
    )
    Base.metadata.create_all(application.extensions["db_engine"])
    return application


@pytest.fixture
def engine(app):
    return app.extensions["db_engine"]


@pytest.fixture
def client(app):
    return app.test_client()


def add_snapshot(engine, hub_key, form, side, levels, *, age_minutes=0):
    with session_scope(engine) as session:
        session.add(
            MarketSnapshot(
                hub_key=hub_key,
                type_id=TYPE_IDS[form],
                side=side.value,
                collected_at=utcnow() - timedelta(minutes=age_minutes),
                ladder=dump_ladder(levels),
                total_volume=sum(v for _p, v, _m in levels),
                order_count=len(levels),
            )
        )


def fill_all(engine, *, age_minutes=0, price="2750.00"):
    """Полная сетка: все хабы, обе формы, обе стороны."""
    for hub in HUBS:
        for form in (GasForm.RAW, GasForm.COMPRESSED):
            for side in OrderSide:
                add_snapshot(
                    engine, hub.key, form, side,
                    [(price, 1_000_000, 1)], age_minutes=age_minutes,
                )


def add_history(engine, hub_key, form, *, average=3000.0, days=5, volume=100_000, spread=0.02):
    """История реальных сделок по региону хаба (ESI §5)."""
    region_id = {h.key: h.region_id for h in HUBS}[hub_key]
    today = utcnow().date()
    with session_scope(engine) as session:
        for i in range(days):
            session.add(
                MarketHistory(
                    region_id=region_id,
                    type_id=TYPE_IDS[form],
                    date=today - timedelta(days=i + 1),
                    average=Decimal(str(round(average, 2))),
                    highest=Decimal(str(round(average * (1 + spread), 2))),
                    lowest=Decimal(str(round(average * (1 - spread), 2))),
                    volume=volume,
                    order_count=10,
                )
            )


class TestHistoryBand:
    """Коридор по истории при чтении из базы (ESI §5.4, этап 11.4)."""

    def test_garbage_book_gives_no_price(self, engine):
        """Тот самый Rens: единственный buy на 1 ISK при реальной цене 6000.

        Правило «×100 от медианы книги» на книге из одного уровня не работает
        вовсе — сравнивать не с чем. Внешняя опора закрывает и это.
        """
        add_snapshot(engine, "rens", GasForm.RAW, OrderSide.BUY, [("1.00", 85_481, 1)])
        add_history(engine, "rens", GasForm.RAW, average=6000.0)

        book = load_price_book(engine, HUBS, TYPE_IDS, NEEDED)
        quote = book.get("rens", GasForm.RAW, OrderSide.BUY)
        assert quote.price is None
        assert quote.no_liquid_orders is True
        assert quote.dropped == 1

    def test_real_order_survives_garbage_majority(self, engine):
        """Мусора больше половины: медиана книги выбросила бы настоящий ордер."""
        levels = [("6000.00", 5_000, 1), ("1.00", 1_000, 1), ("1.00", 1_000, 1), ("1.00", 1_000, 1)]
        add_snapshot(engine, "jita", GasForm.RAW, OrderSide.BUY, levels)
        add_history(engine, "jita", GasForm.RAW, average=6000.0)

        book = load_price_book(engine, HUBS, TYPE_IDS, NEEDED)
        quote = book.get("jita", GasForm.RAW, OrderSide.BUY)
        assert quote.price == pytest.approx(6000.0)
        assert quote.dropped == 3

    def test_without_history_old_rule_applies(self, engine):
        """Нет истории — поведение ровно как раньше, и это не ошибка."""
        levels = [("2750.00", 10_000, 1), ("0.01", 10_000, 1)]
        add_snapshot(engine, "jita", GasForm.RAW, OrderSide.BUY, levels)

        book = load_price_book(engine, HUBS, TYPE_IDS, NEEDED)
        quote = book.get("jita", GasForm.RAW, OrderSide.BUY)
        assert quote.price == pytest.approx(2750.0)  # 0.01 убрало правило §4
        assert quote.history.usable is False

    def test_history_reaches_the_quote(self, engine):
        """Свод из той же опоры, по которой считали цену, — для колонок таблицы."""
        add_snapshot(engine, "jita", GasForm.RAW, OrderSide.SELL, [("3000.00", 10_000, 1)])
        add_history(engine, "jita", GasForm.RAW, average=3000.0, volume=70_000)

        quote = load_price_book(engine, HUBS, TYPE_IDS, NEEDED).get(
            "jita", GasForm.RAW, OrderSide.SELL
        )
        assert quote.history.reference == pytest.approx(3000.0)
        assert quote.history.daily_volume == pytest.approx(50_000)
        assert quote.history.short_of_volume(NEEDED[GasForm.RAW]) is False

    def test_borrowed_reference_from_other_hubs(self, engine):
        """Своей истории нет — опора берётся у соседей.

        Найдено проверкой в браузере 19.08.2026: по сжатому Fullerite-C84
        в Rens сделок за неделю не было, коридор не применялся, и buy-ордер
        на 33 ISK при реальной цене около 9000 вышел на первое место таблицы
        как «лучший вариант». Тестами это не ловилось.
        """
        add_snapshot(engine, "rens", GasForm.COMPRESSED, OrderSide.BUY, [("33.02", 50_000, 1)])
        for hub in ("jita", "amarr", "dodixie"):
            add_history(engine, hub, GasForm.COMPRESSED, average=9900.0)

        quote = load_price_book(engine, HUBS, TYPE_IDS, NEEDED).get(
            "rens", GasForm.COMPRESSED, OrderSide.BUY
        )
        assert quote.price is None
        assert quote.no_liquid_orders is True
        assert quote.history.borrowed is True

    def test_single_neighbour_is_not_enough(self, engine):
        """Один сосед мог сам оказаться перекошенным — подпирать им нечего."""
        add_snapshot(engine, "rens", GasForm.COMPRESSED, OrderSide.BUY, [("33.02", 50_000, 1)])
        add_history(engine, "jita", GasForm.COMPRESSED, average=9900.0)

        quote = load_price_book(engine, HUBS, TYPE_IDS, NEEDED).get(
            "rens", GasForm.COMPRESSED, OrderSide.BUY
        )
        assert quote.history.usable is False
        assert quote.price == pytest.approx(33.02)  # прежнее правило §4 бессильно

    def test_borrowed_volume_is_not_shown_as_own(self, engine):
        """Оборот чужого региона за свой не выдаём: в колонке «Продано» прочерк."""
        add_snapshot(engine, "rens", GasForm.COMPRESSED, OrderSide.BUY, [("9000.00", 50_000, 1)])
        for hub in ("jita", "amarr", "dodixie"):
            add_history(engine, hub, GasForm.COMPRESSED, average=9900.0, volume=10)

        stats = load_price_book(engine, HUBS, TYPE_IDS, NEEDED).get(
            "rens", GasForm.COMPRESSED, OrderSide.BUY
        ).history
        assert stats.borrowed is True
        assert stats.short_of_volume(NEEDED[GasForm.COMPRESSED]) is False
        assert stats.slow_for_volume(NEEDED[GasForm.COMPRESSED]) is False

    def test_high_side_is_generous(self, engine):
        """Сверху коридор широкий: честный sell в 5 опор — не мусор (замер 11.3)."""
        add_snapshot(engine, "hek", GasForm.RAW, OrderSide.SELL, [("15000.00", 10_000, 1)])
        add_history(engine, "hek", GasForm.RAW, average=3000.0)

        quote = load_price_book(engine, HUBS, TYPE_IDS, NEEDED).get(
            "hek", GasForm.RAW, OrderSide.SELL
        )
        assert quote.price == pytest.approx(15_000.0)
        assert quote.dropped == 0


class TestQuoteFromLadder:
    """Обратная сторона того, что сложил сборщик."""

    def test_vwap_over_levels(self):
        levels = [(Decimal("2750.00"), 12_000, 1), (Decimal("2760.00"), 50_000, 1)]
        quote = quote_from_ladder(levels, OrderSide.SELL, 20_000)
        # 12 000 юнитов по 2750 и ещё 8 000 по 2760
        assert quote.price == pytest.approx((12_000 * 2750 + 8_000 * 2760) / 20_000)
        assert quote.filled == 20_000

    def test_depth_shortfall_is_visible(self):
        quote = quote_from_ladder([(Decimal("2750.00"), 100, 1)], OrderSide.SELL, 50_000)
        assert quote.shallow
        assert quote.filled == 100
        assert quote.available == 100

    def test_buy_min_volume_filter(self):
        """Ордер с min_volume больше нужного исполнить нельзя (ESI §4).
        Ради этого min_volume и хранится третьим числом уровня."""
        levels = [(Decimal("2400.00"), 5_000, 100_000), (Decimal("2300.00"), 5_000, 1)]
        quote = quote_from_ladder(levels, OrderSide.BUY, 1_000)
        assert quote.price == pytest.approx(2300.0)  # первый уровень отсечён

    def test_sell_ignores_min_volume(self):
        """Для sell правило про min_volume не действует."""
        levels = [(Decimal("2750.00"), 5_000, 100_000)]
        assert quote_from_ladder(levels, OrderSide.SELL, 1_000).price == pytest.approx(2750.0)

    def test_empty_ladder(self):
        quote = quote_from_ladder([], OrderSide.SELL, 1_000)
        assert quote.price is None
        assert quote.available == 0

    def test_zero_needed_rejected(self):
        with pytest.raises(ValueError):
            quote_from_ladder([], OrderSide.SELL, 0)


class TestPriceBook:
    """Что база отдаёт приложению."""

    def test_empty_database(self, engine):
        book = load_price_book(engine, HUBS, TYPE_IDS, NEEDED)
        assert book.empty
        assert book.age() is None
        assert len(book.missing_hubs) == len(HUBS)

    def test_reads_all_hubs(self, engine):
        fill_all(engine)
        book = load_price_book(engine, HUBS, TYPE_IDS, NEEDED)
        assert len(book.quotes) == len(HUBS) * 4
        assert book.missing_hubs == ()

    def test_takes_newest_snapshot(self, engine):
        add_snapshot(engine, "jita", GasForm.COMPRESSED, OrderSide.SELL,
                     [("3000.00", 100_000, 1)], age_minutes=60)
        add_snapshot(engine, "jita", GasForm.COMPRESSED, OrderSide.SELL,
                     [("2750.00", 100_000, 1)], age_minutes=1)
        book = load_price_book(engine, HUBS, TYPE_IDS, NEEDED)
        assert book.get("jita", GasForm.COMPRESSED, OrderSide.SELL).price == pytest.approx(2750.0)

    def test_missing_hub_is_named(self, engine):
        add_snapshot(engine, "jita", GasForm.COMPRESSED, OrderSide.SELL, [("2750.00", 10**6, 1)])
        book = load_price_book(engine, HUBS, TYPE_IDS, NEEDED)
        assert "hek" in book.missing_hubs
        assert "jita" not in book.missing_hubs

    def test_empty_book_is_not_zero(self, engine):
        """Пустая книга — это данные: газ в хабе просто не продают.
        Ячейка обязана остаться пустой, а не показать ноль."""
        add_snapshot(engine, "hek", GasForm.COMPRESSED, OrderSide.SELL, [])
        book = load_price_book(engine, HUBS, TYPE_IDS, NEEDED)
        assert book.get("hek", GasForm.COMPRESSED, OrderSide.SELL) is None
        assert "hek" not in book.missing_hubs  # данные есть, просто ордеров нет

    def test_price_depends_on_needed(self, engine):
        """Средневзвешенная считается под фактический объём — в этом весь смысл
        хранения лестницы вместо готовой цены."""
        add_snapshot(engine, "jita", GasForm.COMPRESSED, OrderSide.SELL,
                     [("2750.00", 10_000, 1), ("3500.00", 10_000_000, 1)])
        small = load_price_book(engine, HUBS, TYPE_IDS, {GasForm.RAW: 1, GasForm.COMPRESSED: 10_000})
        big = load_price_book(engine, HUBS, TYPE_IDS, {GasForm.RAW: 1, GasForm.COMPRESSED: 1_000_000})
        assert small.get("jita", GasForm.COMPRESSED, OrderSide.SELL).price == pytest.approx(2750.0)
        assert big.get("jita", GasForm.COMPRESSED, OrderSide.SELL).price > 3400

    def test_staleness(self, engine):
        fill_all(engine, age_minutes=200)
        book = load_price_book(engine, HUBS, TYPE_IDS, NEEDED)
        assert book.is_stale(timedelta(minutes=90))
        assert not book.is_stale(timedelta(minutes=300))

    def test_broken_database_is_reported(self, app):
        """Недоступная база не должна ронять расчёт по ручным ценам."""
        Base.metadata.drop_all(app.extensions["db_engine"])
        book = load_price_book(app.extensions["db_engine"], HUBS, TYPE_IDS, NEEDED)
        assert book.error is not None
        assert book.empty


class TestPage:
    """Что видно на экране."""

    def test_esi_button_is_gone(self, client):
        """Пользователь больше не может инициировать обращение к ESI."""
        html = client.get("/").get_data(as_text=True)
        assert "Подтянуть цены" not in html
        assert "/fetch-prices" not in html

    def test_fetch_route_removed(self, client):
        assert client.post("/fetch-prices", data=CONTROL_FORM).status_code == 404

    def test_empty_database_explains_itself(self, client):
        html = client.get("/").get_data(as_text=True)
        assert "Цены ещё не собраны" in html

    def test_prices_appear_in_grid(self, client, engine):
        fill_all(engine, price="2750.00")
        html = client.get("/").get_data(as_text=True)
        assert "2 750" in html
        assert "Цены из базы, собраны" in html

    def test_age_is_shown(self, client, engine):
        fill_all(engine, age_minutes=42)
        assert "42 мин назад" in client.get("/").get_data(as_text=True)

    def test_stale_data_is_flagged(self, client, engine):
        fill_all(engine, age_minutes=500)
        html = client.get("/").get_data(as_text=True)
        assert "Данные устарели" in html
        assert "cron" in html

    def test_grid_has_its_own_target(self, client):
        """Регрессия. Форма задаёт hx-target="#results", и потомки его наследуют:
        без своего target ответ с сеткой уезжал в блок результата, а сетка
        молча оставалась со старыми ценами."""
        html = client.get("/").get_data(as_text=True)
        grid = html[html.index('id="price-grid"') : html.index('id="price-grid"') + 400]
        assert "hx-target=" in grid

    def test_form_listens_for_recalc(self, client):
        """После перерисовки сетки результат обязан пересчитаться: иначе на
        экране останутся цифры по прежним ценам."""
        html = client.get("/").get_data(as_text=True)
        assert "recalc" in html

    def test_missing_hub_is_named_not_zeroed(self, client, engine):
        for form in (GasForm.RAW, GasForm.COMPRESSED):
            for side in OrderSide:
                add_snapshot(engine, "jita", form, side, [("2750.00", 10**6, 1)])
        html = client.get("/").get_data(as_text=True)
        assert "Данных нет по хабам" in html
        assert "Hek" in html


def cell_value(html: str, name: str) -> str:
    """Значение ценовой ячейки по имени поля.

    Искать подстроку value="..." по всему ответу нельзя: то же значение
    встречается в скрытых полях auto и depth соседних ячеек.
    """
    match = re.search(
        r'name="' + re.escape(name) + r'"\s+value="([^"]*)"', html
    )
    assert match is not None, f"в ответе нет ячейки {name}"
    return match.group(1)


class TestGridRefresh:
    """POST /price-grid — пересчёт сетки под новую потребность."""

    def test_recomputes_for_new_amount(self, client, engine):
        add_snapshot(engine, "jita", GasForm.COMPRESSED, OrderSide.SELL,
                     [("2750.00", 10_000, 1), ("9000.00", 10_000_000, 1)])
        small = client.post("/price-grid", data=dict(CONTROL_FORM, n_units="1000"))
        big = client.post("/price-grid", data=dict(CONTROL_FORM, n_units="500000"))
        assert "2 750" in small.get_data(as_text=True)
        assert "2 750" not in big.get_data(as_text=True)

    def test_manual_value_survives_refresh(self, client, engine):
        """Ручной ввод главнее: ячейку без пометки auto сервер не трогает."""
        fill_all(engine)
        form = dict(CONTROL_FORM, jita_compressed_sell="1234", n_units="60000")
        html = client.post("/price-grid", data=form).get_data(as_text=True)
        assert cell_value(html, "jita_compressed_sell") == "1234"

    def test_auto_value_is_replaced(self, client, engine):
        """А помеченную auto — обязан пересчитать."""
        fill_all(engine, price="2750.00")
        form = dict(
            CONTROL_FORM,
            jita_compressed_sell="1",
            jita_compressed_sell_auto="1",
        )
        html = client.post("/price-grid", data=form).get_data(as_text=True)
        assert cell_value(html, "jita_compressed_sell") == "2 750"

    def test_freight_rates_survive(self, client, engine):
        fill_all(engine)
        html = client.post(
            "/price-grid", data=dict(CONTROL_FORM, jita_rate="500")
        ).get_data(as_text=True)
        assert cell_value(html, "jita_rate") == "500"

    def test_broken_form_does_not_wipe_grid(self, client, engine):
        fill_all(engine)
        form = dict(CONTROL_FORM, n_units="сколько-нибудь", jita_rate="500")
        html = client.post("/price-grid", data=form).get_data(as_text=True)
        assert cell_value(html, "jita_rate") == "500"


class TestCalculateUsesStoredPrices:
    """Расчёт по ценам из базы: цифры доезжают до таблицы результата."""

    def test_result_from_database_prices(self, client, engine):
        fill_all(engine, price="2750.00")
        form = dict(CONTROL_FORM)
        for hub in HUBS:
            form[f"{hub.key}_rate"] = "500"
            for suffix in ("raw_sell", "raw_buy", "compressed_sell", "compressed_buy"):
                form[f"{hub.key}_{suffix}"] = "2750"
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "ISK/юнит" in html

    def test_depth_travels_to_the_form(self, client, engine):
        """Предупреждение о глубине обязано работать и поверх данных из базы:
        глубина едет скрытым полем ровно как раньше."""
        add_snapshot(engine, "jita", GasForm.COMPRESSED, OrderSide.SELL, [("2750.00", 100, 1)])
        html = client.get("/").get_data(as_text=True)
        assert cell_value(html, "jita_compressed_sell_depth") == "100"


class TestHistoryInResults:
    """История в таблице результата (SPEC §5.2 и §6, этап 11.5)."""

    def form_with_price(self, price: str) -> dict:
        form = dict(CONTROL_FORM)
        form["jita_rate"] = "500"
        form["jita_raw_sell"] = price
        return form

    def test_columns_show_real_trades(self, client, engine):
        add_history(engine, "jita", GasForm.RAW, average=3000.0, volume=700_000)
        html = client.post("/calculate", data=self.form_with_price("3000")).get_data(as_text=True)
        # Колонка суточного оборота с версии 0.3.0 называется «Ликвидность»,
        # а само число стоит в ней подписью «N/сут» (ROADMAP 12.5)
        assert "Сделки" in html and "Ликвидность" in html and "/сут" in html
        assert "2 940" in html and "3 060" in html  # диапазон сделок ±2%

    def test_illiquid_price_is_flagged(self, client, engine):
        """Цена вне диапазона сделок помечается, но не пересчитывается."""
        add_history(engine, "jita", GasForm.RAW, average=6000.0)
        html = client.post("/calculate", data=self.form_with_price("1")).get_data(as_text=True)
        assert "Неликвидная цена" in html
        # Пометка переехала из подписи под ценой в значок у имени хаба;
        # текст, объясняющий её, живёт в подсказке значка (ROADMAP 12.5)
        assert "Сделок по такой цене не было" in html

    def test_normal_price_is_not_flagged(self, client, engine):
        """Нормальный спред пометки не даёт — иначе она превратится в шум."""
        add_history(engine, "jita", GasForm.RAW, average=3000.0)
        html = client.post("/calculate", data=self.form_with_price("2950")).get_data(as_text=True)
        assert "Неликвидная цена" not in html

    def test_volume_shortage_is_flagged(self, client, engine):
        """50 000 нужно, а за неделю в регионе продали 3 500."""
        add_history(engine, "jita", GasForm.RAW, average=3000.0, days=5, volume=700)
        html = client.post("/calculate", data=self.form_with_price("3000")).get_data(as_text=True)
        assert "столько не набрать" in html

    def test_no_history_no_columns_no_crash(self, client, engine):
        """Без истории таблица работает как раньше: прочерки, ни одной пометки."""
        html = client.post("/calculate", data=self.form_with_price("3000")).get_data(as_text=True)
        assert "ISK/юнит" in html
        assert "Неликвидная цена" not in html
        assert "столько не набрать" not in html


class TestHealthz:
    """Проверка состояния для мониторинга и после развёртывания."""

    def test_reports_profile_and_database(self, client):
        payload = client.get("/healthz").get_json()
        assert payload["profile"] == "dev"
        assert payload["database"] == "ok"

    def test_no_collection_yet(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.get_json()["last_collection"] is None

    def test_fresh_collection(self, client, engine):
        with session_scope(engine) as session:
            session.add(CollectionRun(status="ok", finished_at=utcnow()))
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.get_json()["prices"] == "ok"

    def test_stale_collection_is_not_ok(self, client, engine):
        """Мониторинг обязан узнать, что сбор встал, не дожидаясь жалоб."""
        with session_scope(engine) as session:
            session.add(
                CollectionRun(status="ok", finished_at=utcnow() - timedelta(hours=5))
            )
        response = client.get("/healthz")
        assert response.status_code == 503
        assert response.get_json()["prices"] == "устарели"

    def test_aborted_run_does_not_count(self, client, engine):
        """Прерванный лимитом цикл — не успешный сбор."""
        with session_scope(engine) as session:
            session.add(CollectionRun(status="aborted", finished_at=utcnow()))
        assert client.get("/healthz").get_json()["last_collection"] is None

    def test_history_collection_is_separate(self, client, engine):
        """Сборщики видны раздельно: один встал — это должно быть заметно."""
        with session_scope(engine) as session:
            session.add(CollectionRun(status="ok", kind="orders", finished_at=utcnow()))
            session.add(
                CollectionRun(
                    status="ok", kind="history", finished_at=utcnow() - timedelta(hours=30)
                )
            )
        payload = client.get("/healthz").get_json()
        assert payload["prices"] == "ok"
        assert payload["history_age_hours"] == 30

    def test_missing_history_does_not_break_healthz(self, client, engine):
        """История — вспомогательный сбор: без неё статус не красный."""
        with session_scope(engine) as session:
            session.add(CollectionRun(status="ok", kind="orders", finished_at=utcnow()))
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.get_json()["last_history_collection"] is None

    def test_database_down(self, app, client):
        """Таблиц нет — мониторинг обязан увидеть 503, а не бодрое ok."""
        Base.metadata.drop_all(app.extensions["db_engine"])
        response = client.get("/healthz")
        assert response.status_code == 503
        assert "недоступна" in response.get_json()["database"]
