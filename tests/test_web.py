"""Тесты веб-слоя: форматирование, разбор формы, контрольный пример через HTTP.

Приёмка этапа 2: вбитый руками контрольный пример из docs/DOMAIN.md §5
показывает на экране те же числа, что в таблице ожидаемых результатов.
"""

import pytest

from app import create_app
from app.formatting import fmt_compact, fmt_number, fmt_percent


@pytest.fixture()
def client():
    """Тестовый клиент Flask."""
    app = create_app()
    app.testing = True
    return app.test_client()


# Форма контрольного примера DOMAIN §5. Пробелы и запятая в числах — намеренно:
# парсер обязан их понимать.
CONTROL_FORM = {
    "gas": "fullerite_c320",
    "n_units": "50 000",
    "structure": "athanor",
    "gde_level": "5",
    "broker_fee": "1,5",
    "collateral_pct": "0,5",
    "jita_rate": "500",
    "amarr_rate": "700",
    "dodixie_rate": "600",
    "rens_rate": "900",
    "hek_rate": "850",
    "jita_raw_sell": "3000",
    "jita_raw_buy": "2620",
    "jita_compressed_sell": "2750",
    "jita_compressed_buy": "2400",
    "amarr_compressed_sell": "2300",
    "amarr_compressed_buy": "2050",
    "dodixie_compressed_sell": "2900",
    "dodixie_compressed_buy": "2450",
    "rens_compressed_sell": "3050",
    "hek_compressed_sell": "3200",
}


class TestFormatting:
    """Хелпер форматирования: «1 234 567», «2 751.19», «137.6M»."""

    def test_int_grouping(self):
        """Разряды через пробел."""
        assert fmt_number(1_234_567) == "1 234 567"

    def test_fixed_decimals(self):
        """Явная точность: два знака, без float-мусора."""
        assert fmt_number(2751.1907, 2) == "2 751.19"
        assert fmt_number(2751.0000000004, 2) == "2 751.00"

    def test_auto_decimals(self):
        """Автоматика: целые — без дробей, дробные — без хвостовых нулей."""
        assert fmt_number(28090.0) == "28 090"
        assert fmt_number(0.5) == "0.5"
        assert fmt_number(137559535.0000001) == "137 559 535"

    def test_compact(self):
        """Компактные миллионы и миллиарды."""
        assert fmt_compact(138_440_465) == "138.4M"
        assert fmt_compact(2_500_000_000) == "2.5B"
        assert fmt_compact(950_000) == "950 000"

    def test_percent(self):
        """Проценты: целые по умолчанию, точность задаётся."""
        assert fmt_percent(11.00035) == "11%"
        assert fmt_percent(8.954, 1) == "9.0%"


class TestIndexPage:
    """GET / — страница с формой."""

    def test_page_renders(self, client):
        """Страница отдаётся, все 25 газов и 5 хабов на месте."""
        html = client.get("/").get_data(as_text=True)
        assert "Fullerite-C320" in html
        assert html.count("<option value=") >= 25 + 6  # газы + уровни GDE
        for hub in ("Jita", "Amarr", "Dodixie", "Rens", "Hek"):
            assert hub in html

    def test_defaults(self, client):
        """Значения по умолчанию из SPEC §3 и живые подписи."""
        html = client.get("/").get_data(as_text=True)
        assert 'value="10000"' in html                      # N по умолчанию
        assert ">89</strong>%" in html                      # Athanor + GDE 5
        assert "5 м³ сырой / 0.5 м³ сжатый" in html         # объёмы C320

    def test_eta_map_from_core(self, client):
        """Карта процентов для JS отрендерена и содержит граничные значения."""
        html = client.get("/").get_data(as_text=True)
        assert "data-eta-map" in html
        assert '&#34;tatara&#34;' in html  # JSON в атрибуте, экранирован Jinja


class TestCalculateControlExample:
    """Контрольный пример через POST /calculate."""

    @pytest.fixture()
    def html(self, client):
        """Отрендеренный фрагмент результата."""
        response = client.post("/calculate", data=CONTROL_FORM)
        assert response.status_code == 200
        return response.get_data(as_text=True)

    def test_key_numbers_on_screen(self, html):
        """Числа из таблицы DOMAIN §5 присутствуют на экране."""
        for number in (
            "2 742.71",      # лучший ISK/юнит
            "2 990.46", "3 031.47", "3 144.96", "3 386.25",
            "3 611.81", "3 949.73", "4 091.03", "5 172.40", "5 515.00",
            "137 135 380",   # лучший итог
            "275 750 000",   # худший итог
            "56 180",        # сколько сжатого покупаем
            "20 238 845",    # полная доставка Amarr: объём плюс обеспечение
            "6 180",         # потери
        ):
            assert number in html, f"нет числа {number}"

    def test_sort_order(self, html):
        """Строки идут по возрастанию ISK/юнит."""
        positions = [html.index(n) for n in ("2 742.71", "2 990.46", "3 031.47", "5 515.00")]
        assert positions == sorted(positions)

    def test_summary(self, html):
        """Сводка: лучший вариант, экономия, потери."""
        assert "Amarr" in html
        assert "сжатый, buy" in html
        assert "138.6M" in html      # экономия против худшего
        assert "11%" in html         # процент потерь

    def test_breakeven(self, html):
        """Точка безубыточности Jita: 4 645 ISK. От обеспечения не зависит."""
        assert "сжатый выгоднее сырого" in html
        assert "4 645" in html

    def test_collateral_note(self, html):
        """Сноска: процент, надбавка лучшей строки и плата за объём отдельно."""
        assert "обеспечение — 0.5%" in html
        assert "575 845" in html     # надбавка Amarr / сжатый / buy
        assert "19 663 000" in html  # плата за объём той же строки

    def test_no_missing_rate_notes(self, html):
        """Все ставки заданы — пометок «не задана доставка» нет."""
        assert "не задана доставка" not in html


class TestCalculateVariants:
    """Вариации: процент обеспечения, пропуски."""

    def test_zero_collateral(self, client):
        """0% — доставка Amarr равна плате за объём, сноски об обеспечении нет."""
        form = dict(CONTROL_FORM, collateral_pct="0")
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "19 663 000" in html
        assert "20 238 845" not in html
        assert "В «Доставку» входит обеспечение" not in html

    def test_bigger_collateral_raises_freight(self, client):
        """2% вместо 0.5%: надбавка Amarr вчетверо больше, 575 845 → 2 303 380."""
        form = dict(CONTROL_FORM, collateral_pct="2")
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "2 303 380" in html
        assert "обеспечение — 2%" in html

    def test_collateral_missing(self, client):
        """Пустое поле обеспечения — понятная ошибка, а не молчаливый ноль."""
        form = {k: v for k, v in CONTROL_FORM.items() if k != "collateral_pct"}
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "«Обеспечение»: поле пустое." in html

    def test_collateral_not_a_number(self, client):
        """Мусор в поле обеспечения — ошибка с указанием, что именно введено."""
        form = dict(CONTROL_FORM, collateral_pct="полпроцента")
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "не похоже на число" in html

    def test_missing_rate_drops_hub(self, client):
        """Хаб без ставки: строк нет, пометка есть."""
        form = dict(CONTROL_FORM)
        del form["hek_rate"]
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "Hek: не задана доставка" in html
        assert "4 093.05" not in html  # строка Hek выпала

    def test_empty_grid(self, client):
        """Совсем без цен и ставок — понятное сообщение, не ошибка."""
        form = {k: v for k, v in CONTROL_FORM.items()
                if not any(k.startswith(h) for h in ("jita", "amarr", "dodixie", "rens", "hek"))}
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "Недостаточно данных" in html

    def test_outlier_marked(self, client):
        """Выброс цены помечен звёздочкой и расшифровкой."""
        form = dict(CONTROL_FORM)
        form["dodixie_compressed_sell"] = "50000"  # в ~17 раз выше медианы
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "Цена сильно выбивается" in html


class TestSellOnlyFilter:
    """Чекбокс «только sell» (SPEC §5.4)."""

    def test_off_by_default(self, client):
        """По умолчанию показываются все сценарии."""
        html = client.post("/calculate", data=CONTROL_FORM).get_data(as_text=True)
        assert "2 742.71" in html  # Amarr сжатый buy — лучший вариант без фильтра
        assert "только sell" not in html

    def test_on_hides_buy_rows(self, client):
        form = dict(CONTROL_FORM, sell_only="on")
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "2 742.71" not in html          # buy-строки ушли
        assert "2 990.46" in html              # Amarr сжатый sell остался
        assert "Показаны только sell" in html  # и об этом сказано

    def test_best_variant_recomputed(self, client):
        """Сводка пересчитана по оставшимся строкам."""
        form = dict(CONTROL_FORM, sell_only="on")
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "сжатый, sell" in html

    def test_breakeven_still_shown(self, client):
        form = dict(CONTROL_FORM, sell_only="on")
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "4 645" in html

    def test_checkbox_present_on_page(self, client):
        html = client.get("/").get_data(as_text=True)
        assert 'name="sell_only"' in html

    def test_autocalc_trigger_present(self, client):
        """Автопересчёт с debounce (SPEC §2), кнопка при этом остаётся."""
        html = client.get("/").get_data(as_text=True)
        assert "input delay:400ms" in html
        assert "Посчитать" in html


class TestGasesApi:
    """GET /api/gases (SPEC §9)."""

    def test_returns_all_gases_and_hubs(self, client):
        payload = client.get("/api/gases").get_json()
        assert len(payload["gases"]) == 27
        assert [hub["key"] for hub in payload["hubs"]] == [
            "jita", "amarr", "dodixie", "rens", "hek",
        ]

    def test_gas_shape(self, client):
        payload = client.get("/api/gases").get_json()
        c320 = next(g for g in payload["gases"] if g["key"] == "fullerite_c320")
        assert c320["name"] == "Fullerite-C320"
        assert c320["volume_raw"] == 5.0
        assert c320["volume_compressed"] == 0.5
        assert c320["compressed_type_id"] == 62406

    def test_type_ids_come_from_sde(self, client):
        """type_id заполнены генератором из SDE, а не выдуманы."""
        payload = client.get("/api/gases").get_json()
        assert all(isinstance(gas["raw_type_id"], int) for gas in payload["gases"])
        c320 = next(g for g in payload["gases"] if g["key"] == "fullerite_c320")
        assert c320["raw_type_id"] == 30377

    def test_unknown_ids_are_null_not_invented(self, client):
        """У четырёх хабов system_id неизвестен — в API это честный null."""
        payload = client.get("/api/gases").get_json()
        hubs = {hub["key"]: hub for hub in payload["hubs"]}
        assert hubs["amarr"]["system_id"] is None
        assert hubs["jita"]["system_id"] == 30000142


class TestValidation:
    """Понятные сообщения об ошибках, все сразу."""

    def test_bad_n_units(self, client):
        """Отрицательное N — ошибка."""
        form = dict(CONTROL_FORM)
        form["n_units"] = "-5"
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "Расчёт не выполнен" in html
        assert "больше нуля" in html

    def test_bad_broker(self, client):
        """Брокер вне 0..5 и мусор вместо числа."""
        form = dict(CONTROL_FORM)
        form["broker_fee"] = "12"
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "от 0 до 5" in html

        form["broker_fee"] = "abc"
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "не похоже на число" in html

    def test_bad_price_names_hub_and_column(self, client):
        """Ошибка в ячейке сетки называет хаб и колонку."""
        form = dict(CONTROL_FORM)
        form["amarr_compressed_buy"] = "0"
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert "Amarr" in html
        assert "Сжатый buy" in html
        assert "больше нуля" in html

    def test_multiple_errors_at_once(self, client):
        """Несколько ошибок показываются разом, а не по одной."""
        form = dict(CONTROL_FORM)
        form["n_units"] = "0"
        form["broker_fee"] = "xx"
        form["jita_raw_sell"] = "-3"
        html = client.post("/calculate", data=form).get_data(as_text=True)
        assert html.count("<li>") >= 3

    def test_unknown_gas(self, client):
        """Неизвестный ключ газа — ошибка, а не 500."""
        form = dict(CONTROL_FORM)
        form["gas"] = "veldspar"
        response = client.post("/calculate", data=form)
        assert response.status_code == 200
        assert "Неизвестный газ" in response.get_data(as_text=True)
