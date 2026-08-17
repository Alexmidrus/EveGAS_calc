"""Тесты эндпоинта POST /fetch-prices.

Сеть подменяется на уровне esi.fetch_many: сам клиент проверен в test_esi.py,
здесь проверяется обвязка — что цены попадают в нужные ячейки, что ручной ввод
переживает подтяжку и что про любую неудачу сказано вслух.
"""

import json
import re
from pathlib import Path

import pytest

from app import create_app
from app.routes import PRICE_COLUMNS
from app.services.esi import OrdersResult

FIXTURES = Path(__file__).parent / "fixtures"

# data/gases.json, сгенерирован из SDE
C320_COMPRESSED_TYPE_ID = 62406
C320_RAW_TYPE_ID = 30377
JITA_REGION = 10000002
AMARR_REGION = 10000043

# Форма подтяжки: параметров расчёта достаточно, цены не нужны
FETCH_FORM = {
    "gas": "fullerite_c320",
    "n_units": "10000",
    "structure": "athanor",
    "gde_level": "5",
}


@pytest.fixture()
def client():
    app = create_app()
    app.testing = True
    return app.test_client()


@pytest.fixture()
def books() -> dict[tuple[int, int], list[dict]]:
    """Стаканы по паре (регион, тип), как их отдаёт ESI.

    Заполнены только сжатые типы в Jita и Amarr. По сырому газу стакан пуст —
    так проверяется, что незаполненная колонка не затирает ручной ввод.
    """
    return {
        (JITA_REGION, C320_COMPRESSED_TYPE_ID): json.loads(
            (FIXTURES / "orders_jita_compressed.json").read_text(encoding="utf-8")
        ),
        (AMARR_REGION, C320_COMPRESSED_TYPE_ID): json.loads(
            (FIXTURES / "orders_amarr_compressed.json").read_text(encoding="utf-8")
        ),
    }


@pytest.fixture()
def fake_esi(monkeypatch, books):
    """Подменяет сетевой слой. Возвращает список пар, о которых спросили."""
    asked: list[tuple[int, int]] = []
    failures: dict[int, str] = {}

    async def fetch_many(pairs, settings, **kwargs):
        asked.extend(pairs)
        out = {}
        for region_id, type_id in pairs:
            if region_id in failures:
                out[(region_id, type_id)] = OrdersResult(
                    region_id, type_id, error=failures[region_id]
                )
            else:
                out[(region_id, type_id)] = OrdersResult(
                    region_id, type_id, orders=books.get((region_id, type_id), [])
                )
        return out

    monkeypatch.setattr("app.routes.esi.fetch_many", fetch_many)
    return {"asked": asked, "failures": failures}


def cell_value(html: str, name: str) -> str | None:
    """Значение ячейки сетки по имени поля."""
    match = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html)
    if match is None:
        match = re.search(rf'value="([^"]*)"[^>]*name="{name}"', html)
    return match.group(1) if match else None


def cell_tag(html: str, name: str) -> str:
    """Весь тег input по имени поля — чтобы проверить class и title."""
    match = re.search(rf"<input[^>]*name=\"{name}\"[^>]*>", html)
    assert match, f"поле {name} не найдено в ответе"
    return match.group(0)


class TestFetchPrices:
    def test_compressed_cells_are_filled_with_vwap(self, client, fake_esi):
        """Цена — средневзвешенная на нужный объём, а не лучшая в стакане."""
        html = client.post("/fetch-prices", data=FETCH_FORM).get_data(as_text=True)

        # Нужно ceil(10 000 / 0.89) = 11 236 юнитов сжатого.
        # Jita: и sell, и buy набираются из первого же ордера — цена равна его цене.
        assert cell_value(html, "jita_compressed_sell") == "2 750"
        assert cell_value(html, "jita_compressed_buy") == "2 455"
        assert cell_value(html, "amarr_compressed_sell") == "2 300"
        # Amarr buy: первого ордера (5 000 по 2075) не хватает, остальные 6 236
        # добираются по 2050 → (5 000 * 2075 + 6 236 * 2050) / 11 236 = 2 061.12.
        # Именно ради этого случая цена считается по книге, а не по лучшему ордеру.
        assert cell_value(html, "amarr_compressed_buy") == "2 061.12"

    def test_fetched_cells_are_marked(self, client, fake_esi):
        """Подтянутая цена визуально отличается от введённой руками (SPEC §4)."""
        html = client.post("/fetch-prices", data=FETCH_FORM).get_data(as_text=True)
        assert 'class="fetched"' in cell_tag(html, "jita_compressed_sell")

    def test_depth_is_passed_as_hidden_field(self, client, fake_esi):
        """Глубина стакана уезжает скрытым полем — её проверит /calculate."""
        html = client.post("/fetch-prices", data=FETCH_FORM).get_data(as_text=True)
        assert cell_value(html, "jita_compressed_sell_depth") == "142000"
        assert cell_value(html, "jita_compressed_buy_depth") == "35000"

    def test_raw_cells_are_filled_too(self, client, fake_esi, books):
        """raw_type_id заполнен из SDE, значит колонки сырого газа тоже подтягиваются."""
        books[(JITA_REGION, C320_RAW_TYPE_ID)] = [
            {"is_buy_order": False, "location_id": 60003760, "system_id": 30000142,
             "price": 3000.0, "volume_remain": 50_000, "min_volume": 1, "range": "region"},
        ]
        html = client.post("/fetch-prices", data=FETCH_FORM).get_data(as_text=True)
        assert cell_value(html, "jita_raw_sell") == "3 000"
        assert 'class="fetched"' in cell_tag(html, "jita_raw_sell")

    def test_missing_type_id_is_stated_not_invented(self, client, fake_esi, monkeypatch):
        """Если type_id в справочнике всё же пуст — сказать об этом, а не выдумать.

        Сейчас data/gases.json сгенерирован из SDE и пустых ID в нём нет, но
        защита обязана остаться: подставлять правдоподобную чушь запрещено
        (CLAUDE.md). Поэтому газ с raw_type_id=None подсовывается явно.
        """
        from dataclasses import replace

        from app.core import catalog

        gas = replace(catalog.gas_by_key("fullerite_c320"), raw_type_id=None)
        monkeypatch.setattr("app.routes.catalog.gas_by_key", lambda key: gas)

        html = client.post("/fetch-prices", data=FETCH_FORM).get_data(as_text=True)
        assert "type_id не заполнен" in html
        assert cell_value(html, "jita_raw_sell") == ""
        # выдуманных type_id в запросах нет — спрашивали только про сжатый
        assert {type_id for _, type_id in fake_esi["asked"]} == {C320_COMPRESSED_TYPE_ID}

    def test_ten_requests_five_regions_two_types(self, client, fake_esi):
        """ESI §1: 2 type_id × 5 регионов = 10 запросов на одну подтяжку."""
        html = client.post("/fetch-prices", data=FETCH_FORM).get_data(as_text=True)
        assert len(fake_esi["asked"]) == 10
        assert len(set(fake_esi["asked"])) == 10  # без повторов
        assert {type_id for _, type_id in fake_esi["asked"]} == {
            C320_RAW_TYPE_ID, C320_COMPRESSED_TYPE_ID,
        }
        assert html  # ответ — фрагмент сетки, не полная страница
        assert "<html" not in html

    def test_freight_rates_are_not_touched(self, client, fake_esi):
        """Ставки доставки ESI не знает — подтяжка обязана их сохранить."""
        form = dict(FETCH_FORM, jita_rate="500", rens_rate="900")
        html = client.post("/fetch-prices", data=form).get_data(as_text=True)
        assert cell_value(html, "jita_rate") == "500"
        assert cell_value(html, "rens_rate") == "900"

    def test_manual_price_survives_where_esi_gave_nothing(self, client, fake_esi):
        """Стакан по сырому газу пуст — введённое руками должно остаться на месте."""
        form = dict(FETCH_FORM, jita_raw_sell="3000")
        html = client.post("/fetch-prices", data=form).get_data(as_text=True)
        assert cell_value(html, "jita_raw_sell") == "3000"
        assert 'class="fetched"' not in cell_tag(html, "jita_raw_sell")

    def test_failed_hub_is_reported_and_keeps_manual_input(self, client, fake_esi):
        """Хаб не ответил — «данные не получены», а не тихий ноль (CLAUDE.md)."""
        fake_esi["failures"][JITA_REGION] = "ESI недоступен (503)"
        form = dict(FETCH_FORM, jita_compressed_sell="2700")
        html = client.post("/fetch-prices", data=form).get_data(as_text=True)

        assert "данные не получены" in html
        assert "503" in html
        assert cell_value(html, "jita_compressed_sell") == "2700"
        # остальные хабы посчитаны как ни в чём не бывало
        assert cell_value(html, "amarr_compressed_sell") == "2 300"

    def test_empty_book_is_reported(self, client, fake_esi, books):
        """Пустой стакан — не ошибка, но и не молчание."""
        books[(JITA_REGION, C320_COMPRESSED_TYPE_ID)] = []
        html = client.post("/fetch-prices", data=FETCH_FORM).get_data(as_text=True)
        assert "Подходящих ордеров в стакане нет" in html
        assert cell_value(html, "jita_compressed_sell") == ""

    def test_blind_spot_footnote_is_always_present(self, client, fake_esi):
        """Сноска про buy-ордера с радиусом обязательна (ESI §3.2)."""
        html = client.post("/fetch-prices", data=FETCH_FORM).get_data(as_text=True)
        assert "радиусом" in html
        assert "игровых структур" in html

    def test_bad_params_do_not_hit_esi(self, client, fake_esi):
        """Кривой ввод — внятное объяснение и ни одного запроса наружу."""
        form = dict(FETCH_FORM, n_units="-5")
        html = client.post("/fetch-prices", data=form).get_data(as_text=True)
        assert "Цены не подтянуты" in html
        assert fake_esi["asked"] == []

    def test_unknown_gas_does_not_hit_esi(self, client, fake_esi):
        form = dict(FETCH_FORM, gas="нет такого газа")
        html = client.post("/fetch-prices", data=form).get_data(as_text=True)
        assert "Неизвестный газ" in html
        assert fake_esi["asked"] == []

    def test_index_renders_grid_and_button(self, client):
        """Сетка на странице — тот же partial, что отдаёт подтяжка."""
        html = client.get("/").get_data(as_text=True)
        assert "Подтянуть цены из ESI" in html
        for suffix, _ in PRICE_COLUMNS:
            assert f'name="jita_{suffix}"' in html
            assert f'name="jita_{suffix}_depth"' in html


class TestDepthReachesResults:
    """Глубина из сетки должна доезжать до таблицы результата (SPEC §6)."""

    BASE = {
        "gas": "fullerite_c320",
        "n_units": "50000",
        "structure": "athanor",
        "gde_level": "5",
        "broker_fee": "1.5",
        "collateral_pct": "0.5",
        "jita_rate": "500",
        "jita_compressed_sell": "2750",
    }

    def test_shallow_book_warning_shown(self, client):
        """Нужно 56 180, в стакане 20 000 — строка обязана быть помечена."""
        form = dict(self.BASE, jita_compressed_sell_depth="20000")
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "в стакане только 20 000" in html
        assert "Глубины стакана не хватает" in html

    def test_deep_book_is_quiet(self, client):
        form = dict(self.BASE, jita_compressed_sell_depth="500000")
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "в стакане только" not in html

    def test_manual_price_has_no_depth_warning(self, client):
        """Без подтяжки глубина неизвестна — предупреждений о ней быть не может."""
        html = client.post("/calculate", data=dict(self.BASE)).get_data(as_text=True)
        assert "в стакане только" not in html

    def test_garbage_depth_does_not_break_calculation(self, client):
        """Мусор в скрытом поле не должен ронять расчёт."""
        form = dict(self.BASE, jita_compressed_sell_depth="не число")
        response = client.post("/calculate", data=form)
        assert response.status_code == 200
        assert "в стакане только" not in response.get_data(as_text=True)
