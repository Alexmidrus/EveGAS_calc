"""Тесты ядра расчёта. Главный кейс — контрольный пример из docs/DOMAIN.md §5."""

import pytest

from app.core import calculator as calc
from app.core import catalog
from app.core.models import (
    CalcInput,
    GasForm,
    HubDepth,
    HubPrices,
    OrderSide,
    StructureType,
    WarningCode,
)

C320 = catalog.gas_by_key("fullerite_c320")

# Цены контрольного примера: заполнены только ячейки, попавшие в таблицу ожиданий
CONTROL_PRICES: dict[str, HubPrices] = {
    "jita": HubPrices(freight_rate=500, raw_sell=3000, raw_buy=2620,
                      compressed_sell=2750, compressed_buy=2400),
    "amarr": HubPrices(freight_rate=700, compressed_sell=2300, compressed_buy=2050),
    "dodixie": HubPrices(freight_rate=600, compressed_sell=2900, compressed_buy=2450),
    "rens": HubPrices(freight_rate=900, compressed_sell=3050),
    "hek": HubPrices(freight_rate=850, compressed_sell=3200),
}

# Ожидаемая таблица DOMAIN §5, в порядке сортировки:
# (хаб, форма, сторона, цена, qty, объём м³, доставка полная, итого, ISK/юнит)
# «Доставка полная» = объём × ставка + обеспечение 0.5% от стоимости газа.
EXPECTED_ROWS = [
    ("amarr", GasForm.COMPRESSED, OrderSide.BUY, 2050, 56_180, 28_090, 20_238_845, 137_135_380, 2742.71),
    ("amarr", GasForm.COMPRESSED, OrderSide.SELL, 2300, 56_180, 28_090, 20_309_070, 149_523_070, 2990.46),
    ("jita", GasForm.COMPRESSED, OrderSide.BUY, 2400, 56_180, 28_090, 14_719_160, 151_573_640, 3031.47),
    ("dodixie", GasForm.COMPRESSED, OrderSide.BUY, 2450, 56_180, 28_090, 17_542_205, 157_247_820, 3144.96),
    ("jita", GasForm.COMPRESSED, OrderSide.SELL, 2750, 56_180, 28_090, 14_817_475, 169_312_475, 3386.25),
    ("dodixie", GasForm.COMPRESSED, OrderSide.SELL, 2900, 56_180, 28_090, 17_668_610, 180_590_610, 3611.81),
    ("rens", GasForm.COMPRESSED, OrderSide.SELL, 3050, 56_180, 28_090, 26_137_745, 197_486_745, 3949.73),
    ("hek", GasForm.COMPRESSED, OrderSide.SELL, 3200, 56_180, 28_090, 24_775_380, 204_551_380, 4091.03),
    ("jita", GasForm.RAW, OrderSide.BUY, 2620, 50_000, 250_000, 125_655_000, 258_620_000, 5172.40),
    ("jita", GasForm.RAW, OrderSide.SELL, 3000, 50_000, 250_000, 125_750_000, 275_750_000, 5515.00),
]


def control_input(**overrides) -> CalcInput:
    """Вход контрольного примера; overrides — для вариаций в отдельных тестах."""
    params = dict(
        gas=C320,
        n_units=50_000,
        structure=StructureType.ATHANOR,
        gde_level=5,
        broker_fee=0.015,
        collateral_pct=0.005,
    )
    params.update(overrides)
    return CalcInput(**params)


class TestDecompressionEfficiency:
    """eta = 0.80 + бонус структуры + 0.01 * GDE."""

    def test_minimum(self):
        """Нижняя граница: Upwell без бонуса, навык 0."""
        assert calc.decompression_efficiency(StructureType.UPWELL, 0) == 0.80

    def test_maximum(self):
        """Верхняя граница: Tatara + GDE V. Во float сумма даёт
        0.9500000000000001 — функция обязана вернуть чистые 0.95."""
        assert calc.decompression_efficiency(StructureType.TATARA, 5) == 0.95

    def test_control_case(self):
        """Athanor + GDE V = 0.89 — eta контрольного примера."""
        assert calc.decompression_efficiency(StructureType.ATHANOR, 5) == 0.89

    def test_accepts_plain_string(self):
        """Строковое значение enum тоже принимается."""
        assert calc.decompression_efficiency("tatara", 0) == 0.90

    @pytest.mark.parametrize("gde", [-1, 6, 100])
    def test_gde_out_of_range(self, gde):
        """Навык за пределами 0..5 — ошибка."""
        with pytest.raises(ValueError):
            calc.decompression_efficiency(StructureType.ATHANOR, gde)

    def test_gde_not_int(self):
        """Дробный уровень навыка — ошибка."""
        with pytest.raises(ValueError):
            calc.decompression_efficiency(StructureType.ATHANOR, 2.5)

    def test_unknown_structure(self):
        """Неизвестная структура — ошибка."""
        with pytest.raises(ValueError):
            calc.decompression_efficiency("keepstar", 3)


class TestRequiredCompressedQty:
    """compressed_qty = ceil(N / eta), точная арифметика."""

    def test_control_case(self):
        """ceil(50000 / 0.89) = 56180 — из контрольного примера."""
        assert calc.required_compressed_qty(50_000, 0.89) == 56_180

    def test_ceil_on_fractional(self):
        """Некруглое частное всегда тянется вверх."""
        assert calc.required_compressed_qty(100, 0.89) == 113  # 112.36
        assert calc.required_compressed_qty(4, 0.8) == 5       # 5.0 ровно

    def test_exact_quotient_not_inflated(self):
        """Ловушка float: 95 / 0.95 = 100.0000000000000047, наивный ceil дал бы 101.
        Точное частное не должно раздуваться на лишний юнит."""
        assert calc.required_compressed_qty(95, 0.95) == 100
        assert calc.required_compressed_qty(40_000, 0.8) == 50_000

    def test_eta_one_is_lossless(self):
        """eta = 1 — гипотетический случай без потерь."""
        assert calc.required_compressed_qty(777, 1.0) == 777

    @pytest.mark.parametrize("n", [0, -1])
    def test_invalid_n(self, n):
        """N меньше либо равно нулю — ошибка."""
        with pytest.raises(ValueError):
            calc.required_compressed_qty(n, 0.89)

    @pytest.mark.parametrize("eta", [0.0, -0.5, 1.2])
    def test_invalid_eta(self, eta):
        """eta вне (0, 1] — ошибка."""
        with pytest.raises(ValueError):
            calc.required_compressed_qty(100, eta)


class TestDecompressedOutput:
    """raw_out = floor(compressed_qty * eta), дробные юниты сгорают."""

    def test_control_case(self):
        """Разжатие купленной партии даёт ровно N: floor(56180 * 0.89) = 50000."""
        assert calc.decompressed_output(56_180, 0.89) == 50_000

    def test_floor_on_fractional(self):
        """Некруглое произведение всегда режется вниз."""
        assert calc.decompressed_output(13, 0.8) == 10   # 10.4
        assert calc.decompressed_output(3, 0.89) == 2    # 2.67

    def test_exact_product_not_truncated(self):
        """Ловушка float: 100 * 0.95 = 94.99999999999999, наивный floor дал бы 94."""
        assert calc.decompressed_output(100, 0.95) == 95

    def test_minimum_two_units(self):
        """DOMAIN §1: один сжатый юнит не даёт ничего, два — уже дают."""
        assert calc.decompressed_output(1, 0.95) == 0
        assert calc.decompressed_output(2, 0.8) == 1

    def test_negative_qty(self):
        """Отрицательное количество — ошибка."""
        with pytest.raises(ValueError):
            calc.decompressed_output(-1, 0.89)


class TestBreakevenPrice:
    """P_be = eta * (P_raw + V*r) - V*r/10."""

    def test_domain_example(self):
        """Пример из DOMAIN §5: Jita, 0.89 * (3000 + 2500) - 250 = 4645."""
        assert calc.breakeven_compressed_price(0.89, 3000, 5.0, 500) == pytest.approx(4645, abs=0.01)

    def test_zero_rate(self):
        """Без доставки безубыточность — просто eta * P_raw."""
        assert calc.breakeven_compressed_price(0.89, 1000, 5.0, 0) == pytest.approx(890, abs=0.01)

    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(eta=0.0, p_raw=1000, volume=5.0, rate=500),
            dict(eta=0.89, p_raw=0, volume=5.0, rate=500),
            dict(eta=0.89, p_raw=1000, volume=0, rate=500),
            dict(eta=0.89, p_raw=1000, volume=5.0, rate=-1),
        ],
    )
    def test_invalid_inputs(self, kwargs):
        """Вырожденные входы — ошибка."""
        with pytest.raises(ValueError):
            calc.breakeven_compressed_price(**kwargs)


@pytest.fixture(scope="module")
def result():
    """Результат контрольного примера; считается один раз на модуль."""
    return calc.build_scenarios(control_input(), CONTROL_PRICES)


class TestControlExample:
    """Контрольный пример DOMAIN §5 целиком: все десять строк до сотых."""

    def test_eta_and_qty(self, result):
        """Промежуточные значения примера."""
        assert result.eta == 0.89
        assert result.compressed_qty == 56_180

    def test_row_count(self, result):
        """Заполнено 10 ценовых ячеек — ровно 10 строк, ни одной лишней."""
        assert len(result.scenarios) == 10

    def test_rows_match_expected_table(self, result):
        """Каждая строка: порядок сортировки и все числа с точностью до сотых."""
        for scenario, expected in zip(result.scenarios, EXPECTED_ROWS, strict=True):
            hub, form, side, price, qty, volume, freight, total, per_unit = expected
            label = f"{hub}/{form}/{side}"
            assert scenario.hub_key == hub, label
            assert scenario.form is form, label
            assert scenario.side is side, label
            assert scenario.price == price, label
            assert scenario.qty == qty, label
            assert scenario.volume_m3 == pytest.approx(volume, abs=0.01), label
            assert scenario.freight == pytest.approx(freight, abs=0.01), label
            assert scenario.total == pytest.approx(total, abs=0.01), label
            assert scenario.isk_per_unit == pytest.approx(per_unit, abs=0.01), label

    def test_manual_verification_row(self, result):
        """Строка Amarr / сжатый / buy — разобранная вручную в DOMAIN §5."""
        row = result.scenarios[0]
        assert row.purchase == pytest.approx(115_169_000, abs=0.01)
        assert row.broker == pytest.approx(1_727_535, abs=0.01)
        assert row.freight_volume == pytest.approx(19_663_000, abs=0.01)
        assert row.collateral_fee == pytest.approx(575_845, abs=0.01)
        assert row.freight == pytest.approx(20_238_845, abs=0.01)

    def test_broker_only_on_buy(self, result):
        """Sell-сценарии — без брокерской комиссии, buy — с ней."""
        for scenario in result.scenarios:
            if scenario.side is OrderSide.SELL:
                assert scenario.broker == 0.0
            else:
                assert scenario.broker == pytest.approx(
                    scenario.purchase * 0.015, abs=0.01
                )

    def test_collateral_is_share_of_each_purchase(self, result):
        """Обеспечение своё в каждой строке: 0.5% от стоимости газа этой строки."""
        for scenario in result.scenarios:
            assert scenario.collateral_fee == pytest.approx(
                scenario.purchase * 0.005, abs=0.01
            )
            assert scenario.freight == pytest.approx(
                scenario.freight_volume + scenario.collateral_fee, abs=0.01
            )

    def test_summary(self, result):
        """Сводка: лучший, экономия против худшего, потери разжатия."""
        summary = result.summary
        assert summary is not None
        assert (summary.best.hub_key, summary.best.form, summary.best.side) == (
            "amarr", GasForm.COMPRESSED, OrderSide.BUY,
        )
        assert summary.savings_vs_worst == pytest.approx(138_614_620, abs=0.01)
        assert summary.loss_units == 6_180
        assert summary.loss_pct == pytest.approx(6_180 / 56_180 * 100, abs=1e-9)

    def test_delta_pct(self, result):
        """У первой строки прочерк, у остальных — отставание от лучшей."""
        first, second = result.scenarios[0], result.scenarios[1]
        assert first.delta_pct is None
        assert second.delta_pct == pytest.approx(
            (second.isk_per_unit / first.isk_per_unit - 1) * 100
        )

    def test_breakeven_only_for_jita(self, result):
        """Обе sell-цены заполнены только у Jita: 0.89 * 5500 - 250 = 4645."""
        assert len(result.breakevens) == 1
        breakeven = result.breakevens[0]
        assert breakeven.hub_key == "jita"
        assert breakeven.price == pytest.approx(4645, abs=0.01)

    def test_no_warnings_and_no_skips(self, result):
        """В контрольном примере нет ни выбросов цен, ни пропущенных хабов."""
        assert result.skipped_hubs == ()
        for scenario in result.scenarios:
            assert scenario.warnings == ()


class TestCollateral:
    """Обеспечение — процент от стоимости газа, надбавка к доставке (DOMAIN §4)."""

    def test_fee_per_scenario(self):
        """Надбавка считается от стоимости газа каждой строки.

        N=1000, eta=0.89 → qty = ceil(1123.6) = 1124.
        сжатый sell @2000: закупка 2 248 000, обеспечение 11 240,
            объём 562 м³, плата за объём 281 000 → итого 2 540 240.
        сырой buy @1800: закупка 1 800 000, брокер 27 000, обеспечение 9 000,
            объём 5000 м³, плата за объём 2 500 000 → итого 4 336 000.
        """
        result = calc.build_scenarios(
            control_input(n_units=1000),
            {"jita": HubPrices(freight_rate=500, compressed_sell=2000, raw_buy=1800)},
        )
        assert result.compressed_qty == 1124
        by_form = {s.form: s for s in result.scenarios}

        compressed = by_form[GasForm.COMPRESSED]
        assert compressed.collateral_fee == pytest.approx(11_240, abs=0.01)
        assert compressed.freight == pytest.approx(292_240, abs=0.01)
        assert compressed.total == pytest.approx(2_540_240, abs=0.01)
        assert compressed.isk_per_unit == pytest.approx(2540.24, abs=0.01)

        raw = by_form[GasForm.RAW]
        assert raw.collateral_fee == pytest.approx(9_000, abs=0.01)
        assert raw.broker == pytest.approx(27_000, abs=0.01)
        assert raw.total == pytest.approx(4_336_000, abs=0.01)

    def test_base_excludes_broker(self):
        """База — чистая стоимость газа. Брокерская комиссия в неё не входит.

        Ловушка: 1 800 000 * 0.005 = 9 000, а с комиссией было бы
        (1 800 000 + 27 000) * 0.005 = 9 135.
        """
        result = calc.build_scenarios(
            control_input(n_units=1000),
            {"jita": HubPrices(freight_rate=500, raw_buy=1800)},
        )
        row = result.scenarios[0]
        assert row.broker > 0  # это buy-сценарий, комиссия есть
        assert row.collateral_fee == pytest.approx(9_000, abs=0.01)

    def test_zero_pct_leaves_only_volume_charge(self):
        """Обеспечение 0% — доставка равна плате за объём."""
        result = calc.build_scenarios(
            control_input(collateral_pct=0.0), CONTROL_PRICES
        )
        for scenario in result.scenarios:
            assert scenario.collateral_fee == 0.0
            assert scenario.freight == pytest.approx(scenario.freight_volume, abs=0.01)

    def test_pct_can_change_order(self):
        """Надбавка пропорциональна стоимости газа и потому влияет на сортировку.

        Это главное отличие от старой модели с фиксированной суммой: та сдвигала
        все строки на одну величину и порядок сохраняла.

        Jita:  дорогой газ (3000), дешёвая доставка (10)  → закупка 168 540 000
        Amarr: газ дешевле (2600), доставка дорогая (900) → закупка 146 068 000

        При 0%  Jita впереди: 168 820 900 против 171 349 000.
        При 30% дорогой газ штрафуется сильнее, и вперёд выходит Amarr:
        219 382 900 против 215 169 400.
        """
        prices = {
            "jita": HubPrices(freight_rate=10, compressed_sell=3000),
            "amarr": HubPrices(freight_rate=900, compressed_sell=2600),
        }
        without = calc.build_scenarios(control_input(collateral_pct=0.0), prices)
        assert without.scenarios[0].hub_key == "jita"
        assert without.scenarios[0].total == pytest.approx(168_820_900, abs=0.01)

        heavy = calc.build_scenarios(control_input(collateral_pct=0.30), prices)
        assert heavy.scenarios[0].hub_key == "amarr"
        assert heavy.scenarios[0].total == pytest.approx(215_169_400, abs=0.01)

    def test_negative_pct(self):
        """Отрицательное обеспечение — ошибка."""
        with pytest.raises(ValueError):
            calc.build_scenarios(control_input(collateral_pct=-0.01), CONTROL_PRICES)


class TestMissingData:
    """Пропуски: пустые цены, хаб без ставки, пустой ввод."""

    def test_empty_price_skips_scenario(self):
        """Пустая цена — сценарий просто отсутствует: у Amarr в контрольном
        примере заполнены две ячейки — и строк от него ровно две."""
        result = calc.build_scenarios(control_input(), CONTROL_PRICES)
        amarr_rows = [s for s in result.scenarios if s.hub_key == "amarr"]
        assert len(amarr_rows) == 2
        assert {s.form for s in amarr_rows} == {GasForm.COMPRESSED}

    def test_hub_without_rate_drops_entirely(self):
        """Хаб без ставки доставки выпадает целиком, даже с заполненными ценами."""
        prices = dict(CONTROL_PRICES)
        prices["jita"] = HubPrices(
            freight_rate=None, raw_sell=3000, raw_buy=2620,
            compressed_sell=2750, compressed_buy=2400,
        )
        result = calc.build_scenarios(control_input(), prices)
        assert result.skipped_hubs == ("jita",)
        assert all(s.hub_key != "jita" for s in result.scenarios)
        assert len(result.scenarios) == 6  # было 10, минус 4 сценария Jita
        assert result.breakevens == ()  # обе sell-цены были только у Jita

    def test_hub_with_rate_but_no_prices(self):
        """Ставка есть, цен нет: строк нет, но и в пропущенные хаб не попадает."""
        result = calc.build_scenarios(
            control_input(), {"rens": HubPrices(freight_rate=900)}
        )
        assert result.scenarios == ()
        assert result.skipped_hubs == ()
        assert result.summary is None

    def test_completely_empty_input(self):
        """Совсем без данных: пустой результат, сводки нет, расчёт не падает."""
        result = calc.build_scenarios(
            control_input(),
            {hub: HubPrices() for hub in ("jita", "amarr", "dodixie", "rens", "hek")},
        )
        assert result.scenarios == ()
        assert result.summary is None
        assert result.skipped_hubs == ("jita", "amarr", "dodixie", "rens", "hek")

    @pytest.mark.parametrize("bad_price", [0, -100])
    def test_non_positive_price_raises(self, bad_price):
        """Нулевая или отрицательная цена — ошибка, а не молчаливый пропуск."""
        with pytest.raises(ValueError):
            calc.build_scenarios(
                control_input(),
                {"jita": HubPrices(freight_rate=500, raw_sell=bad_price)},
            )

    def test_negative_rate_raises(self):
        """Отрицательная ставка доставки — ошибка."""
        with pytest.raises(ValueError):
            calc.build_scenarios(
                control_input(),
                {"jita": HubPrices(freight_rate=-1, raw_sell=3000)},
            )

    def test_negative_broker_raises(self):
        """Отрицательная брокерская комиссия — ошибка."""
        with pytest.raises(ValueError):
            calc.build_scenarios(control_input(broker_fee=-0.01), CONTROL_PRICES)


class TestOutlierWarning:
    """Пометка «цена сильно выбивается» (SPEC §6)."""

    def test_outlier_flagged(self):
        """5000 против медианы 1050 (в 4.8 раза) — выброс. 1000 против медианы
        3050 (меньше трети) — тоже. 1100 против медианы 3000 — в норме."""
        result = calc.build_scenarios(
            control_input(),
            {
                "jita": HubPrices(freight_rate=500, compressed_sell=1000),
                "amarr": HubPrices(freight_rate=700, compressed_sell=1100),
                "dodixie": HubPrices(freight_rate=600, compressed_sell=5000),
            },
        )
        flagged = {s.hub_key for s in result.scenarios if WarningCode.PRICE_OUTLIER in s.warnings}
        assert flagged == {"jita", "dodixie"}

    def test_single_hub_never_flagged(self):
        """Один хаб сравнивать не с чем — предупреждения нет."""
        result = calc.build_scenarios(
            control_input(),
            {"jita": HubPrices(freight_rate=500, compressed_sell=999_999)},
        )
        assert result.scenarios[0].warnings == ()


class TestSellOnlyFilter:
    """Фильтр «только sell» (SPEC §5.4): buy-ордер — заявка, а не покупка."""

    def test_buy_scenarios_disappear(self):
        result = calc.build_scenarios(control_input(sell_only=True), CONTROL_PRICES)
        assert all(s.side is OrderSide.SELL for s in result.scenarios)
        assert len(result.scenarios) == 6  # из десяти строк контрольного примера

    def test_sell_rows_are_unchanged(self):
        """Фильтр только убирает строки, но не трогает арифметику оставшихся."""
        full = calc.build_scenarios(control_input(), CONTROL_PRICES)
        filtered = calc.build_scenarios(control_input(sell_only=True), CONTROL_PRICES)

        full_sells = {
            (s.hub_key, s.form): s.total for s in full.scenarios if s.side is OrderSide.SELL
        }
        for row in filtered.scenarios:
            assert row.total == pytest.approx(full_sells[(row.hub_key, row.form)])

    def test_summary_recomputed_on_filtered_rows(self):
        """Лучший вариант считается по тому, что осталось на экране."""
        result = calc.build_scenarios(control_input(sell_only=True), CONTROL_PRICES)
        # в полном примере лучшим был Amarr сжатый buy по 2 742.71
        assert result.summary.best.hub_key == "amarr"
        assert result.summary.best.side is OrderSide.SELL
        assert result.summary.best.isk_per_unit == pytest.approx(2990.46, abs=0.01)
        assert result.summary.best.delta_pct is None

    def test_breakevens_survive_the_filter(self):
        """Точки безубыточности и так считаются по sell-ценам — фильтр им не помеха."""
        result = calc.build_scenarios(control_input(sell_only=True), CONTROL_PRICES)
        assert len(result.breakevens) == 1
        assert result.breakevens[0].price == pytest.approx(4645, abs=0.01)

    def test_only_buy_prices_gives_empty_result(self):
        """Заполнены только buy — показывать нечего, но и падать не за что."""
        result = calc.build_scenarios(
            control_input(sell_only=True),
            {"jita": HubPrices(freight_rate=500, compressed_buy=2400)},
        )
        assert result.scenarios == ()
        assert result.summary is None

    def test_loss_stats_follow_the_filter(self):
        """Потери на разжатии показываются, пока в выдаче есть сжатые сценарии."""
        result = calc.build_scenarios(
            control_input(sell_only=True),
            {"jita": HubPrices(freight_rate=500, raw_sell=3000, compressed_buy=2400)},
        )
        # остался только сырой sell — сжатых строк нет, значит и потерь показывать нечего
        assert result.summary.loss_units is None


class TestShallowBookWarning:
    """Пометка «в стакане меньше, чем нужно» (SPEC §6).

    Глубина известна только для цен из ESI; при ручном вводе depth=None
    и предупреждений быть не должно.
    """

    def test_shallow_flagged_with_available_qty(self):
        """Нужно 56 180 сжатого, в стакане 20 000 — флаг и само число."""
        result = calc.build_scenarios(
            control_input(),
            {
                "jita": HubPrices(
                    freight_rate=500,
                    compressed_sell=2750,
                    depth=HubDepth(compressed_sell=20_000),
                )
            },
        )
        row = result.scenarios[0]
        assert WarningCode.SHALLOW_BOOK in row.warnings
        assert row.available_qty == 20_000
        assert row.qty == 56_180

    def test_deep_book_not_flagged(self):
        result = calc.build_scenarios(
            control_input(),
            {
                "jita": HubPrices(
                    freight_rate=500,
                    compressed_sell=2750,
                    depth=HubDepth(compressed_sell=56_180),
                )
            },
        )
        assert result.scenarios[0].warnings == ()  # ровно впритык — хватает

    def test_depth_is_per_cell(self):
        """Мелкий buy-стакан не пачкает предупреждением соседний sell."""
        result = calc.build_scenarios(
            control_input(),
            {
                "jita": HubPrices(
                    freight_rate=500,
                    compressed_sell=2750,
                    compressed_buy=2400,
                    depth=HubDepth(compressed_sell=999_999, compressed_buy=100),
                )
            },
        )
        flagged = {
            s.side for s in result.scenarios if WarningCode.SHALLOW_BOOK in s.warnings
        }
        assert flagged == {OrderSide.BUY}

    def test_raw_depth_compared_with_n(self):
        """У сырого газа потребность — ровно N, без запаса на разжатие."""
        result = calc.build_scenarios(
            control_input(),
            {
                "jita": HubPrices(
                    freight_rate=500,
                    raw_sell=3000,
                    depth=HubDepth(raw_sell=50_000),
                )
            },
        )
        assert result.scenarios[0].warnings == ()

    def test_manual_input_has_no_depth(self):
        """Без подтяжки depth=None — предупреждение невозможно в принципе."""
        result = calc.build_scenarios(control_input(), CONTROL_PRICES)
        assert all(s.available_qty is None for s in result.scenarios)
        assert all(WarningCode.SHALLOW_BOOK not in s.warnings for s in result.scenarios)

    def test_warnings_can_combine(self):
        """Выброс и нехватка глубины — независимые пометки на одной строке."""
        result = calc.build_scenarios(
            control_input(),
            {
                "jita": HubPrices(
                    freight_rate=500,
                    compressed_sell=50_000,
                    depth=HubDepth(compressed_sell=1),
                ),
                "amarr": HubPrices(freight_rate=700, compressed_sell=2300),
                "dodixie": HubPrices(freight_rate=600, compressed_sell=2400),
            },
        )
        jita = next(s for s in result.scenarios if s.hub_key == "jita")
        assert set(jita.warnings) == {WarningCode.PRICE_OUTLIER, WarningCode.SHALLOW_BOOK}

    def test_negative_depth_raises(self):
        with pytest.raises(ValueError):
            calc.build_scenarios(
                control_input(),
                {
                    "jita": HubPrices(
                        freight_rate=500,
                        compressed_sell=2750,
                        depth=HubDepth(compressed_sell=-1),
                    )
                },
            )


class TestCatalog:
    """Справочники: газы из gases.json и хабы из констант."""

    def test_gas_count(self):
        """В справочнике 27 типов газа: 9 фуллеренов, 8 Mykoserocin, 10 Cytoserocin."""
        assert len(catalog.gases()) == 27

    def test_control_gas(self):
        """Fullerite-C320 — газ контрольного примера: 5 м³ сырой, 0.5 м³ сжатый."""
        gas = catalog.gas_by_key("fullerite_c320")
        assert gas.name == "Fullerite-C320"
        assert gas.volume_raw == 5.0
        assert gas.volume_compressed == 0.5

    def test_families(self):
        """Три семейства: 9 фуллеренов, 8 Mykoserocin, 10 Cytoserocin.

        Десять цветов Cytoserocin — не опечатка: сверено с SDE, см. DOMAIN §2.
        """
        families: dict[str, int] = {}
        for gas in catalog.gases():
            families[gas.family] = families.get(gas.family, 0) + 1
        assert families == {"fullerite": 9, "mykoserocin": 8, "cytoserocin": 10}

    def test_compressed_type_ids_unique(self):
        """type_id сжатых типов заполнены и не дублируются."""
        ids = [gas.compressed_type_id for gas in catalog.gases()]
        assert all(isinstance(i, int) for i in ids)
        assert len(set(ids)) == len(ids)

    def test_raw_type_ids_filled_and_unique(self):
        """raw_type_id заполнены из SDE (tools/build_gases.py) и не дублируются."""
        ids = [gas.raw_type_id for gas in catalog.gases()]
        assert all(isinstance(i, int) for i in ids)
        assert len(set(ids)) == len(ids)

    def test_raw_and_compressed_ids_do_not_overlap(self):
        """Сырой и сжатый — разные типы: пересечения быть не может."""
        raw = {gas.raw_type_id for gas in catalog.gases()}
        compressed = {gas.compressed_type_id for gas in catalog.gases()}
        assert raw.isdisjoint(compressed)

    def test_control_example_gas_ids(self):
        """Fullerite-C320 из контрольного примера: пара 30377 / 62406 (SDE)."""
        gas = catalog.gas_by_key("fullerite_c320")
        assert (gas.raw_type_id, gas.compressed_type_id) == (30377, 62406)

    def test_unknown_gas(self):
        """Неизвестный ключ газа — KeyError."""
        with pytest.raises(KeyError):
            catalog.gas_by_key("veldspar")

    def test_hub_order(self):
        """Хабы идут в порядке из DOMAIN §3."""
        assert [hub.key for hub in catalog.hubs()] == [
            "jita", "amarr", "dodixie", "rens", "hek",
        ]

    def test_jita_ids(self):
        """Выборочная сверка ID с DOMAIN §3."""
        jita = catalog.hub_by_key("jita")
        assert jita.region_id == 10000002
        assert jita.station_id == 60003760

    def test_unknown_hub(self):
        """Неизвестный ключ хаба — KeyError."""
        with pytest.raises(KeyError):
            catalog.hub_by_key("thera")
