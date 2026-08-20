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


class TestTerminalLayout:
    """Оформление «GasLens Terminal» (SPEC §10, этап 12).

    Проверяются не классы и не теги, а то, что человек видит и на что жмёт:
    какая тема включается сама, откуда берётся знаменатель «из N», сходятся ли
    доли в полоске и честно ли выключены элементы без функционала.
    """

    def test_dark_theme_is_default(self, client):
        """Тёмная тема — по умолчанию, и она стоит в разметке до всякого JS."""
        html = client.get("/").get_data(as_text=True)
        assert 'data-theme="dark"' in html

    def test_theme_applied_before_first_paint(self, client):
        """Выбор темы читается из localStorage в <head>, а не после отрисовки:
        иначе тёмная страница мигает светлой."""
        html = client.get("/").get_data(as_text=True)
        head = html[: html.index("</head>")]
        assert "gascalc.theme" in head
        assert 'id="theme-toggle"' in html

    def test_scenario_denominator_counts_hubs_and_columns(self, client):
        """«N из 20» — не константа: знаменатель считается от числа хабов
        и ценовых колонок. Появится шестой хаб — двадцатка соврёт."""
        from app.core import catalog
        from app.routes import PRICE_COLUMNS, scenario_slots

        expected = len(catalog.hubs()) * len(PRICE_COLUMNS)
        assert scenario_slots() == expected

        html = client.post("/calculate", data=CONTROL_FORM).get_data(as_text=True)
        assert f"из {expected}" in html

    def test_cost_shares_add_up_to_hundred(self, client):
        """Доли в полоске «из чего сложилось» дают ровно 100 %.

        Три независимо округлённые доли дали бы 99.9 % — щель на конце полоски.
        """
        import re

        html = client.post("/calculate", data=CONTROL_FORM).get_data(as_text=True)
        stacks = re.findall(r'<div class="stack">(.*?)</div>', html, re.S)
        assert stacks, "полоска «из чего сложилось» не отрисована"
        for stack in stacks:
            shares = [float(value) for value in re.findall(r"width: ([\d.]+)%", stack)]
            assert len(shares) == 3
            assert sum(shares) == pytest.approx(100.0)

    def test_pending_elements_are_disabled_and_honest(self, client):
        """Элементы из макета, к которым ещё нет функционала, остаются на экране,
        но не притворяются рабочими (SPEC §10.7)."""
        html = client.get("/").get_data(as_text=True)
        # Осталось четыре: переключатель RU / EN и три нерабочих фильтра.
        # Число намеренно точное — закрывая очередной пункт списка, его
        # положено уменьшить осознанно, а не оставить «>=» прикрывать факт
        assert html.count('title="функционал ещё не сделан"') == 4
        # Три нерабочих фильтра из четырёх: работает только «только sell»
        assert html.count("<input type=\"checkbox\" disabled>") == 3
        assert 'name="sell_only"' in html
        # Чип ESI из списка вышел: он показывает состояние сервера из базы
        assert "chip--esi pending" not in html

    def test_header_shows_current_gas_and_data_age(self, client):
        """Шапка говорит, про какой газ идёт расчёт и насколько свежи данные."""
        html = client.get("/").get_data(as_text=True)
        assert 'id="header-gas"' in html
        assert "Fullerite-C320" in html
        # Цен в тестовой базе нет — молчать об этом нельзя
        assert "цены не собраны" in html or "цены " in html

    def test_form_contract_survives_the_redesign(self, client):
        """Имена полей не поехали: на них держатся _parse_form, _build_grid
        и настройки, сохранённые в localStorage прежней версией."""
        html = client.get("/").get_data(as_text=True)
        for name in (
            "gas", "n_units", "structure", "gde_level", "broker_fee",
            "collateral_pct", "sell_only", "jita_rate", "jita_compressed_sell",
            "jita_compressed_sell_auto", "jita_compressed_sell_depth",
        ):
            assert f'name="{name}"' in html, f"поле {name} исчезло из формы"


class TestViewMath:
    """Числа, которые считает сервер вместо шаблона (SPEC §10.3)."""

    def test_share_percents_sum_to_hundred(self):
        from app.formatting import share_percents

        assert sum(share_percents((1, 1, 1), 3)) == 100.0
        assert sum(share_percents((999, 0.5, 0.5), 1000)) == 100.0

    def test_share_percents_without_total(self):
        """Делить не на что — нули, а не деление на ноль и не выдуманные доли."""
        from app.formatting import share_percents

        assert share_percents((0, 0, 0), 0) == [0.0, 0.0, 0.0]

    def test_bar_width_is_clamped(self):
        from app.formatting import bar_width

        assert bar_width(140) == "100%"
        assert bar_width(-10) == "0%"
        assert bar_width(2, floor=6) == "6%"

    def test_share_label_never_says_zero_for_a_real_cost(self):
        """Полпроцента от стоимости газа — это миллионы ISK. «0 %» их сотрёт."""
        from app.formatting import fmt_share

        assert fmt_share(0.5) == "<1"
        assert fmt_share(0) == "0"
        assert fmt_share(14.3) == "14"

class TestSparkline:
    """Спарклайн «7 дней» в строке результата (ROADMAP, пункт 3 после 0.3.0)."""

    def test_points_span_the_whole_width(self):
        """Линия начинается у левого края и заканчивается у правого."""
        from app.formatting import SPARK_WIDTH, sparkline_points

        points = sparkline_points([1, 2, 3, 4]).split()
        assert points[0].startswith("0.0,")
        assert points[-1].startswith(f"{SPARK_WIDTH:.1f},")

    def test_line_stays_inside_the_viewbox(self):
        """Иначе линию срежет краем: у полоски толщина, а у viewBox границы."""
        from app.formatting import SPARK_HEIGHT, SPARK_PADDING, sparkline_points

        ys = [
            float(pair.split(",")[1])
            for pair in sparkline_points([5, 900, 17, 240, 3]).split()
        ]
        assert min(ys) >= SPARK_PADDING
        assert max(ys) <= SPARK_HEIGHT - SPARK_PADDING

    def test_expensive_day_is_higher_on_screen(self):
        """Ось Y в SVG растёт вниз, и перепутать её значит перевернуть график."""
        from app.formatting import sparkline_points

        first, last = sparkline_points([100, 200]).split()
        assert float(first.split(",")[1]) > float(last.split(",")[1])

    def test_flat_series_sits_in_the_middle(self):
        """Ровная линия у нижнего края читается как «упало в пол»."""
        from app.formatting import SPARK_HEIGHT, sparkline_points

        ys = {pair.split(",")[1] for pair in sparkline_points([7, 7, 7]).split()}
        assert ys == {f"{SPARK_HEIGHT / 2:.1f}"}

    def test_one_point_is_not_a_line(self):
        """По одному дню недельного движения не покажешь."""
        from app.formatting import sparkline_points

        assert sparkline_points([42]) == ""
        assert sparkline_points([]) == ""

    def test_change_percent(self):
        from app.formatting import sparkline_change_pct

        assert sparkline_change_pct([100, 150]) == pytest.approx(50.0)
        assert sparkline_change_pct([100, 90]) == pytest.approx(-10.0)
        assert sparkline_change_pct([100]) is None
        assert sparkline_change_pct([0, 50]) is None

    def test_column_is_no_longer_pending(self, client):
        """Пункт закрыт: заголовок «7 дней» больше не помечен как нерабочий."""
        html = client.post("/calculate", data=CONTROL_FORM).get_data(as_text=True)
        assert "7 дней" in html
        assert '>7 дней</th>' in html
        assert 'pending" title="функционал ещё не сделан">7 дней' not in html

    def test_missing_history_says_why(self, client):
        """Без истории ячейка не пустая: прочерк и объяснение в подсказке.

        Контрольный пример вводится руками и базы истории под собой не имеет —
        ровно тот случай, когда линии нет.
        """
        html = client.post("/calculate", data=CONTROL_FORM).get_data(as_text=True)
        assert 'class="cell-spark"' in html
        assert "Недельной линии нет" in html


class TestServerChip:
    """Чип состояния сервера в шапке (ROADMAP, пункт 2 после 0.3.0)."""

    def seed(self, app, **fields):
        from app.db import EsiStatus, session_scope, utcnow
        from sqlalchemy import delete

        with app.app_context():
            engine = app.extensions["db_engine"]
            with session_scope(engine) as session:
                session.execute(delete(EsiStatus))
                session.add(
                    EsiStatus(
                        checked_at=utcnow(),
                        **(dict(reachable=True, players=26_930, vip=False, error=None) | fields),
                    )
                )
                session.commit()

    @pytest.fixture()
    def app(self):
        from app import create_app

        app = create_app()
        app.testing = True
        return app

    def test_online_shows_players(self, app):
        self.seed(app)
        html = app.test_client().get("/").get_data(as_text=True)
        assert "chip--online" in html
        assert "26 930" in html

    def test_offline_is_not_silent(self, app):
        """ESI не ответил — на экране это видно, а не подменено нулём."""
        self.seed(app, reachable=False, players=None, error="ESI недоступен (503)")
        html = app.test_client().get("/").get_data(as_text=True)
        assert "chip--offline" in html
        assert "не отвечает" in html

    def test_unknown_when_never_collected(self, app):
        from app.db import EsiStatus, session_scope
        from sqlalchemy import delete

        with app.app_context():
            with session_scope(app.extensions["db_engine"]) as session:
                session.execute(delete(EsiStatus))
                session.commit()
        html = app.test_client().get("/").get_data(as_text=True)
        assert "chip--unknown" in html
        assert "неизвестно" in html

    def test_chip_is_no_longer_pending(self, app):
        self.seed(app)
        html = app.test_client().get("/").get_data(as_text=True)
        assert "chip--esi pending" not in html
