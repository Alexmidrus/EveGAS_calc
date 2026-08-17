"""Тесты миграций Alembic (ROADMAP, этап 6).

Миграции гоняются по-настоящему, на временном файле SQLite: смысл проверки
именно в том, что `alembic upgrade head` работает на пустой базе, а не в том,
что файл миграции существует.
"""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.config import BASE_DIR
from app.db import Base

EXPECTED_TABLES = {
    "collection_run",
    "market_snapshot",
    "user_account",
    "user_settings",
    "user_freight_rate",
}


@pytest.fixture
def alembic_config(tmp_path, monkeypatch):
    """Alembic, нацеленный на временный файл вместо рабочей базы."""
    database_url = f"sqlite:///{(tmp_path / 'migrations.sqlite3').as_posix()}"
    # env.py берёт адрес из конфигурации приложения, а та — из окружения
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("APP_ENV", "dev")

    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "migrations"))
    return config, database_url


def table_names(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


class TestUpgradeDowngrade:
    """Обе стороны миграции обязаны работать."""

    def test_upgrade_creates_schema(self, alembic_config):
        config, url = alembic_config
        command.upgrade(config, "head")
        assert EXPECTED_TABLES <= table_names(url)

    def test_downgrade_removes_schema(self, alembic_config):
        """Откат должен убирать за собой всё, кроме служебной таблицы версии."""
        config, url = alembic_config
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        assert table_names(url) & EXPECTED_TABLES == set()

    def test_upgrade_is_repeatable(self, alembic_config):
        """Повторный upgrade на уже накатанной базе — не ошибка, а ничего не делает."""
        config, url = alembic_config
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert EXPECTED_TABLES <= table_names(url)

    def test_full_cycle(self, alembic_config):
        """Вверх, вниз и снова вверх: откат не должен ломать повторный накат."""
        config, url = alembic_config
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        assert EXPECTED_TABLES <= table_names(url)


class TestSchemaMatchesModels:
    """Миграция и models.py не должны разъезжаться."""

    def test_same_tables(self, alembic_config):
        config, url = alembic_config
        command.upgrade(config, "head")
        from_migration = table_names(url) - {"alembic_version"}
        assert from_migration == set(Base.metadata.tables)

    def test_same_columns(self, alembic_config):
        """Забытая в миграции колонка — самый частый способ развалить прод."""
        config, url = alembic_config
        command.upgrade(config, "head")
        engine = create_engine(url)
        inspector = inspect(engine)
        try:
            for name, table in Base.metadata.tables.items():
                actual = {c["name"] for c in inspector.get_columns(name)}
                assert actual == set(table.columns.keys()), f"таблица {name}"
        finally:
            engine.dispose()

    def test_lookup_index_present(self, alembic_config):
        """Индекс легко потерять при ручной правке миграции, а без него
        «последний срез» превратится в полный перебор 270 срезов на цикл."""
        config, url = alembic_config
        command.upgrade(config, "head")
        engine = create_engine(url)
        try:
            names = {i["name"] for i in inspect(engine).get_indexes("market_snapshot")}
            assert "ix_market_snapshot_lookup" in names
        finally:
            engine.dispose()


class TestSingleHead:
    """Одна голова: две ветки миграций — это конфликт, который всплывёт на проде."""

    def test_one_head(self, alembic_config):
        from alembic.script import ScriptDirectory

        config, _ = alembic_config
        assert len(ScriptDirectory.from_config(config).get_heads()) == 1
