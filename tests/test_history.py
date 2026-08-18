"""Тесты опоры на историю сделок (ROADMAP, этап 11.4).

Правила — docs/ESI.md §5.4 и §5.5. Сети и базы здесь нет: на вход идут дни,
на выход цифры и признаки.
"""

from datetime import date, timedelta

import pytest

from app.services.history import HistoryDay, HistoryStats, summarize

TODAY = date(2026, 8, 19)


def days(
    *averages: float, volume: int = 100_000, spread: float = 0.05, start: date | None = None
) -> list[HistoryDay]:
    """Дни истории подряд, заканчивая вчерашним: сегодняшнего в истории нет."""
    last = (start or TODAY) - timedelta(days=1)
    return [
        HistoryDay(
            date=last - timedelta(days=len(averages) - 1 - i),
            average=avg,
            highest=avg * (1 + spread),
            lowest=avg * (1 - spread),
            volume=volume,
        )
        for i, avg in enumerate(averages)
    ]


class TestSummarize:
    def test_reference_is_median_not_mean(self):
        """Один сумасшедший день не должен утащить опору за собой."""
        stats = summarize(days(3000, 3000, 3000, 3000, 90_000), today=TODAY)
        assert stats.reference == 3000

    def test_zero_volume_days_ignored(self):
        """Цена дня без сделок ничего не подтверждает."""
        history = days(3000, 3000, 3000) + [
            HistoryDay(date=TODAY - timedelta(days=1), average=99, highest=99, lowest=99, volume=0)
        ]
        stats = summarize(history, today=TODAY)
        assert stats.reference == 3000
        assert stats.traded_days == 3

    def test_range_covers_the_spread(self):
        stats = summarize(days(3000, 3000, 3000, spread=0.1), today=TODAY)
        assert stats.lowest == pytest.approx(2700)
        assert stats.highest == pytest.approx(3300)

    def test_window_cuts_old_days(self):
        old = days(9999, start=TODAY - timedelta(days=30))
        stats = summarize(old + days(3000, 3000, 3000), window=7, today=TODAY)
        assert stats.reference == 3000
        assert stats.traded_days == 3

    def test_daily_volume_divides_by_window(self):
        stats = summarize(days(3000, 3000, volume=70_000), window=7, today=TODAY)
        assert stats.daily_volume == pytest.approx(20_000)

    def test_empty_history_is_not_usable(self):
        assert summarize([]).usable is False

    def test_stale_history_is_not_usable(self):
        """Данные месячной давности опорой быть не могут."""
        stats = summarize(days(3000, 3000, 3000, start=TODAY - timedelta(days=30)), today=TODAY)
        assert stats.usable is False

    def test_too_few_traded_days_is_not_usable(self):
        assert summarize(days(3000, 3000), today=TODAY).usable is False

    def test_yesterday_is_fresh(self):
        """История обновляется раз в сутки — вчерашняя это норма, не устаревание."""
        assert summarize(days(3000, 3000, 3000), today=TODAY).usable is True


class TestBand:
    """Жёсткий коридор: что вне — выбрасывается из книги (ESI §5.4)."""

    def setup_method(self):
        self.stats = summarize(days(6000, 6000, 6000, 5900), today=TODAY)

    def test_scam_buy_is_out(self):
        """Тот самый ордер в Rens: 1 ISK при реальной цене 6000."""
        assert self.stats.out_of_band(1.0) is True

    def test_wall_is_out(self):
        assert self.stats.out_of_band(6_000_000.0) is True

    def test_honest_double_move_stays_in(self):
        """Цена газа спокойно ходит вдвое за неделю — это не мусор."""
        assert self.stats.out_of_band(12_000.0) is False
        assert self.stats.out_of_band(3_000.0) is False

    def test_band_is_asymmetric(self):
        """Снизу строго, сверху свободно — вывод из замера 19.08.2026.

        Мусор снизу даёт неправильный совет: buy на 1 ISK делает хаб
        «выгоднейшим местом выставиться». Завышенный sell к неправильному
        совету не ведёт — строка просто проигрывает сравнение, — а честные
        ордера в несколько опорных цен на тонких газах обычное дело.
        """
        assert self.stats.out_of_band(6000 * 5) is False   # ×5 сверху — оставляем
        assert self.stats.out_of_band(6000 * 12) is True   # ×12 — «стена»
        assert self.stats.out_of_band(6000 / 5) is True    # ×0.2 снизу — режем

    def test_no_history_no_filter(self):
        """Нет опоры — фильтр не применяется, поведение как раньше."""
        assert summarize([]).out_of_band(1.0) is False
        assert summarize([]).band() is None


class TestUnconfirmed:
    """Мягкая пометка: строка помечается, но ничего не пересчитывается."""

    def setup_method(self):
        # Реальные сделки шли в диапазоне 5700 … 6300
        self.stats = summarize(days(6000, 6000, 6000, spread=0.05), today=TODAY)

    def test_scam_price_is_flagged(self):
        assert self.stats.unconfirmed(1.0) is True

    def test_normal_spread_is_not_flagged(self):
        """Buy ниже средней, sell выше — это спред, а не неликвид."""
        assert self.stats.unconfirmed(5_800.0) is False
        assert self.stats.unconfirmed(6_200.0) is False

    def test_price_below_range_but_within_warn_is_not_flagged(self):
        """Порог мягкий намеренно: пометка на каждой строке — это шум."""
        assert self.stats.unconfirmed(5_700 / 1.4) is False

    def test_far_below_range_is_flagged(self):
        assert self.stats.unconfirmed(5_700 / 2) is True

    def test_no_history_no_flag(self):
        assert summarize([]).unconfirmed(1.0) is False


class TestVolume:
    """Цена может быть настоящей, а объём всё равно не набрать."""

    def setup_method(self):
        # Торговали три дня из семи по 200 юнитов: 600 за окно, ~86 в сутки.
        # Делится именно на длину окна: рынок, стоявший четыре дня из семи,
        # в среднем и принимает меньше.
        self.stats = summarize(days(3000, 3000, 3000, volume=200), window=7, today=TODAY)

    def test_more_than_window_volume(self):
        """10 000 при обороте 200 в сутки — за неделю столько не продали."""
        assert self.stats.short_of_volume(10_000) is True

    def test_more_than_daily_volume(self):
        """Меньше недельного, но больше суточного: набирать несколько дней."""
        assert self.stats.slow_for_volume(500) is True
        assert self.stats.short_of_volume(500) is False

    def test_small_order_is_fine(self):
        assert self.stats.short_of_volume(50) is False
        assert self.stats.slow_for_volume(50) is False

    def test_no_history_no_claims(self):
        """Без истории мы про объём ничего не знаем и молчим."""
        assert summarize([]).short_of_volume(10_000) is False
        assert summarize([]).slow_for_volume(10_000) is False


class TestDefaults:
    def test_empty_stats_are_harmless(self):
        """Пустой свод не должен ломать вызывающий код ни в одной точке."""
        stats = HistoryStats()
        assert stats.usable is False
        assert stats.daily_volume == 0.0
        assert stats.band() is None
        assert stats.out_of_band(1.0) is False
        assert stats.unconfirmed(1.0) is False
