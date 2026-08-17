"""Схема должна собираться под все четыре СУБД (ROADMAP, этап 6).

Живого сервера здесь нет: SQLAlchemy умеет отрендерить DDL под диалект без
подключения. Этого хватает, чтобы поймать типовые расхождения — VARCHAR без
длины, слишком длинное имя индекса, тип, которого в диалекте нет. Прогон
на настоящих MariaDB, MySQL и Postgres этот тест не заменяет.
"""

import pytest
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from app.db import Base

DIALECTS = {
    "postgresql": postgresql.dialect(),
    "mysql": mysql.dialect(),
    "mariadb": mysql.dialect(is_mariadb=True),
    "sqlite": sqlite.dialect(),
}

# MySQL и MariaDB режут идентификаторы на 64 символах
MAX_IDENTIFIER_LENGTH = 64

TABLES = sorted(Base.metadata.tables)


@pytest.mark.parametrize("dialect_name", sorted(DIALECTS))
@pytest.mark.parametrize("table_name", TABLES)
def test_create_table_compiles(dialect_name, table_name):
    """CREATE TABLE собирается без ошибок под каждым диалектом."""
    table = Base.metadata.tables[table_name]
    ddl = str(CreateTable(table).compile(dialect=DIALECTS[dialect_name]))
    assert ddl.strip().upper().startswith("CREATE TABLE")


@pytest.mark.parametrize("dialect_name", sorted(DIALECTS))
def test_indexes_compile(dialect_name):
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            str(CreateIndex(index).compile(dialect=DIALECTS[dialect_name]))


@pytest.mark.parametrize("table_name", TABLES)
def test_varchar_columns_have_length(table_name):
    """MySQL не умеет VARCHAR без длины: на SQLite такая колонка пройдёт,
    а на проде создание таблицы упадёт.

    TEXT — исключение, ему длина не нужна. В SQLAlchemy Text наследуется
    от String, поэтому проверять надо не просто «строковая колонка».
    """
    from sqlalchemy import String, Text

    for column in Base.metadata.tables[table_name].columns:
        if isinstance(column.type, Text) or not isinstance(column.type, String):
            continue
        assert column.type.length, f"{table_name}.{column.name}: VARCHAR без длины"


def test_identifier_lengths():
    """Имена таблиц, колонок, индексов и ограничений — в пределах 64 символов."""
    too_long: list[str] = []
    for table in Base.metadata.tables.values():
        names = [table.name]
        names += [c.name for c in table.columns]
        names += [i.name for i in table.indexes]
        names += [c.name for c in table.constraints if c.name]
        too_long += [n for n in names if n and len(str(n)) > MAX_IDENTIFIER_LENGTH]
    assert too_long == []


def test_no_float_money_columns():
    """Деньги и доли — только Numeric. Float в схеме означает потерянные копейки."""
    from sqlalchemy import Float

    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Float)
    ]
    assert offenders == []
