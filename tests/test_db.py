"""Тесты слоя базы данных (ROADMAP, этап 6).

Всё гоняется на SQLite в памяти: схема одна на все четыре СУБД, а проверка
на живых MariaDB, MySQL и Postgres — отдельная ручная задача этапа.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from app.config import build_config
from app.db import (
    Base,
    CollectionRun,
    MarketSnapshot,
    UserAccount,
    UserFreightRate,
    UserSettings,
    create_db_engine,
    dump_ladder,
    load_ladder,
    session_scope,
    utcnow,
)

MEMORY = {"DATABASE_URL": "sqlite:///:memory:"}


@pytest.fixture
def engine():
    """Пустая база в памяти со свежей схемой."""
    engine = create_db_engine(MEMORY)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def account(engine):
    """Аккаунт, на который можно вешать настройки: они за ним по внешнему ключу."""
    with session_scope(engine) as session:
        session.add(UserAccount(character_id=90_000_001, character_name="Test Pilot"))
    return 90_000_001


class TestEngine:
    """Создание движка под разные адреса."""

    def test_memory_keeps_one_connection(self, engine):
        """База в памяти живёт внутри соединения: обычный пул выдал бы пустую."""
        with session_scope(engine) as session:
            session.add(CollectionRun(status="ok"))
        with session_scope(engine) as session:
            assert session.scalar(select(CollectionRun).limit(1)) is not None

    def test_sqlite_file_creates_directory(self, tmp_path):
        """SQLite не создаёт каталог сам — движок обязан сделать это за него."""
        target = tmp_path / "var" / "nested" / "app.sqlite3"
        assert not target.parent.exists()
        create_db_engine({"DATABASE_URL": f"sqlite:///{target.as_posix()}"})
        assert target.parent.exists()

    def test_missing_url(self):
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            create_db_engine({"DATABASE_URL": None})

    def test_dev_profile_url_works(self, tmp_path):
        """Адрес, который выдаёт dev-профиль, должен быть пригоден как есть."""
        config = build_config({}, tmp_path)
        engine = create_db_engine(config)
        Base.metadata.create_all(engine)
        assert "market_snapshot" in inspect(engine).get_table_names()
        engine.dispose()


class TestSchema:
    """Таблицы, индексы и ограничения."""

    def test_all_tables_created(self, engine):
        assert set(inspect(engine).get_table_names()) == {
            "collection_run",
            "esi_status",
            "market_snapshot",
            "market_history",
            "market_history_state",
            "user_account",
            "user_settings",
            "user_freight_rate",
        }

    def test_lookup_index_exists(self, engine):
        """Единственный горячий запрос — «последний срез» — обязан идти по индексу."""
        indexes = {i["name"]: i["column_names"] for i in inspect(engine).get_indexes("market_snapshot")}
        assert indexes["ix_market_snapshot_lookup"] == [
            "hub_key",
            "type_id",
            "side",
            "collected_at",
        ]

    def test_side_check_constraint(self, engine):
        """В сторону стакана нельзя записать что попало."""
        with pytest.raises(IntegrityError), session_scope(engine) as session:
            session.add(
                MarketSnapshot(hub_key="jita", type_id=62406, side="both", ladder="[]")
            )

    def test_run_status_check_constraint(self, engine):
        with pytest.raises(IntegrityError), session_scope(engine) as session:
            session.add(CollectionRun(status="как-то так"))

    def test_settings_require_existing_account(self, engine):
        """Настройки без аккаунта — мусор. SQLite проверяет ключи только с PRAGMA,
        которую движок ставит сам: иначе ограничение работало бы на проде
        и молчало в разработке."""
        with pytest.raises(IntegrityError), session_scope(engine) as session:
            session.add(UserSettings(character_id=42))

    def test_freight_rate_requires_existing_account(self, engine):
        with pytest.raises(IntegrityError), session_scope(engine) as session:
            session.add(UserFreightRate(character_id=42, hub_key="jita", rate=Decimal("500")))


class TestRoundTrip:
    """Что положили — то и прочитали."""

    def test_collection_run(self, engine):
        started = utcnow()
        with session_scope(engine) as session:
            session.add(
                CollectionRun(
                    started_at=started,
                    finished_at=started + timedelta(seconds=42),
                    status="partial",
                    requests_made=270,
                    errors_count=3,
                    snapshots_written=267,
                    note="Rens не ответил",
                )
            )
        with session_scope(engine) as session:
            run = session.scalar(select(CollectionRun))
            assert run.status == "partial"
            assert run.requests_made == 270
            assert run.errors_count == 3
            assert run.note == "Rens не ответил"

    def test_snapshot(self, engine):
        collected = utcnow()
        with session_scope(engine) as session:
            session.add(
                MarketSnapshot(
                    hub_key="jita",
                    type_id=62406,
                    side="sell",
                    collected_at=collected,
                    expires_at=collected + timedelta(minutes=5),
                    ladder=dump_ladder([("2750.00", 12_000, 1), ("2755.50", 3_000, 100)]),
                    total_volume=15_000,
                    order_count=2,
                )
            )
        with session_scope(engine) as session:
            row = session.scalar(select(MarketSnapshot))
            assert row.hub_key == "jita"
            assert row.total_volume == 15_000
            assert load_ladder(row.ladder) == [
                (Decimal("2750.00"), 12_000, 1),
                (Decimal("2755.50"), 3_000, 100),
            ]

    def test_freight_rate_is_exact(self, engine, account):
        """Ставка доставки — деньги: из базы обязана вернуться ровно та же."""
        with session_scope(engine) as session:
            session.add(
                UserFreightRate(character_id=account, hub_key="jita", rate=Decimal("512.35"))
            )
        with session_scope(engine) as session:
            rate = session.scalar(select(UserFreightRate.rate))
            assert rate == Decimal("512.35")
            assert isinstance(rate, Decimal)

    def test_settings(self, engine, account):
        with session_scope(engine) as session:
            session.add(
                UserSettings(
                    character_id=account,
                    gas_key="fullerite_c320",
                    n_units=50_000,
                    structure="athanor",
                    gde_level=5,
                    broker_fee=Decimal("0.0150"),
                    collateral_pct=Decimal("0.0050"),
                    sell_only=True,
                )
            )
        with session_scope(engine) as session:
            settings = session.scalar(select(UserSettings))
            assert settings.gas_key == "fullerite_c320"
            assert settings.broker_fee == Decimal("0.0150")
            assert settings.collateral_pct == Decimal("0.0050")
            assert settings.sell_only is True

    def test_freight_rate_one_per_hub(self, engine, account):
        """Ключ составной: две ставки на один хаб для одного персонажа невозможны."""
        with session_scope(engine) as session:
            session.add(UserFreightRate(character_id=account, hub_key="jita", rate=Decimal("500")))
        with pytest.raises(IntegrityError), session_scope(engine) as session:
            session.add(UserFreightRate(character_id=account, hub_key="jita", rate=Decimal("600")))


class TestLadderFormat:
    """Лестница стакана: цена не должна проходить через float."""

    def test_kopecks_survive(self):
        """0.1 + 0.2 во float даёт 0.30000000000000004. Здесь такого быть не может."""
        raw = dump_ladder([("1234567.89", 1, 1)])
        assert load_ladder(raw) == [(Decimal("1234567.89"), 1, 1)]

    def test_accepts_decimal_and_float(self):
        raw = dump_ladder([(Decimal("100.50"), 5, 1), (100.25, 7, 50)])
        assert load_ladder(raw) == [(Decimal("100.50"), 5, 1), (Decimal("100.25"), 7, 50)]

    def test_empty(self):
        assert load_ladder(dump_ladder([])) == []

    def test_json_is_compact(self):
        """Лестниц будет 270 штук на цикл — лишние пробелы это лишние байты."""
        assert " " not in dump_ladder([("2750.00", 12_000, 1)])


class TestLatestSnapshotQuery:
    """Тот самый горячий запрос, ради которого сделан индекс."""

    def test_latest_wins(self, engine):
        older = utcnow() - timedelta(minutes=30)
        newer = utcnow()
        with session_scope(engine) as session:
            session.add_all(
                [
                    MarketSnapshot(
                        hub_key="jita", type_id=62406, side="sell",
                        collected_at=older, ladder=dump_ladder([("3000.00", 10, 1)]),
                    ),
                    MarketSnapshot(
                        hub_key="jita", type_id=62406, side="sell",
                        collected_at=newer, ladder=dump_ladder([("2750.00", 10, 1)]),
                    ),
                ]
            )
        with session_scope(engine) as session:
            row = session.scalar(
                select(MarketSnapshot)
                .where(
                    MarketSnapshot.hub_key == "jita",
                    MarketSnapshot.type_id == 62406,
                    MarketSnapshot.side == "sell",
                )
                .order_by(MarketSnapshot.collected_at.desc())
                .limit(1)
            )
            assert load_ladder(row.ladder)[0][0] == Decimal("2750.00")


class TestSessionScope:
    """Границы транзакции."""

    def test_commit_on_success(self, engine):
        with session_scope(engine) as session:
            session.add(CollectionRun(status="ok"))
        with session_scope(engine) as session:
            assert session.scalars(select(CollectionRun)).all()

    def test_rollback_on_error(self, engine):
        with pytest.raises(ValueError):
            with session_scope(engine) as session:
                session.add(CollectionRun(status="ok"))
                session.flush()
                raise ValueError("что-то пошло не так по дороге")
        with session_scope(engine) as session:
            assert session.scalars(select(CollectionRun)).all() == []


class TestTimeConvention:
    """Время в базе — наивный UTC, иначе MySQL и SQLite разъедутся."""

    def test_utcnow_is_naive(self):
        assert utcnow().tzinfo is None

    def test_utcnow_is_really_utc(self):
        """Ловушка: datetime.now() вместо UTC. На машине в Москве это +3 часа
        разницы, и время сбора цен в базе окажется из будущего."""
        delta = abs(utcnow() - datetime.now(UTC).replace(tzinfo=None))
        assert delta.total_seconds() < 1
