"""Чтение состояния сервера из базы (ROADMAP, пункт 2 после 0.3.0).

Сети здесь нет: в базу кладётся строка, проверяется то, во что она
превращается на экране. Главное, что проверяется, — «неизвестно» остаётся
отдельным состоянием и не подменяется ни «online», ни «offline».
"""

from datetime import timedelta

import pytest
from sqlalchemy import delete

from app.db import Base, EsiStatus, create_db_engine, session_scope, utcnow
from app.services import server_status


@pytest.fixture
def engine():
    engine = create_db_engine({"DATABASE_URL": "sqlite:///:memory:"})
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def put(engine, *, minutes_ago: int = 0, **fields) -> None:
    """Одна строка проверки, снятая minutes_ago минут назад."""
    defaults = dict(reachable=True, players=26_930, vip=False, error=None)
    with session_scope(engine) as session:
        session.add(
            EsiStatus(checked_at=utcnow() - timedelta(minutes=minutes_ago), **(defaults | fields))
        )
        session.commit()


class TestStates:
    def test_online(self, engine):
        put(engine)
        status = server_status.load(engine)
        assert status.state == "online"
        assert status.players == 26_930
        assert status.known

    def test_vip_is_not_online(self, engine):
        """Сервер поднят, но обычный игрок войти не может — это своё состояние."""
        put(engine, vip=True, players=12)
        status = server_status.load(engine)
        assert status.state == "vip"
        assert "VIP" in status.title

    def test_unreachable(self, engine):
        put(engine, reachable=False, players=None, error="ESI не ответил за 15 с")
        status = server_status.load(engine)
        assert status.state == "offline"
        assert status.players is None
        # Причина отказа доходит до подсказки, а не теряется в логах сборщика
        assert "ESI не ответил за 15 с" in status.title

    def test_never_checked(self, engine):
        """Сборщик ещё не отрабатывал: это не «сервер лежит»."""
        status = server_status.load(engine)
        assert status.state == "unknown"
        assert not status.known

    def test_stale_check_is_not_a_verdict(self, engine):
        """Показывать «online» по позавчерашней проверке нельзя."""
        put(engine, minutes_ago=60 * 48)
        assert server_status.load(engine).state == "unknown"

    def test_fresh_enough_within_window(self, engine):
        """Один пропущенный цикл сбора — ещё не потеря состояния."""
        put(engine, minutes_ago=35)
        assert server_status.load(engine).state == "online"

    def test_latest_check_wins(self, engine):
        """Читается последняя проверка, а не первая попавшаяся."""
        put(engine, minutes_ago=40, reachable=False, players=None, error="лежал")
        put(engine, minutes_ago=1, players=25_000)
        status = server_status.load(engine)
        assert status.state == "online"
        assert status.players == 25_000


class TestResilience:
    def test_broken_database_does_not_raise(self, engine):
        """Расчёт обязан работать всегда: чип — украшение, а не условие."""
        with session_scope(engine) as session:
            session.execute(delete(EsiStatus))
            session.commit()
        engine.dispose()
        Base.metadata.drop_all(engine)
        assert server_status.load(engine).state == "unknown"

    def test_zero_players_is_not_none(self, engine):
        """Ноль игроков — настоящий ответ (сервер пуст), а не «данных нет»."""
        put(engine, players=0)
        status = server_status.load(engine)
        assert status.state == "online"
        assert status.players == 0
