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
from app.routes import SORT_COLUMNS
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
        """Цены из базы стоят в сетке и помечены как подставленные."""
        fill_all(engine, price="2750.00")
        html = client.get("/").get_data(as_text=True)
        assert "2 750" in html
        assert 'class="fetched" title="Из базы, можно перебить"' in html

    def test_healthy_grid_says_nothing_under_itself(self, client, engine):
        """Всё в порядке — под сеткой пусто. Заметки остались только про
        проблемы: рассказывать, что всё хорошо, незачем (SPEC §4)."""
        fill_all(engine, price="2750.00")
        html = client.get("/").get_data(as_text=True)
        assert "Цены из базы, собраны" not in html
        assert "Отброшено ордеров вне рынка" not in html
        assert "Данные устарели" not in html
        # Возраст цен никуда не делся — он чипом в шапке
        assert "цены " in html[: html.index("</header>")]

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
        # Пометка живёт значком у имени хаба, объясняет её подсказка значка
        # (ROADMAP 12.5). Проверяем именно строку: в легенде под таблицей тот же
        # значок стоит всегда, и по нему одному судить нельзя
        body = html[html.index("<tbody>"):html.index("</tbody>")]
        assert "Сделок по такой цене не было: за неделю" in body

    def test_normal_price_is_not_flagged(self, client, engine):
        """Нормальный спред пометки не даёт — иначе она превратится в шум."""
        add_history(engine, "jita", GasForm.RAW, average=3000.0)
        html = client.post("/calculate", data=self.form_with_price("2950")).get_data(as_text=True)
        body = html[html.index("<tbody>"):html.index("</tbody>")]
        assert "Сделок по такой цене не было" not in body

    def test_volume_shortage_is_flagged(self, client, engine):
        """50 000 нужно, а за неделю в регионе продали 3 500."""
        add_history(engine, "jita", GasForm.RAW, average=3000.0, days=5, volume=700)
        html = client.post("/calculate", data=self.form_with_price("3000")).get_data(as_text=True)
        assert "столько не набрать" in html

    def test_no_history_no_columns_no_crash(self, client, engine):
        """Без истории таблица работает как раньше: прочерки, ни одной пометки."""
        html = client.post("/calculate", data=self.form_with_price("3000")).get_data(as_text=True)
        assert "ISK/юнит" in html
        body = html[html.index("<tbody>"):html.index("</tbody>")]
        assert "Сделок по такой цене не было" not in body
        assert "столько не набрать" not in body


class TestHideIlliquidFilter:
    """Фильтр «скрыть неликвид» (ROADMAP, этап 14).

    Фильтр показа, а не расчёта: признак «сделок по такой цене не было» живёт
    в истории, которой ядро не видит. Отсюда и проверки — через HTTP, на
    отрендеренной таблице.
    """

    def two_hubs(self, **extra) -> dict:
        """Jita с неликвидной ценой и Amarr, по которому истории нет вовсе."""
        form = dict(CONTROL_FORM)
        form.update(
            {
                "jita_rate": "500",
                "jita_raw_sell": "1",       # вне коридора реальных сделок
                "amarr_rate": "700",
                "amarr_raw_sell": "3000",   # истории по Amarr нет
            }
        )
        form.update(extra)
        return form

    def test_off_by_default(self, client, engine):
        """Без галочки не скрывается ничего и счётчика нет."""
        add_history(engine, "jita", GasForm.RAW, average=6000.0)
        html = client.post("/calculate", data=self.two_hubs()).get_data(as_text=True)
        assert "Jita" in html and "Amarr" in html
        assert "Скрыто строк" not in html

    def test_unconfirmed_row_disappears(self, client, engine):
        """Строка, по цене которой за неделю не торговали, уходит из таблицы."""
        add_history(engine, "jita", GasForm.RAW, average=6000.0)
        html = client.post(
            "/calculate", data=self.two_hubs(hide_illiquid="on")
        ).get_data(as_text=True)
        assert "Jita" not in html
        assert "Amarr" in html

    def test_row_without_history_stays(self, client, engine):
        """Нет данных — это «мы не знаем», а не «неликвид»: строка остаётся."""
        add_history(engine, "jita", GasForm.RAW, average=6000.0)
        html = client.post(
            "/calculate", data=self.two_hubs(hide_illiquid="on")
        ).get_data(as_text=True)
        assert "Amarr" in html
        assert "Скрыто строк" in html and "<b>1</b>" in html

    def test_nothing_to_hide_no_counter(self, client, engine):
        """Истории нет ни по одному хабу — фильтр молчит и ничего не убирает."""
        html = client.post(
            "/calculate", data=self.two_hubs(hide_illiquid="on")
        ).get_data(as_text=True)
        assert "Jita" in html and "Amarr" in html
        assert "Скрыто строк" not in html

    def test_low_volume_row_is_not_hidden(self, client, engine):
        """Маленький оборот — другая пометка про другое: фильтр её не трогает."""
        add_history(engine, "jita", GasForm.RAW, average=3000.0, days=5, volume=700)
        form = dict(CONTROL_FORM)
        form.update({"jita_rate": "500", "jita_raw_sell": "3000", "hide_illiquid": "on"})
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "Jita" in html
        assert "столько не набрать" in html
        assert "Скрыто строк" not in html

    def test_summary_follows_the_visible_rows(self, client, engine):
        """«Лучший вариант» — первая строка таблицы, а не скрытая фильтром.

        Без фильтра лучшей была Jita по цене 1 ISK: её и убирает галочка.
        """
        add_history(engine, "jita", GasForm.RAW, average=6000.0)
        plain = client.post("/calculate", data=self.two_hubs()).get_data(as_text=True)
        assert "Jita ·" in plain  # лучший вариант до фильтрации

        html = client.post(
            "/calculate", data=self.two_hubs(hide_illiquid="on")
        ).get_data(as_text=True)
        assert "Amarr ·" in html
        # Δ первой видимой строки — прочерк: считать её теперь не от чего
        assert 'cell-delta--muted">—' in html

    def test_scenario_counter_counts_the_visible(self, client, engine):
        """Числитель карточки «Сценариев в расчёте» — видимые строки.

        Знаменатель не трогается: 20 — это число ячеек сетки цен, а не число
        построенных сценариев.
        """
        add_history(engine, "jita", GasForm.RAW, average=6000.0)
        html = client.post(
            "/calculate", data=self.two_hubs(hide_illiquid="on")
        ).get_data(as_text=True)
        assert 'class="summary__big">1</span>' in html
        assert "из 20" in html

    def test_everything_hidden_is_explained(self, client, engine):
        """Фильтр убрал всё — на экране объяснение, а не «Недостаточно данных»."""
        add_history(engine, "jita", GasForm.RAW, average=6000.0)
        form = dict(CONTROL_FORM)
        form.update({"jita_rate": "500", "jita_raw_sell": "1", "hide_illiquid": "on"})
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "Все строки скрыты фильтром «скрыть неликвид»" in html
        assert "Снимите галочку" in html
        assert "Недостаточно данных" not in html

    def test_no_prices_still_says_not_enough_data(self, client, engine):
        """Пустая сетка при включённом фильтре — по-прежнему «Недостаточно данных»:
        скрывать было нечего, и валить это на галочку нельзя."""
        form = dict(CONTROL_FORM, hide_illiquid="on")
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "Недостаточно данных" in html
        assert "Все строки скрыты" not in html


class TestBestPerHubFilter:
    """Фильтр «по одной строке на хаб» (ROADMAP, этап 15).

    Оставляет у каждого хаба самую дешёвую строку по ISK за полезный юнит —
    той же метрике, по которой отсортирована вся таблица.
    """

    def full_grid(self, **extra) -> dict:
        """Jita со всеми четырьмя ценами и Amarr с двумя: 6 строк на 2 хаба."""
        form = dict(CONTROL_FORM)
        form.update(
            {
                "jita_rate": "500",
                "jita_raw_sell": "3000",
                "jita_raw_buy": "2620",
                "jita_compressed_sell": "2750",
                "jita_compressed_buy": "2400",
                "amarr_rate": "700",
                "amarr_compressed_sell": "2300",
                "amarr_compressed_buy": "2050",
            }
        )
        form.update(extra)
        return form

    def rows(self, html: str) -> list[str]:
        """Текст каждой строки таблицы, в порядке отрисовки.

        Номер в колонке «#» отбрасывается: он позиционный, и после свёртки
        та же самая строка честно едет с третьего места на второе. Сравнивать
        строки по нему значит ловить не содержимое, а место в списке.
        """
        body = html[html.index("<tbody>"):html.index("</tbody>")]
        return [
            " ".join(re.sub(r"<[^>]+>", " ", cells).split()).split(" ", 1)[1]
            for cells in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)
        ]

    def test_off_by_default(self, client):
        """Без галочки таблица полная и счётчика свёрнутого нет."""
        html = client.post("/calculate", data=self.full_grid()).get_data(as_text=True)
        assert len(self.rows(html)) == 6
        assert "Свёрнуто строк" not in html

    def test_one_row_per_hub(self, client):
        """Из четырёх строк Jita остаётся одна, из двух строк Amarr — одна."""
        html = client.post(
            "/calculate", data=self.full_grid(best_per_hub="on")
        ).get_data(as_text=True)
        rows = self.rows(html)
        assert len(rows) == 2
        assert sum("Jita" in row for row in rows) == 1
        assert sum("Amarr" in row for row in rows) == 1

    def test_kept_row_is_the_cheapest_of_the_hub(self, client):
        """Осталась именно самая дешёвая строка хаба, а не первая попавшаяся.

        У Jita дешевле всех сжатый buy: 2400 за юнит сжатого против 2620
        за юнит сырого, и объём вдесятеро меньше — доставка тоже.
        """
        html = client.post(
            "/calculate", data=self.full_grid(best_per_hub="on")
        ).get_data(as_text=True)
        jita = next(row for row in self.rows(html) if "Jita" in row)
        assert "Compressed" in jita and "buy" in jita

    def test_hub_count_survives_the_collapse(self, client):
        """Хабов в выдаче ровно столько, сколько их было до свёртки."""
        plain = self.rows(
            client.post("/calculate", data=self.full_grid()).get_data(as_text=True)
        )
        collapsed = self.rows(
            client.post(
                "/calculate", data=self.full_grid(best_per_hub="on")
            ).get_data(as_text=True)
        )
        hubs = {name for name in ("Jita", "Amarr") if any(name in r for r in plain)}
        assert hubs == {name for name in ("Jita", "Amarr") if any(name in r for r in collapsed)}
        assert len(collapsed) == len(hubs)

    def test_counter_matches_the_difference(self, client):
        """Счётчик свёрнутого — разница длин списков, а не выдуманное число."""
        plain = self.rows(
            client.post("/calculate", data=self.full_grid()).get_data(as_text=True)
        )
        html = client.post(
            "/calculate", data=self.full_grid(best_per_hub="on")
        ).get_data(as_text=True)
        collapsed = self.rows(html)
        assert f"<b>{len(plain) - len(collapsed)}</b>" in html
        assert "Свёрнуто строк" in html

    def test_summary_follows_the_visible_rows(self, client):
        """Сводка сходится с таблицей: лучший вариант — первая строка.

        Лучший в полном примере — Amarr сжатый buy; свёртка его не трогает,
        зато число сценариев в карточке становится равно числу видимых строк.
        """
        html = client.post(
            "/calculate", data=self.full_grid(best_per_hub="on")
        ).get_data(as_text=True)
        assert "Amarr ·" in html
        assert 'class="summary__big">2</span>' in html
        assert 'cell-delta--muted">—' in html

    def test_both_filters_match_the_step_by_step_result(self, client, engine):
        """Обе галочки разом дают то же, что применённые по очереди.

        Порядок зафиксирован: сначала «скрыть неликвид», потом свёртка. Если
        бы свёртка шла первой, Jita осталась бы представленной неликвидной
        строкой, вторая галочка убрала бы её — и хаб пропал бы целиком,
        хотя живая альтернатива у него была.
        """
        add_history(engine, "jita", GasForm.RAW, average=3000.0)
        add_history(engine, "jita", GasForm.COMPRESSED, average=3000.0)
        # Сжатый buy у Jita — самый дешёвый и при этом вне коридора сделок
        form = self.full_grid(jita_compressed_buy="1")

        # Шаг за шагом: убрать неликвид — и лучшая живая строка Jita это первая
        # строка Jita в оставшемся списке, потому что список отсортирован
        after_hide = self.rows(
            client.post(
                "/calculate", data=dict(form, hide_illiquid="on")
            ).get_data(as_text=True)
        )
        best_jita = next(row for row in after_hide if "Jita" in row)

        both = self.rows(
            client.post(
                "/calculate", data=dict(form, hide_illiquid="on", best_per_hub="on")
            ).get_data(as_text=True)
        )
        jita_rows = [row for row in both if "Jita" in row]
        # Jita не пропала целиком: неликвидную строку убрали, живая осталась
        assert jita_rows == [best_jita]

        # А вот свёртка без первой галочки оставила бы у Jita именно ту строку,
        # которую «скрыть неликвид» потом бы и убрала, — ради этого порядок
        # и зафиксирован
        collapsed_first = self.rows(
            client.post(
                "/calculate", data=dict(form, best_per_hub="on")
            ).get_data(as_text=True)
        )
        assert next(row for row in collapsed_first if "Jita" in row) != best_jita

    def test_nothing_to_collapse_no_counter(self, client):
        """По одной строке на хаб и так — сворачивать нечего, счётчика нет."""
        form = dict(CONTROL_FORM)
        form.update({"jita_rate": "500", "jita_raw_sell": "3000", "best_per_hub": "on"})
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert len(self.rows(html)) == 1
        assert "Свёрнуто строк" not in html


class TestSorting:
    """Сортировка кликом по заголовку (ROADMAP, этап 16).

    Сортирует сервер: клик пишет выбор в скрытые поля формы, дальше это
    обычный расчёт. Порядок показа меняется, рейтинг по ISK/юнит — нет.
    """

    def grid(self, **extra) -> dict:
        """Три хаба, шесть строк: хватает, чтобы порядок был различим."""
        form = dict(CONTROL_FORM)
        form.update(
            {
                "jita_rate": "500",
                "jita_raw_sell": "3000",
                "jita_compressed_sell": "2750",
                "jita_compressed_buy": "2400",
                "amarr_rate": "700",
                "amarr_compressed_sell": "2300",
                "amarr_compressed_buy": "2050",
                "dodixie_rate": "600",
                "dodixie_compressed_sell": "2900",
            }
        )
        form.update(extra)
        return form

    def cells(self, html: str, index: int) -> list[str]:
        """Одна колонка таблицы сверху вниз, в порядке отрисовки."""
        body = html[html.index("<tbody>"):html.index("</tbody>")]
        out = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            out.append(" ".join(re.sub(r"<[^>]+>", " ", cells[index]).split()))
        return out

    def numbers(self, html: str, index: int) -> list[float]:
        """Та же колонка числами: разрядники убраны.

        Годится только для колонок, где стоит ровно одно точное число, —
        цена, количество, объём, ISK/юнит. «Итого» и «Доставка» показываются
        компактной записью («169.3M»), и сравнивать порядок по ней нельзя:
        точное значение у них лежит в подсказке ячейки, см. titles().
        """
        return [
            float(re.sub(r"[^0-9.]", "", value)) for value in self.cells(html, index)
        ]

    def titles(self, html: str, index: int) -> list[float]:
        """Точные значения из подсказки ячейки — для компактных колонок."""
        body = html[html.index("<tbody>"):html.index("</tbody>")]
        out = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
            cell = re.findall(r"<td[^>]*>.*?</td>", row, re.S)[index]
            title = re.search(r'title="([^"]+)"', cell).group(1)
            out.append(float(re.sub(r"[^0-9.]", "", title)))
        return out

    def post(self, client, **extra) -> str:
        return client.post("/calculate", data=self.grid(**extra)).get_data(as_text=True)

    def test_default_is_isk_ascending(self, client):
        """Умолчание не поехало: ISK/юнит по возрастанию, лучшее сверху."""
        isk = self.numbers(self.post(client), 12)
        assert isk == sorted(isk)

    @pytest.mark.parametrize(
        ("column", "index"),
        [("price", 3), ("qty", 7), ("volume_m3", 8), ("isk_per_unit", 12)],
    )
    def test_numeric_columns_sort_both_ways(self, client, column, index):
        """Каждая числовая колонка сортирует по своему ключу в обе стороны."""
        up = self.numbers(self.post(client, sort=column, sort_dir="asc"), index)
        down = self.numbers(self.post(client, sort=column, sort_dir="desc"), index)
        assert len(up) == 6, "выборка должна быть достаточной, чтобы порядок был виден"
        assert up == sorted(up)
        assert down == sorted(down, reverse=True)

    def test_total_sorts_by_the_exact_value(self, client):
        """«Итого» показывается компактно, а сортируется по точному числу."""
        up = self.titles(self.post(client, sort="total", sort_dir="asc"), 11)
        down = self.titles(self.post(client, sort="total", sort_dir="desc"), 11)
        assert up == sorted(up)
        assert down == sorted(down, reverse=True)
        assert up == list(reversed(down))

    def test_hub_sorts_in_game_order_not_alphabet(self, client):
        """Хаб — в порядке constants.HUBS: Jita, Amarr, Dodixie. По алфавиту
        было бы Amarr, Dodixie, Jita — и это не тот порядок, к которому
        игрок привык в сетке цен."""
        hubs = [name.split()[0] for name in self.cells(self.post(client, sort="hub"), 1)]
        assert hubs == sorted(hubs, key=["Jita", "Amarr", "Dodixie"].index)
        assert hubs[0] == "Jita"

        back = [
            name.split()[0]
            for name in self.cells(self.post(client, sort="hub", sort_dir="desc"), 1)
        ]
        assert back[0] == "Dodixie"

    def test_garbage_falls_back_to_default(self, client):
        """Мусор в скрытых полях — состояние интерфейса, а не ввод человека:
        молча умолчание, а не ошибка."""
        default = self.post(client)
        for bad in ({"sort": "; drop table"}, {"sort_dir": "боком"}, {"sort": ""}):
            assert self.numbers(self.post(client, **bad), 12) == self.numbers(default, 12)

    def test_rank_stays_with_isk_not_with_the_screen(self, client):
        """«#» — место в порядке по ISK/юнит, а не номер строки на экране."""
        html = self.post(client, sort="isk_per_unit", sort_dir="desc")
        ranks = [int(value) for value in self.cells(html, 0)]
        # Порядок перевёрнут: сверху самая дорогая строка, и её место последнее
        assert ranks == sorted(ranks, reverse=True)
        assert ranks[-1] == 1

    def test_best_row_and_delta_follow_isk(self, client):
        """При сортировке по цене подсвечена та же лучшая строка, а её Δ —
        прочерк. Лучшее не переезжает вслед за порядком показа."""
        plain = self.post(client)
        best = self.cells(plain, 1)[0]

        html = self.post(client, sort="price", sort_dir="desc")
        body = html[html.index("<tbody>"):html.index("</tbody>")]
        marked = re.findall(r'<tr class="([^"]*)"', body)
        assert sum("best" in cls for cls in marked) == 1
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)
        best_row = next(row for row, cls in zip(rows, marked) if "best" in cls)
        assert best.split()[0] in re.sub(r"<[^>]+>", " ", best_row)
        assert 'cell-delta--muted">—' in best_row
        # Прочерк ровно один: Δ есть у всех строк, кроме лучшей
        assert body.count('cell-delta--muted">—') == 1

    def test_aria_sort_on_exactly_one_column(self, client):
        """Озвучка обязана видеть ровно одну отсортированную колонку."""
        for extra in ({}, {"sort": "hub"}, {"sort": "total", "sort_dir": "desc"}):
            html = self.post(client, **extra)
            head = html[: html.index("<tbody>")]
            assert head.count('aria-sort="ascending"') + head.count(
                'aria-sort="descending"'
            ) == 1
            assert head.count('aria-sort="none"') == len(SORT_COLUMNS) - 1

    def test_headers_are_buttons(self, client):
        """Заголовок — <button> внутри <th>, а не обработчик на ячейке:
        клавиатура и озвучка должны видеть кнопку."""
        html = self.post(client)
        head = html[: html.index("<tbody>")]
        assert head.count("data-sort=") == len(SORT_COLUMNS)
        assert "<th onclick" not in head
        for key, _label in SORT_COLUMNS:
            assert f'data-sort="{key}"' in head

    def test_second_click_flips_direction(self, client):
        """Направление следующего клика: по активной колонке — обратное,
        по новой — по возрастанию."""
        head = self.post(client, sort="total", sort_dir="asc")
        head = head[: head.index("<tbody>")]
        assert 'data-sort="total" data-sort-dir="desc"' in head
        assert 'data-sort="price" data-sort-dir="asc"' in head

    def test_sorting_applies_after_the_collapse(self, client):
        """Свёртка по хабам выбирает лучшую строку по ISK/юнит независимо
        от того, по какой колонке смотрит пользователь (ROADMAP 15.0)."""
        by_isk = self.cells(self.post(client, best_per_hub="on"), 2)
        by_volume = self.cells(
            self.post(client, best_per_hub="on", sort="volume_m3", sort_dir="desc"), 2
        )
        assert sorted(by_isk) == sorted(by_volume)


class TestRowDetail:
    """Панель «Разбор строки» (ROADMAP, этап 17).

    Панель рисует сервер тем же кодом, что и таблицу: второго экземпляра
    математики в браузере быть не должно.
    """

    def form(self, **extra) -> dict:
        form = dict(CONTROL_FORM)
        form.update(
            {
                "jita_rate": "500",
                "jita_raw_sell": "3000",
                "jita_compressed_sell": "2750",
                "amarr_rate": "700",
                "amarr_compressed_buy": "2050",
            }
        )
        form.update(extra)
        return form

    def open(self, client, key: str, **extra) -> str:
        return client.post(
            "/row-detail", data=self.form(row=key, **extra)
        ).get_data(as_text=True)

    def test_shows_the_numbers_of_that_row(self, client):
        """Числа именно этой строки, а не соседней и не «примерно такие же»."""
        html = self.open(client, "jita|raw|sell")
        assert "Jita" in html
        # Сырой sell по 3000: 50 000 юнитов, 250 000 м³, доставка 125 000 000
        assert "3 000" in html          # ваша цена
        assert "50 000" in html         # нужно купить
        assert "125 000 000" in html    # доставка за объём
        assert "[<b>sell</b>]" in html

    def test_other_row_gives_other_numbers(self, client):
        """Ключ выбирает строку: два разных ключа дают разные разборы."""
        raw = self.open(client, "jita|raw|sell")
        compressed = self.open(client, "jita|compressed|sell")
        assert raw != compressed
        assert "56 180" in compressed and "56 180" not in raw

    def test_unknown_key_is_an_empty_answer(self, client):
        """Форма изменилась между кликом и запросом — панель не открывается.
        Ошибку показывать не за что."""
        for key in ("", "мусор", "jita|raw|buy", "thera|raw|sell"):
            response = client.post("/row-detail", data=self.form(row=key))
            assert response.status_code == 200
            assert response.get_data(as_text=True).strip() == ""

    def test_broken_form_does_not_raise(self, client):
        """Кривая форма — тоже пустой ответ, а не исключение."""
        response = client.post("/row-detail", data={"row": "jita|raw|sell"})
        assert response.status_code == 200
        assert response.get_data(as_text=True).strip() == ""

    def test_shares_sum_to_hundred(self, client):
        """Доли раскладки те же, что в полоске таблицы: ровно 100 %."""
        html = self.open(client, "jita|raw|sell")
        stack = re.search(r'<div class="stack">(.*?)</div>', html, re.S).group(1)
        shares = [float(value) for value in re.findall(r"width: ([\d.]+)%", stack)]
        assert len(shares) == 3
        assert sum(shares) == pytest.approx(100.0)

    def test_numbers_match_the_table(self, client):
        """Панель и таблица говорят одними и теми же числами.

        Числа в панели не пересчитываются «примерно так же»: их собирает тот же
        `_row_views`, что рисует таблицу. Сверяем по строке, а не по первой
        попавшейся ячейке — первой в таблице стоит лучшая, а это другая строка.
        """
        key = "jita|raw|sell"
        table = client.post("/calculate", data=self.form()).get_data(as_text=True)
        row = next(
            chunk
            for chunk in re.findall(r"<tr[^>]*>.*?</tr>", table, re.S)
            if key in chunk
        )
        isk = re.search(r'isk__value[^>]*>([^<]+)<', row).group(1).strip()
        # Подсказок с точной суммой в строке две: доставка и итог. Нужен итог,
        # он второй; доставку панель показывает разложенной на объём
        # и обеспечение, и одним числом её там нет
        total = re.findall(r'title="([^"]+) ISK"', row)[-1]

        panel = self.open(client, key)
        assert isk in panel
        assert total in panel

    def test_without_history_the_market_block_explains_itself(self, client):
        """Истории нет — вместо чисел рынка объяснение, а не прочерки молча."""
        html = self.open(client, "jita|raw|sell")
        assert "нет данных" in html
        assert "Истории сделок по этому хабу" in html

    def test_with_history_the_market_block_has_numbers(self, client, engine):
        add_history(engine, "jita", GasForm.RAW, average=3000.0, volume=700_000)
        html = self.open(client, "jita|raw|sell")
        assert "Истории сделок по этому хабу" not in html
        assert "2 940" in html and "3 060" in html   # диапазон недели

    def test_depth_is_honest_when_unknown(self, client):
        """Цена введена руками — глубина неизвестна, и это сказано словами."""
        html = self.open(client, "jita|raw|sell")
        assert "неизвестно" in html
        assert "введена руками" in html

    def test_breakeven_shown_where_both_sell_prices_are_filled(self, client):
        """У Jita заполнены обе цены sell — точка считается."""
        html = self.open(client, "jita|raw|sell")
        assert "4 645" in html
        assert "сжатый выгоднее сырого" in html

    def test_breakeven_absence_is_explained(self, client):
        """У Amarr только цена buy — сказать, чего не хватает, а не молчать."""
        html = self.open(client, "amarr|compressed|buy")
        assert "Посчитать не из чего" in html
        assert "сырой sell" in html and "сжатый sell" in html

    def test_worries_block_says_when_calm(self, client):
        """Ни один значок не поднялся — так и написано, а не пустое место."""
        html = self.open(client, "jita|raw|sell")
        assert "Ничего: цена в диапазоне сделок" in html

    def test_worries_block_lists_the_flags(self, client, engine):
        """Значок поднялся — его текст стоит в панели целиком."""
        add_history(engine, "jita", GasForm.RAW, average=6000.0)
        html = self.open(client, "jita|raw|sell", jita_raw_sell="1")
        assert "Сделок по такой цене не было" in html
        assert "Ничего: цена в диапазоне сделок" not in html

    def test_row_is_clickable_and_reachable_from_keyboard(self, client):
        """Строка таблицы открывает панель и доступна с клавиатуры."""
        html = client.post("/calculate", data=self.form()).get_data(as_text=True)
        assert 'hx-post="/row-detail"' in html
        assert 'tabindex="0"' in html
        assert "keyup[key==&#39;Enter&#39;]" in html or "keyup[key=='Enter']" in html
        assert 'id="row-detail"' in html

    def test_breakeven_left_the_table_for_the_panel(self, client):
        """Блока «Точки безубыточности» под таблицей больше нет — убран
        22.08.2026. Число не пропало: оно в разборе строки своего хаба."""
        table = client.post("/calculate", data=self.form()).get_data(as_text=True)
        assert "Точки безубыточности" not in table
        assert "4 645" not in table
        assert "4 645" in self.open(client, "jita|raw|sell")


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
