"""Чтение собранных цен из базы (ROADMAP, этап 8).

Приложение к ESI не обращается: сюда приходит запрос пользователя, отсюда
уходит запрос в базу. Всё, что связано с сетью, живёт в сборщике.

Один поход в базу отдаёт срезы сразу по всем хабам и обеим формам газа —
десять троек на газ. Ходить за каждой отдельно значило бы десять запросов
на отрисовку страницы.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.models import GasForm, Hub, OrderSide
from app.db import CollectionRun, MarketSnapshot, load_ladder, session_scope, utcnow
from app.services.orderbook import Quote, quote_from_ladder


@dataclass(frozen=True, slots=True)
class StoredQuote:
    """Цена одной ячейки сетки, посчитанная из сохранённой лестницы."""

    quote: Quote
    collected_at: datetime

    @property
    def price(self) -> float | None:
        return self.quote.price

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
    try:
        with session_scope(engine) as session:
            rows = _latest_rows(session, list(by_type))
            payload = [
                (r.hub_key, int(r.type_id), r.side, r.ladder, r.collected_at) for r in rows
            ]
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
        quotes[(hub_key, form, side)] = StoredQuote(
            quote=quote_from_ladder(levels, side, needed[form]),
            collected_at=collected_at,
        )
        seen_hubs.add(hub_key)

    times = [q.collected_at for q in quotes.values()]
    return PriceBook(
        quotes=quotes,
        missing_hubs=tuple(h.key for h in hubs if h.key not in seen_hubs),
        collected_at=max(times) if times else None,
        oldest_at=min(times) if times else None,
    )


def _short_error(exc: Exception) -> str:
    """Первая строка ошибки: полный трейс psycopg в интерфейсе никому не нужен."""
    return str(exc).strip().splitlines()[0][:200]


def last_successful_run(engine: Engine) -> datetime | None:
    """Когда сборщик в последний раз отработал без прерывания. Нужно /healthz."""
    with session_scope(engine) as session:
        return session.scalar(
            select(func.max(CollectionRun.finished_at)).where(
                CollectionRun.status.in_(("ok", "partial"))
            )
        )
