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
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
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
    """Один запуск сборщика. Нужен, чтобы видеть, живёт ли сбор вообще."""

    __tablename__ = "collection_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'ok', 'partial', 'aborted')",
            name="collection_run_status",
        ),
        CheckConstraint(
            "kind IN ('orders', 'history')",
            name="collection_run_kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Сборщиков два, и ходят они с разной частотой: стакан каждые 30 минут,
    # история сделок раз в сутки (ESI §1). Без этого поля «время последнего
    # успешного сбора» на /healthz смешивало бы их в одну кучу.
    kind: Mapped[str] = mapped_column(String(16), default="orders")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # running — идёт прямо сейчас, ok — всё собрано, partial — часть хабов
    # не ответила, aborted — цикл прерван лимитом ESI
    status: Mapped[str] = mapped_column(String(16), default="running")
    requests_made: Mapped[int] = mapped_column(Integer, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, default=0)
    # Для kind='orders' — срезы стакана, для kind='history' — дни истории
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
    # JSON вида [["2750.00", 12000, 1], ...]: цена строкой, объём и min_volume целыми.
    #
    # min_volume хранится не для красоты: buy-ордер с min_volume больше нужного
    # количества исполнить нельзя (ESI §4), а нужное количество знает только
    # пользователь. Без этого поля фильтр было бы нечем воспроизвести при чтении.
    ladder: Mapped[str] = mapped_column(Text)
    total_volume: Mapped[int] = mapped_column(BigInteger, default=0)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    # ETag последнего ответа ESI. В следующий раз уходит в If-None-Match:
    # ответ 304 стоит вдвое дешевле по токенам рейт-лимита (ESI §2).
    etag: Mapped[str | None] = mapped_column(String(128), default=None)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("collection_run.id", ondelete="SET NULL"), default=None
    )


class MarketHistory(Base):
    """Дневной итог реальных сделок по типу в регионе (ESI §5).

    Ключ регионный, а не по хабу: эндпоинт истории отдаёт данные на регион
    целиком, разбивки по станциям в нём нет. Сейчас пять хабов лежат в пяти
    разных регионах и одно однозначно отображается в другое, но это свойство
    нашего списка хабов, а не API.

    Строка на сутки: ESI обновляет её раз в день в 11:05 UTC, и сегодняшнего
    дня в истории не бывает никогда.
    """

    __tablename__ = "market_history"
    __table_args__ = (
        UniqueConstraint("region_id", "type_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region_id: Mapped[int] = mapped_column(Integer)
    type_id: Mapped[int] = mapped_column(Integer)
    # Календарные сутки по UTC, как их отдаёт ESI
    date: Mapped[date] = mapped_column(Date)
    # Дневная средняя цена сделок. Чем именно она взвешена, документация ESI
    # не пишет, и называть её VWAP оснований нет (ESI §5.2).
    average: Mapped[Decimal] = mapped_column(MONEY)
    highest: Mapped[Decimal] = mapped_column(MONEY)
    lowest: Mapped[Decimal] = mapped_column(MONEY)
    # Сколько юнитов реально сменило владельца за сутки
    volume: Mapped[int] = mapped_column(BigInteger, default=0)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MarketHistoryState(Base):
    """Состояние условных запросов истории по паре «регион + тип».

    Отдельно от самих данных: ETag относится ко всему ответу, а не к строке-дню.
    Здесь же лежит время последней проверки — на 304 данные не переписываются,
    но факт «мы только что убедились, что копия актуальна» обязан сохраниться.
    """

    __tablename__ = "market_history_state"

    region_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # ETag у этого эндпоинта слабый (W/"..."), и это нормально (ESI §5.3)
    etag: Mapped[str | None] = mapped_column(String(128), default=None)
    # Строкой ровно в том виде, в каком её отдал ESI: она уедет обратно
    # в If-Modified-Since, и переформатировать её нам незачем
    last_modified: Mapped[str | None] = mapped_column(String(64), default=None)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # False — ESI отвечает «Type not tradable on market!»: истории по типу нет
    # ни в одном регионе, хотя стакан по нему отдаётся. Проверено 19.08.2026
    # на Chartreuse и Gamboge Cytoserocin. Колонка нужна, чтобы не спрашивать
    # такие типы каждые сутки: каждый ответ 400 стоит пять токенов лимита ошибок.
    tradable: Mapped[bool] = mapped_column(default=True)


class EsiStatus(Base):
    """Состояние игрового сервера, снятое сборщиком (ESI §8).

    Зачем в базе. Пользователь к ESI не ходит по определению проекта: двести
    человек с кнопкой «проверить сервер» — это бан. Поэтому /status/ спрашивает
    сборщик раз в цикл, а страница читает готовую строку отсюда.

    Строка на проверку, а не одна на всё: перезаписывать единственную запись
    значит стирать историю проверок вместе с фактом «в прошлый раз не ответил».
    Старые строки чистит тот же prune, что и срезы стакана.

    Недоступность — это тоже результат проверки, а не отсутствие результата:
    у такой строки reachable=False и текст в error, и на экране она
    превращается в честное «ESI не отвечает», а не в молчание.
    """

    __tablename__ = "esi_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # Ответил ли ESI по существу. False — сеть, таймаут или 5xx
    reachable: Mapped[bool] = mapped_column(default=True)
    # None, когда не ответил: ноль здесь соврал бы «на сервере никого»
    players: Mapped[int | None] = mapped_column(Integer, default=None)
    server_version: Mapped[str | None] = mapped_column(String(32), default=None)
    # Время последнего старта Tranquility, наивный UTC
    start_time: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # Сервер поднят, но пускает только избранных: обычный игрок войти не может
    vip: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(String(255), default=None)


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
    buy_only: Mapped[bool] = mapped_column(default=False)
    # Фильтр показа, а не расчёта, но хранится наравне с остальными:
    # разъехавшееся поведение соседних галочек хуже, чем их отсутствие
    hide_illiquid: Mapped[bool] = mapped_column(default=False)
    best_per_hub: Mapped[bool] = mapped_column(default=False)
    # Порядок таблицы: имя колонки и направление. В базе `sort` — слишком
    # общее имя для колонки, поэтому `sort_column`; в форме поле называется
    # `sort` и переименовывать его нельзя — это контракт формы.
    sort_column: Mapped[str | None] = mapped_column(String(16), default=None)
    sort_dir: Mapped[str | None] = mapped_column(String(4), default=None)
    # Тема оформления. None — «не выбирал»: умолчание тёмное и держит его
    # CSS, а не база. Значение проверяется по белому списку при записи.
    theme: Mapped[str | None] = mapped_column(String(8), default=None)
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


def dump_ladder(levels: Iterable[tuple[Decimal | float | str, int, int]]) -> str:
    """Сериализует лестницу в JSON. Цена — строкой, чтобы не потерять копейки.

    Уровень — тройка (цена, доступный объём, min_volume). Порядок уровней
    значимый: он уже отсортирован под свою сторону стакана и при чтении
    пересортировке не подлежит.
    """
    return json.dumps(
        [
            [str(Decimal(str(price))), int(volume), int(min_volume)]
            for price, volume, min_volume in levels
        ],
        separators=(",", ":"),
    )


def load_ladder(raw: str) -> list[tuple[Decimal, int, int]]:
    """Читает лестницу обратно. Цена возвращается Decimal, а не float."""
    return [
        (Decimal(price), int(volume), int(min_volume))
        for price, volume, min_volume in json.loads(raw)
    ]
