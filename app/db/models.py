"""Схема базы данных.

Соглашения, общие для всех таблиц:

- **Время всегда UTC и всегда наивное.** MySQL и SQLite не хранят зону, поэтому
  единственный переносимый вариант — договориться о UTC и не класть в базу
  ничего локального. Для получения текущего момента есть ``utcnow()``.
- **Деньги — не float.** В колонках ``Numeric``, в JSON-лестнице — строками.
  Расчёт по-прежнему идёт во float (правило проекта), преобразование на границе.
  Смысл в том, чтобы база возвращала ровно то, что в неё положили.
- **Длины строк заданы явно.** MySQL не умеет VARCHAR без длины.
- **Имена ограничений задаёт naming_convention.** Без него SQLite создаёт
  безымянные ограничения, и Alembic потом не может их изменить.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Денежные величины: до 10^18 ISK с двумя знаками — с запасом на любые цены
MONEY = Numeric(20, 2)
# Доли: 0.0150 — брокерская комиссия 1.5%
SHARE = Numeric(6, 4)


def utcnow() -> datetime:
    """Текущий момент в UTC без зоны — единственный формат времени в базе."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class CollectionRun(Base):
    """Один запуск сборщика цен. Нужен, чтобы видеть, живёт ли сбор вообще."""

    __tablename__ = "collection_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'ok', 'partial', 'aborted')",
            name="collection_run_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # running — идёт прямо сейчас, ok — всё собрано, partial — часть хабов
    # не ответила, aborted — цикл прерван лимитом ESI
    status: Mapped[str] = mapped_column(String(16), default="running")
    requests_made: Mapped[int] = mapped_column(Integer, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, default=0)
    snapshots_written: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(String(500), default=None)


class MarketSnapshot(Base):
    """Срез стакана по одному (хаб, тип, сторона).

    Хранится не готовая цена, а очищенная лестница ордеров: VWAP зависит от
    нужного пользователю объёма, а сборщик его знать не может. Лестница лежит
    текстовым JSON — одинаково работает во всех четырёх СУБД, читается всегда
    целиком, искать внутри неё не нужно.
    """

    __tablename__ = "market_snapshot"
    __table_args__ = (
        CheckConstraint("side IN ('sell', 'buy')", name="market_snapshot_side"),
        # Единственный горячий запрос: «последний срез по хабу, типу и стороне»
        Index(
            "ix_market_snapshot_lookup",
            "hub_key",
            "type_id",
            "side",
            "collected_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hub_key: Mapped[str] = mapped_column(String(16))
    type_id: Mapped[int] = mapped_column(Integer)
    side: Mapped[str] = mapped_column(String(4))
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Момент, после которого ESI отдаст свежие данные — из заголовка expires
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # JSON вида [["2750.00", 12000], ...]: цена строкой, объём целым
    ladder: Mapped[str] = mapped_column(Text)
    total_volume: Mapped[int] = mapped_column(BigInteger, default=0)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("collection_run.id", ondelete="SET NULL"), default=None
    )


class UserAccount(Base):
    """Персонаж, вошедший через EVE SSO. Ничего приватного здесь не хранится."""

    __tablename__ = "user_account"

    # ID персонажа выдаёт CCP, свой автоинкремент не нужен
    character_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    character_name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserSettings(Base):
    """Настройки расчёта. Ровно то, что анонимный пользователь держит в localStorage."""

    __tablename__ = "user_settings"

    character_id: Mapped[int] = mapped_column(
        ForeignKey("user_account.character_id", ondelete="CASCADE"), primary_key=True
    )
    gas_key: Mapped[str | None] = mapped_column(String(64), default=None)
    n_units: Mapped[int | None] = mapped_column(Integer, default=None)
    structure: Mapped[str | None] = mapped_column(String(16), default=None)
    gde_level: Mapped[int | None] = mapped_column(Integer, default=None)
    broker_fee: Mapped[Decimal | None] = mapped_column(SHARE, default=None)
    collateral_pct: Mapped[Decimal | None] = mapped_column(SHARE, default=None)
    sell_only: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UserFreightRate(Base):
    """Ставка доставки, своя на каждый хаб. Отдельной строкой, а не пятью колонками:
    список хабов может измениться, а схема от этого меняться не должна."""

    __tablename__ = "user_freight_rate"

    character_id: Mapped[int] = mapped_column(
        ForeignKey("user_account.character_id", ondelete="CASCADE"), primary_key=True
    )
    hub_key: Mapped[str] = mapped_column(String(16), primary_key=True)
    rate: Mapped[Decimal] = mapped_column(MONEY)


# --- Лестница стакана: формат хранения ---


def dump_ladder(levels: list[tuple[Decimal | float | str, int]]) -> str:
    """Сериализует лестницу в JSON. Цена — строкой, чтобы не потерять копейки."""
    return json.dumps(
        [[str(Decimal(str(price))), int(volume)] for price, volume in levels],
        separators=(",", ":"),
    )


def load_ladder(raw: str) -> list[tuple[Decimal, int]]:
    """Читает лестницу обратно. Цена возвращается Decimal, а не float."""
    return [(Decimal(price), int(volume)) for price, volume in json.loads(raw)]
