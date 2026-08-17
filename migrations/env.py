"""Окружение Alembic.

Адрес базы берётся из конфигурации приложения, а не из alembic.ini: иначе
он был бы задан дважды и рано или поздно разъехался бы с рабочим. Профиль
выбирается так же, как у приложения, — переменной APP_ENV.

Запуск:

    alembic upgrade head          # dev, SQLite в var/
    APP_ENV=prod alembic upgrade head
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Корень репозитория в путях: alembic запускается из своего каталога
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import build_config  # noqa: E402
from app.db.engine import create_db_engine  # noqa: E402
from app.db.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    """Адрес базы из конфигурации приложения. Он же проходит всю валидацию профиля."""
    return build_config()["DATABASE_URL"]


def make_engine():
    """Тот же engine, что у приложения: одни настройки пула и одно место,
    где для SQLite создаётся каталог var/."""
    return create_db_engine(build_config())


def run_migrations_offline() -> None:
    """Генерация SQL без подключения к базе: alembic upgrade head --sql."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite не умеет ALTER TABLE как остальные: batch-режим пересоздаёт
        # таблицу целиком. Без него любая будущая правка колонки на SQLite упадёт.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Обычный режим: подключиться и накатить."""
    connectable = make_engine()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
