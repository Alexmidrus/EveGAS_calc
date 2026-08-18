"""Тесты сборщика истории сделок (ROADMAP, этап 11.2).

Сети здесь нет: httpx.MockTransport подменяет транспорт, но оставляет живыми
и клиент, и разбор заголовков. Проверяется поведение суточного цикла — что он
экономит запросы, изолирует сбои, помнит ETag и не хранит лишних дней.
"""

import asyncio
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.core import catalog
from app.db import (
    Base,
    CollectionRun,
    MarketHistory,
    MarketHistoryState,
    create_db_engine,
    session_scope,
    utcnow,
)
from app.jobs import collect_history
from app.services.esi import EsiSettings

SETTINGS = EsiSettings(
    user_agent="EveGAS_calc/tests (+https://example.invalid; tests@example.invalid)",
    compatibility_date="2026-08-13",
)

C320 = catalog.gas_by_key("fullerite_c320")


def day(when: date, average: float = 3000.0, volume: int = 100_000) -> dict:
    """Один день истории ровно в том виде, в каком его отдаёт ESI."""
    return {
        "date": when.isoformat(),
        "average": average,
        "highest": average * 1.05,
        "lowest": average * 0.95,
        "order_count": 20,
        "volume": volume,
    }


def recent_days(count: int = 3) -> list[dict]:
    """Последние count дней, заканчивая вчерашним: сегодняшнего в истории нет."""
    yesterday = utcnow().date() - timedelta(days=1)
    return [day(yesterday - timedelta(days=i)) for i in reversed(range(count))]


@pytest.fixture
def engine():
    engine = create_db_engine({"DATABASE_URL": "sqlite:///:memory:"})
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def targets():
    """Одна пара: The Forge и сжатый C320. Хватает, чтобы проверить весь цикл."""
    return [
        t
        for t in collect_history.build_targets(only_hub="jita", only_gas="fullerite_c320")
        if t.form == "compressed"
    ]


def client_returning(response, record=None, by_type=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        type_id = int(request.url.params["type_id"])
        return (by_type or {}).get(type_id, response)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok_response(days=None, etag='W/"abc"', headers=None):
    head = {"etag": etag}
    head.update(headers or {})
    return httpx.Response(200, json=days if days is not None else recent_days(), headers=head)


def run(engine, targets, client, **kwargs):
    async def go():
        async with client:
            return await collect_history.run_collection(
                engine, SETTINGS, targets, client=client, **kwargs
            )

    return asyncio.run(go())


class TestTargets:
    """Из чего складывается обход."""

    def test_full_sweep_size(self):
        """27 газов × 2 формы × 5 регионов = 270 запросов в сутки (ESI §5.3)."""
        assert len(collect_history.build_targets()) == 270

    def test_targets_are_regional(self):
        """Цель — пара «регион + тип», хабы в ней не участвуют."""
        targets = collect_history.build_targets()
        assert len({t.region_id for t in targets}) == 5
        assert len({(t.region_id, t.type_id) for t in targets}) == len(targets)

    def test_unknown_hub(self):
        with pytest.raises(SystemExit):
            collect_history.build_targets(only_hub="perimeter")


class TestParseDays:
    """Разбор ответа: окно хранения и терпимость к мусору."""

    def test_keeps_only_window(self):
        today = date(2026, 8, 19)
        days = [day(today - timedelta(days=n)) for n in (1, 5, 200)]
        parsed = collect_history.parse_days(days, keep_days=90, today=today)
        assert [row["date"] for row in parsed] == [
            today - timedelta(days=1),
            today - timedelta(days=5),
        ]

    def test_money_keeps_two_decimals(self):
        parsed = collect_history.parse_days([day(date(2026, 8, 18), average=3000.126)])
        assert str(parsed[0]["average"]) == "3000.13"

    def test_broken_day_is_skipped_not_fatal(self):
        """Одна кривая строка не должна ронять сбор по всему региону."""
        days = [day(date(2026, 8, 18)), {"date": "не дата", "average": 1}]
        parsed = collect_history.parse_days(days)
        assert len(parsed) == 1


class TestHappyPath:
    def test_writes_days(self, engine, targets):
        stats = run(engine, targets, client_returning(ok_response()))
        assert stats.written == 3
        assert stats.errors == 0
        with session_scope(engine) as session:
            assert session.scalar(select(func.count()).select_from(MarketHistory)) == 3

    def test_records_run_as_history(self, engine, targets):
        """Запуск виден отдельно от сбора стакана — иначе /healthz смешает их."""
        run(engine, targets, client_returning(ok_response()))
        with session_scope(engine) as session:
            row = session.scalars(select(CollectionRun)).one()
            assert row.kind == "history"
            assert row.status == "ok"
            assert row.snapshots_written == 3

    def test_state_saved(self, engine, targets):
        response = ok_response(headers={"last-modified": "Tue, 18 Aug 2026 11:25:54 GMT"})
        run(engine, targets, client_returning(response))
        with session_scope(engine) as session:
            state = session.scalars(select(MarketHistoryState)).one()
            assert state.etag == 'W/"abc"'
            assert state.last_modified == "Tue, 18 Aug 2026 11:25:54 GMT"

    def test_conditional_headers_sent_back(self, engine, targets):
        """Второй заход обязан прийти с обоими условными заголовками."""
        response = ok_response(headers={"last-modified": "Tue, 18 Aug 2026 11:25:54 GMT"})
        run(engine, targets, client_returning(response))
        record: list[httpx.Request] = []
        run(engine, targets, client_returning(response, record=record))
        assert record[0].headers["if-none-match"] == 'W/"abc"'
        assert record[0].headers["if-modified-since"] == "Tue, 18 Aug 2026 11:25:54 GMT"

    def test_replaces_instead_of_appending(self, engine, targets):
        """История заменяется целиком: ESI отдаёт её всю, дубли не нужны."""
        run(engine, targets, client_returning(ok_response()))
        run(engine, targets, client_returning(ok_response()))
        with session_scope(engine) as session:
            assert session.scalar(select(func.count()).select_from(MarketHistory)) == 3

    def test_old_days_dropped_on_refresh(self, engine, targets):
        """День, вышедший за окно хранения, при обновлении исчезает."""
        old = [day(utcnow().date() - timedelta(days=200))]
        run(engine, targets, client_returning(ok_response(days=old + recent_days(1))))
        with session_scope(engine) as session:
            assert session.scalar(select(func.count()).select_from(MarketHistory)) == 1


class TestNotModified:
    def test_304_writes_nothing(self, engine, targets):
        run(engine, targets, client_returning(ok_response()))
        stats = run(engine, targets, client_returning(httpx.Response(304)))
        assert stats.not_modified == 1
        assert stats.written == 0
        with session_scope(engine) as session:
            assert session.scalar(select(func.count()).select_from(MarketHistory)) == 3

    def test_304_confirms_freshness(self, engine, targets):
        """«Твоя копия актуальна» — это подтверждение, а не отсутствие данных."""
        run(engine, targets, client_returning(ok_response()))
        with session_scope(engine) as session:
            state = session.scalars(select(MarketHistoryState)).one()
            state.checked_at = utcnow() - timedelta(days=2)
            stale = state.checked_at

        run(engine, targets, client_returning(httpx.Response(304)))
        with session_scope(engine) as session:
            assert session.scalars(select(MarketHistoryState)).one().checked_at > stale


class TestEconomy:
    def test_fresh_history_skips_request(self, engine, targets):
        """До 11:05 UTC ответ не изменится — ходить незачем."""
        expires = "Wed, 19 Aug 2026 11:05:00 GMT"
        run(engine, targets, client_returning(ok_response(headers={"expires": expires})))
        with session_scope(engine) as session:
            session.scalars(select(MarketHistoryState)).one().expires_at = utcnow() + timedelta(
                hours=5
            )

        record: list[httpx.Request] = []
        stats = run(engine, targets, client_returning(ok_response(), record=record))
        assert stats.skipped_fresh == 1
        assert record == []


class TestFailures:
    def test_one_region_failure_does_not_stop_others(self, engine):
        """Падение по одной паре не отменяет остальные (ESI §2)."""
        targets = collect_history.build_targets(only_gas="fullerite_c320")
        broken = {C320.raw_type_id: httpx.Response(500)}
        stats = run(engine, targets, client_returning(ok_response(), by_type=broken))
        assert stats.errors == 5  # пять регионов по сырому газу
        assert stats.written == 5 * 3  # сжатый собран везде

    def test_previous_history_survives_failure(self, engine, targets):
        """Ноль вместо цены хуже отсутствия цены — прежние данные остаются."""
        run(engine, targets, client_returning(ok_response()))
        run(engine, targets, client_returning(httpx.Response(500)))
        with session_scope(engine) as session:
            assert session.scalar(select(func.count()).select_from(MarketHistory)) == 3

    def test_rate_limit_aborts(self, engine):
        targets = collect_history.build_targets(only_gas="fullerite_c320")
        stats = run(engine, targets, client_returning(httpx.Response(429)))
        assert stats.aborted_reason
        assert stats.exit_code == collect_history.EXIT_ABORTED

    def test_error_limit_floor_aborts(self, engine):
        """Упереться в лимит ошибок нельзя: ESI начнёт отбрасывать всё подряд."""
        targets = collect_history.build_targets(only_gas="fullerite_c320")
        response = ok_response(headers={"x-esi-error-limit-remain": "1"})
        stats = run(engine, targets, client_returning(response))
        assert stats.aborted_reason
        assert "Лимит ошибок" in stats.aborted_reason


class TestDryRun:
    def test_nothing_written(self, engine, targets):
        stats = run(engine, targets, client_returning(ok_response()), dry_run=True)
        assert stats.written == 3
        with session_scope(engine) as session:
            assert session.scalar(select(func.count()).select_from(MarketHistory)) == 0
            assert session.scalar(select(func.count()).select_from(CollectionRun)) == 0
