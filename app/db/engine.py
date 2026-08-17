"""Подключение к базе: engine, фабрика сессий, привязка сессии к запросу.

Модуль не знает ни про схему, ни про бизнес-логику: только соединения.
Работает и без Flask — сборщику цен из этапа 7 нужен тот же engine из CLI.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Соединение переоткрывается после получаса простоя. MySQL по умолчанию рвёт
# простаивающие соединения через 8 часов (wait_timeout), и без этого приложение
# узнавало бы о разрыве в момент запроса пользователя.
POOL_RECYCLE_SECONDS = 1800
POOL_SIZE = 5
MAX_OVERFLOW = 10

_SQLITE_PREFIX = "sqlite:///"
_MEMORY = ":memory:"


def _sqlite_path(url: str) -> str | None:
    """Путь к файлу для SQLite-адреса. None — если это не SQLite."""
    if not url.startswith(_SQLITE_PREFIX):
        return None
    return url[len(_SQLITE_PREFIX) :]


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    """Включает проверку внешних ключей в SQLite.

    По умолчанию SQLite их не проверяет — в отличие от MariaDB, MySQL и Postgres.
    Без этого ограничения молча не работали бы в разработке и внезапно
    заработали бы на проде. PRAGMA действует на соединение, поэтому ставится
    на каждое новое.
    """

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_db_engine(config: Mapping[str, Any]) -> Engine:
    """Создаёт engine по DATABASE_URL из конфигурации.

    Соединение при этом не открывается: SQLAlchemy подключается лениво,
    при первом запросе. Поэтому вызов безопасен на старте приложения.
    """
    url = config.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL не задан. В профиле dev он подставляется автоматически, "
            "в профиле prod приходит из переменной окружения."
        )

    path = _sqlite_path(url)
    if path is None:
        # MariaDB, MySQL, Postgres: пул с проверкой живости соединения
        return create_engine(
            url,
            pool_pre_ping=True,
            pool_size=POOL_SIZE,
            max_overflow=MAX_OVERFLOW,
            pool_recycle=POOL_RECYCLE_SECONDS,
        )

    if path == _MEMORY or not path:
        # База в памяти живёт внутри соединения. Обычный пул выдал бы разным
        # потокам разные пустые базы, поэтому держим ровно одно соединение.
        engine = create_engine(
            url,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        _enable_sqlite_foreign_keys(engine)
        return engine

    # SQLite не создаёт каталог сам: без var/ падает «unable to open database file»
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        url,
        pool_pre_ping=True,
        # waitress обслуживает запросы в нескольких потоках, и соединение
        # из пула может достаться не тому потоку, который его открыл
        connect_args={"check_same_thread": False},
    )
    _enable_sqlite_foreign_keys(engine)
    return engine


def init_app(app: Any) -> None:
    """Заводит engine и фабрику сессий, вешает закрытие сессии на конец запроса."""
    engine = create_db_engine(app.config)
    app.extensions["db_engine"] = engine
    app.extensions["db_sessionmaker"] = sessionmaker(bind=engine, expire_on_commit=False)
    app.teardown_appcontext(close_session)


def get_session() -> Session:
    """Сессия текущего запроса. Одна на запрос, создаётся при первом обращении."""
    from flask import current_app, g

    session: Session | None = getattr(g, "db_session", None)
    if session is None:
        session = current_app.extensions["db_sessionmaker"]()
        g.db_session = session
    return session


def close_session(exc: BaseException | None = None) -> None:
    """Закрывает сессию запроса.

    Коммита здесь нет намеренно: сохранять данные должен тот код, который их
    менял, и делать это явно. Автоматический коммит на выходе прячет ошибки
    и превращает половину незавершённой операции в записанную.
    """
    from flask import g

    session: Session | None = g.pop("db_session", None) if hasattr(g, "pop") else None
    if session is None:
        return
    if exc is not None:
        session.rollback()
    session.close()


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Сессия для кода вне Flask: CLI-сборщика цен и тестов.

    Успешный выход — коммит, исключение — откат. В обоих случаях сессия закрыта.
    """
    session = Session(bind=engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
