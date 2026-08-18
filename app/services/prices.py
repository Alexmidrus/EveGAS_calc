"""Чтение собранных цен из базы (ROADMAP, этап 8).

Приложение к ESI не обращается: сюда приходит запрос пользователя, отсюда
уходит запрос в базу. Всё, что связано с сетью, живёт в сборщике.

Один поход в базу отдаёт срезы сразу по всем хабам и обеим формам газа —
десять троек на газ. Ходить за каждой отдельно значило бы десять запросов
на отрисовку страницы.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.constants import HISTORY_REFERENCE_DAYS
from app.core.models import GasForm, Hub, OrderSide
from app.db import CollectionRun, MarketHistory, MarketSnapshot, load_ladder, session_scope, utcnow
from app.services.history import HistoryDay, HistoryStats, borrow, summarize
from app.services.orderbook import Quote, quote_from_ladder


@dataclass(frozen=True, slots=True)
class StoredQuote:
    """Цена одной ячейки сетки, посчитанная из сохранённой лестницы.

    history — свод реальных сделок по этой паре «хаб + форма». Он же служил
    опорой при отсечении мусора, он же показывается пользователю в колонках
    «Сделки» и «Продано» (ESI §5.5). Считать его второй раз для показа было бы
    честным способом разойтись с тем, по чему считали цену.
    """

    quote: Quote
    collected_at: datetime
    history: HistoryStats = field(default_factory=HistoryStats)

    @property
    def price(self) -> float | None:
        return self.quote.price

    @property
    def dropped(self) -> int:
        """Сколько уровней выброшено как не в рынке — это видно пользователю."""
        return self.quote.dropped

    @property
    def no_liquid_orders(self) -> bool:
        """Книга была, но вся оказалась вне рынка.

        Не то же самое, что «данных нет» (срез не собран) и не то же самое,
        что «стакан пуст» (ордеров не было вовсе). Откатывать фильтр нельзя:
        откат вернул бы ровно тот мусор, ради которого он и написан (ESI §5.4).
        """
        return self.quote.price is None and self.quote.dropped > 0

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or utcnow()) - self.collected_at


@dataclass(frozen=True, slots=True)
class PriceBook:
    """Всё, что база знает про выбранный газ прямо сейчас.

    quotes — по ключу (хаб, форма, сторона). Отсутствие ключа означает
    «данных нет», и это не то же самое, что цена ноль: пустую ячейку
    интерфейс обязан показать пустой и объяснить почему.
    """

    quotes: Mapping[tuple[str, GasForm, OrderSide], StoredQuote]
    missing_hubs: tuple[str, ...] = ()
    collected_at: datetime | None = None  # самый свежий срез из использованных
    oldest_at: datetime | None = None     # самый старый — по нему считается устаревание
    error: str | None = None              # база недоступна; цен нет не потому, что их не собрали

    def get(self, hub_key: str, form: GasForm, side: OrderSide) -> StoredQuote | None:
        return self.quotes.get((hub_key, form, side))

    @property
    def empty(self) -> bool:
        return not self.quotes

    def age(self, now: datetime | None = None) -> timedelta | None:
        """Возраст самых старых данных: именно он решает, устарела ли сетка."""
        if self.oldest_at is None:
            return None
        return (now or utcnow()) - self.oldest_at

    def is_stale(self, max_age: timedelta, now: datetime | None = None) -> bool:
        age = self.age(now)
        return age is not None and age > max_age


def _latest_rows(session: Session, type_ids: Sequence[int]) -> list[MarketSnapshot]:
    """Последний срез на каждую тройку (хаб, тип, сторона).

    Группировка по максимуму времени, а не выборка всех строк с отбором
    в Python: срезов хранится по три на тройку, и таскать лишние незачем.
    """
    if not type_ids:
        return []
    newest = (
        select(
            MarketSnapshot.hub_key,
            MarketSnapshot.type_id,
            MarketSnapshot.side,
            func.max(MarketSnapshot.collected_at).label("collected_at"),
        )
        .where(MarketSnapshot.type_id.in_(type_ids))
        .group_by(MarketSnapshot.hub_key, MarketSnapshot.type_id, MarketSnapshot.side)
        .subquery()
    )
    return list(
        session.scalars(
            select(MarketSnapshot).join(
                newest,
                (MarketSnapshot.hub_key == newest.c.hub_key)
                & (MarketSnapshot.type_id == newest.c.type_id)
                & (MarketSnapshot.side == newest.c.side)
                & (MarketSnapshot.collected_at == newest.c.collected_at),
            )
        )
    )


def _history_stats(
    session: Session, region_ids: Sequence[int], type_ids: Sequence[int]
) -> dict[tuple[int, int], HistoryStats]:
    """Свод реальных сделок по каждой паре «регион + тип».

    Берутся только дни окна, а не все 90 хранимых: опоре нужна неделя, а тащить
    в память лишнее на каждую отрисовку страницы незачем.

    Ключ регионный, потому что история регионная: разбивки по станциям
    у эндпоинта нет (ESI §5.2).
    """
    if not region_ids or not type_ids:
        return {}
    cutoff = utcnow().date() - timedelta(days=HISTORY_REFERENCE_DAYS)
    rows = session.execute(
        select(
            MarketHistory.region_id,
            MarketHistory.type_id,
            MarketHistory.date,
            MarketHistory.average,
            MarketHistory.highest,
            MarketHistory.lowest,
            MarketHistory.volume,
        ).where(
            MarketHistory.region_id.in_(region_ids),
            MarketHistory.type_id.in_(type_ids),
            MarketHistory.date > cutoff,
        )
    ).all()

    days: dict[tuple[int, int], list[HistoryDay]] = defaultdict(list)
    for region_id, type_id, when, average, highest, lowest, volume in rows:
        days[(int(region_id), int(type_id))].append(
            HistoryDay(
                date=when,
                average=float(average),
                highest=float(highest),
                lowest=float(lowest),
                volume=int(volume),
            )
        )
    stats = {key: summarize(value) for key, value in days.items()}
    return _fill_gaps(stats, region_ids, type_ids)


def _fill_gaps(
    stats: Mapping[tuple[int, int], HistoryStats],
    region_ids: Sequence[int],
    type_ids: Sequence[int],
) -> dict[tuple[int, int], HistoryStats]:
    """Подпирает пары без своей истории соседними регионами (history.borrow).

    Пара без истории — это пара без коридора, а значит книга, в которую мусор
    проходит как есть. Именно так buy-ордер на 33 ISK по сжатому Fullerite-C84
    в Rens оказался лучшим вариантом в таблице.
    """
    filled = dict(stats)
    for type_id in type_ids:
        neighbours = [
            value
            for (region, tid), value in stats.items()
            if tid == type_id and value.usable
        ]
        if not neighbours:
            continue
        for region_id in region_ids:
            current = filled.get((region_id, type_id))
            if current is not None and current.usable:
                continue
            spare = borrow(
                [
                    value
                    for (region, tid), value in stats.items()
                    if tid == type_id and region != region_id
                ]
            )
            if spare.usable:
                filled[(region_id, type_id)] = spare
    return filled


def load_price_book(
    engine: Engine,
    hubs: Sequence[Hub],
    type_ids: Mapping[GasForm, int],
    needed: Mapping[GasForm, int],
) -> PriceBook:
    """Считает цены по всем хабам для выбранного газа.

    type_ids — известные ID форм газа: если ID неизвестен, формы просто нет
    в словаре, и выдумывать её нельзя. needed — сколько юнитов нужно каждой
    формы; именно под этот объём и считается средневзвешенная цена.
    """
    if not type_ids:
        return PriceBook(quotes={})

    by_type = {type_id: form for form, type_id in type_ids.items()}
    region_of = {hub.key: hub.region_id for hub in hubs}
    try:
        with session_scope(engine) as session:
            rows = _latest_rows(session, list(by_type))
            payload = [
                (r.hub_key, int(r.type_id), r.side, r.ladder, r.collected_at) for r in rows
            ]
            history = _history_stats(
                session, sorted(set(region_of.values())), list(by_type)
            )
    except SQLAlchemyError as exc:
        # Расчёт по ручным ценам обязан работать всегда: недоступная база —
        # повод сказать об этом вслух, а не отдать пользователю пятисотку
        return PriceBook(quotes={}, error=_short_error(exc))

    quotes: dict[tuple[str, GasForm, OrderSide], StoredQuote] = {}
    seen_hubs: set[str] = set()
    for hub_key, type_id, side_value, ladder, collected_at in payload:
        form = by_type.get(type_id)
        if form is None:
            continue
        side = OrderSide(side_value)
        levels = load_ladder(ladder)
        if not levels:
            # Пустая книга — это данные, а не сбой: в мелком хабе редкий газ
            # просто никто не продаёт. Ячейка останется пустой.
            seen_hubs.add(hub_key)
            continue
        # Нет пригодной истории — band вернёт None, и сработает прежнее правило
        # по медиане книги. Для редкого газа в мелком хабе это норма (ESI §5.4).
        stats = history.get((region_of.get(hub_key, 0), type_id), HistoryStats())
        quotes[(hub_key, form, side)] = StoredQuote(
            quote=quote_from_ladder(levels, side, needed[form], band=stats.band()),
            collected_at=collected_at,
            history=stats,
        )
        seen_hubs.add(hub_key)

    times = [q.collected_at for q in quotes.values()]
    return PriceBook(
        quotes=quotes,
        missing_hubs=tuple(h.key for h in hubs if h.key not in seen_hubs),
        collected_at=max(times) if times else None,
        oldest_at=min(times) if times else None,
    )


def load_history_stats(
    engine: Engine, hubs: Sequence[Hub], type_ids: Mapping[GasForm, int]
) -> dict[tuple[str, GasForm], HistoryStats]:
    """Свод реальных сделок по каждой паре «хаб + форма» — для таблицы результата.

    Отдельный вход нужен потому, что /calculate считает по ценам из формы:
    человек мог их перебить, и лестницы под ними нет. История при этом наша,
    а не пользовательская, и подмешивать её через скрытые поля было бы враньём
    с возможностью подделки.

    Недоступная база здесь не повод для ошибки: расчёт обязан работать и без
    истории, просто без колонок «Сделки» и «Продано».
    """
    if not type_ids or not hubs:
        return {}
    by_type = {type_id: form for form, type_id in type_ids.items()}
    try:
        with session_scope(engine) as session:
            stats = _history_stats(
                session, sorted({hub.region_id for hub in hubs}), list(by_type)
            )
    except SQLAlchemyError:
        return {}

    return {
        (hub.key, by_type[type_id]): value
        for (region_id, type_id), value in stats.items()
        for hub in hubs
        if hub.region_id == region_id and type_id in by_type
    }


def _short_error(exc: Exception) -> str:
    """Первая строка ошибки: полный трейс psycopg в интерфейсе никому не нужен."""
    return str(exc).strip().splitlines()[0][:200]


def last_successful_run(engine: Engine, kind: str = "orders") -> datetime | None:
    """Когда сборщик в последний раз отработал без прерывания. Нужно /healthz.

    Сборщиков два, и ходят они с разной частотой: стакан каждые 30 минут,
    история сделок раз в сутки. Смешивать их в одно «время последнего сбора»
    значит показывать зелёный статус, когда встал один из двух.
    """
    with session_scope(engine) as session:
        return session.scalar(
            select(func.max(CollectionRun.finished_at)).where(
                CollectionRun.status.in_(("ok", "partial")),
                CollectionRun.kind == kind,
            )
        )
