"""Опорная цена и коридор по истории реальных сделок.

Все правила — docs/ESI.md §5.4 и §5.5. Смысл модуля в одном: стакан показывает
хотелки продавцов, а история — то, что рынок реально принял. Без внешней опоры
отсечение мусора врёт вместе с книгой: когда мусора больше половины, медиана
книги переезжает к нему и выбрасывает настоящий ордер.

Модуль чистый: ни HTTP, ни базы, ни Flask. На вход — дни истории, на выход —
цифры и признаки. Поэтому он целиком покрывается тестами без единого запроса.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from app.core.constants import (
    HISTORY_BAND_HIGH_FACTOR,
    HISTORY_BAND_LOW_FACTOR,
    HISTORY_MAX_AGE_DAYS,
    HISTORY_MIN_TRADED_DAYS,
    HISTORY_REFERENCE_DAYS,
    HISTORY_WARN_FACTOR,
)


@dataclass(frozen=True, slots=True)
class HistoryDay:
    """Один день реальных сделок по паре «регион + тип» (ESI §5.2)."""

    date: date
    average: float
    highest: float
    lowest: float
    volume: int


@dataclass(frozen=True, slots=True)
class HistoryStats:
    """Свод по окну: то, чем можно проверять цену.

    reference — опорная цена, медиана дневных average по дням с оборотом.
    lowest / highest — диапазон реальных сделок за окно. Именно диапазон, а не
    одна цена: история мешает обе стороны сделки, buy честно стоит ниже
    средней, sell выше, и сравнивать их с одним числом значит ловить нормальный
    спред вместо мусора (ESI §5.5).
    """

    reference: float | None = None
    lowest: float | None = None
    highest: float | None = None
    volume: int = 0
    traded_days: int = 0
    window_days: int = HISTORY_REFERENCE_DAYS
    last_day: date | None = None
    fresh: bool = False

    @property
    def usable(self) -> bool:
        """Можно ли на это опираться.

        Два условия сразу: данные свежие и в окне есть хотя бы несколько дней
        с ненулевым оборотом. Не выполнено — фильтр по истории не применяется
        и работает прежнее правило по медиане книги. Для редкого газа в мелком
        хабе это нормальное состояние, а не авария.
        """
        return (
            self.fresh
            and self.reference is not None
            and self.traded_days >= HISTORY_MIN_TRADED_DAYS
        )

    @property
    def daily_volume(self) -> float:
        """Среднесуточный оборот по окну — с ним сравнивается потребность."""
        return self.volume / self.window_days if self.window_days else 0.0

    def band(
        self,
        low_factor: float = HISTORY_BAND_LOW_FACTOR,
        high_factor: float = HISTORY_BAND_HIGH_FACTOR,
    ) -> tuple[float, float] | None:
        """Жёсткий коридор вокруг опорной цены: что вне — выбрасывается.

        Коридор асимметричный. Снизу строго: мусорный buy на 1 ISK делает хаб
        «выгоднейшим местом выставиться» и стоит человеку денег. Сверху
        свободнее: завышенный sell к неправильному совету не ведёт, строка
        просто проигрывает сравнение, а на тонких газах честные ордера
        в несколько опорных цен — обычное дело.
        """
        if not self.usable or self.reference is None:
            return None
        return self.reference / low_factor, self.reference * high_factor

    def out_of_band(
        self,
        price: float,
        low_factor: float = HISTORY_BAND_LOW_FACTOR,
        high_factor: float = HISTORY_BAND_HIGH_FACTOR,
    ) -> bool:
        """Цена настолько далека от реальных сделок, что ордер не в рынке."""
        bounds = self.band(low_factor, high_factor)
        if bounds is None:
            return False
        low, high = bounds
        return not (low <= price <= high)

    def unconfirmed(self, price: float, factor: float = HISTORY_WARN_FACTOR) -> bool:
        """Мягкая пометка: сделок по такой цене за окно не было (ESI §5.5).

        Считается от диапазона, а не от опорной цены, и работает для любой
        цены — включая введённую руками, к которой жёсткий фильтр не
        применяется вовсе.
        """
        if not self.usable or self.lowest is None or self.highest is None:
            return False
        if self.lowest <= 0:
            return price > self.highest * factor
        return price < self.lowest / factor or price > self.highest * factor

    def short_of_volume(self, needed: int) -> bool:
        """За всё окно продали меньше, чем нужно человеку: набрать нереально."""
        return self.usable and needed > self.volume

    def slow_for_volume(self, needed: int) -> bool:
        """Объём больше суточного оборота: набирать придётся несколько дней."""
        return self.usable and not self.short_of_volume(needed) and needed > self.daily_volume


def summarize(
    days: Sequence[HistoryDay],
    *,
    window: int = HISTORY_REFERENCE_DAYS,
    max_age: int = HISTORY_MAX_AGE_DAYS,
    today: date | None = None,
) -> HistoryStats:
    """Свод по последним window дням.

    Опорная цена — медиана, а не среднее: один сумасшедший день не должен
    утащить опору за собой. Дни с нулевым оборотом в опоре не участвуют —
    цена дня, в который не было ни одной сделки, ничего не подтверждает.

    Свежесть считается по последнему дню окна, а не по последнему дню
    с оборотом: история обновляется раз в сутки и сегодняшнего дня в ней нет
    никогда, поэтому «вчерашняя» — это норма (ESI §5.3).
    """
    if not days:
        return HistoryStats(window_days=window)

    now = today or max(day.date for day in days)
    cutoff = now - timedelta(days=window)
    window_days = [day for day in days if day.date > cutoff]
    if not window_days:
        return HistoryStats(window_days=window)

    last_day = max(day.date for day in window_days)
    traded = [day for day in window_days if day.volume > 0]

    return HistoryStats(
        reference=median(day.average for day in traded) if traded else None,
        lowest=min(day.lowest for day in traded) if traded else None,
        highest=max(day.highest for day in traded) if traded else None,
        volume=sum(day.volume for day in window_days),
        traded_days=len(traded),
        window_days=window,
        last_day=last_day,
        fresh=(now - last_day).days <= max_age,
    )
