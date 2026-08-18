"""Тесты сборщика цен (ROADMAP, этап 7).

Сети здесь нет: httpx.MockTransport подменяет транспорт, но оставляет живыми
и клиент, и разбор заголовков. Проверяется именно поведение цикла — что он
экономит запросы, изолирует сбои и вовремя останавливается.
"""

import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.core import catalog
from app.db import Base, CollectionRun, MarketSnapshot, create_db_engine, load_ladder, session_scope, utcnow
from app.jobs import collect
from app.jobs.lock import AlreadyRunning, file_lock
from app.services.esi import EsiSettings

SETTINGS = EsiSettings(
    user_agent="GasLens/tests (+https://example.invalid; tests@example.invalid)",
    compatibility_date="2026-08-13",
)

C320 = catalog.gas_by_key("fullerite_c320")

ORDERS = [
    {
        "order_id": 1, "type_id": C320.compressed_type_id, "location_id": 60003760,
        "system_id": 30000142, "is_buy_order": False, "price": 2750.10,
        "volume_remain": 12000, "min_volume": 1, "range": "station",
    },
    {
        "order_id": 2, "type_id": C320.compressed_type_id, "location_id": 60003760,
        "system_id": 30000142, "is_buy_order": True, "price": 2400.00,
        "volume_remain": 5000, "min_volume": 100, "range": "region",
    },
]


@pytest.fixture
def engine():
    engine = create_db_engine({"DATABASE_URL": "sqlite:///:memory:"})
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def targets():
    """Одна пара: Jita и сжатый C320. Хватает, чтобы проверить весь цикл."""
    return [t for t in collect.build_targets(only_hub="jita", only_gas="fullerite_c320")
            if t.form == "compressed"]


def client_returning(response, record=None, by_type=None):
    """Клиент с подменённым транспортом.

    Ответ выбирается по type_id из запроса, а не по счётчику вызовов: цели
    обходятся параллельно, и порядок обращений не определён.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        type_id = int(request.url.params["type_id"])
        return (by_type or {}).get(type_id, response)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok_response(orders=None, etag='"abc"', expires=None, headers=None):
    head = {"etag": etag}
    if expires:
        head["expires"] = expires
    head.update(headers or {})
    return httpx.Response(200, json=orders if orders is not None else ORDERS, headers=head)


def run(engine, targets, client, **kwargs):
    async def go():
        async with client:
            return await collect.run_collection(engine, SETTINGS, targets, client=client, **kwargs)

    return asyncio.run(go())


class TestTargets:
    """Из чего складывается обход."""

    def test_full_sweep_size(self):
        """27 газов × 2 формы × 5 хабов = 270 запросов за цикл (ESI §1)."""
        assert len(collect.build_targets()) == 270

    def test_filter_by_hub(self):
        assert len(collect.build_targets(only_hub="jita")) == 54

    def test_filter_by_gas(self):
        assert len(collect.build_targets(only_gas="fullerite_c320")) == 10

    def test_unknown_hub(self):
        with pytest.raises(SystemExit):
            collect.build_targets(only_hub="perimeter")

    def test_unknown_gas(self):
        with pytest.raises(SystemExit):
            collect.build_targets(only_gas="veldspar")


class TestHappyPath:
    """Обычный цикл."""

    def test_writes_both_sides(self, engine, targets):
        stats = run(engine, targets, client_returning(ok_response()))
        assert stats.written == 2  # sell и buy на каждый запрос
        assert stats.errors == 0
        assert stats.status == "ok"
        assert stats.exit_code == collect.EXIT_OK

    def test_ladder_keeps_min_volume(self, engine, targets):
        """min_volume обязан доехать до базы: без него на чтении нечем
        отсечь buy-ордера, которые физически не исполнить."""
        run(engine, targets, client_returning(ok_response()))
        with session_scope(engine) as session:
            buy = session.scalar(select(MarketSnapshot).where(MarketSnapshot.side == "buy"))
            assert load_ladder(buy.ladder) == [(pytest.approx(2400.00), 5000, 100)]

    def test_price_kept_exactly(self, engine, targets):
        run(engine, targets, client_returning(ok_response()))
        with session_scope(engine) as session:
            sell = session.scalar(select(MarketSnapshot).where(MarketSnapshot.side == "sell"))
            assert str(load_ladder(sell.ladder)[0][0]) == "2750.10"

    def test_records_run(self, engine, targets):
        run(engine, targets, client_returning(ok_response()))
        with session_scope(engine) as session:
            record = session.scalar(select(CollectionRun))
            assert record.status == "ok"
            assert record.requests_made == 1
            assert record.snapshots_written == 2
            assert record.finished_at is not None

    def test_etag_saved_and_sent_back(self, engine, targets):
        """ETag сохраняется и в следующий раз уходит в If-None-Match."""
        run(engine, targets, client_returning(ok_response(etag='"v1"')))
        sent: list[httpx.Request] = []
        run(engine, targets, client_returning(httpx.Response(304), record=sent))
        assert sent[0].headers.get("if-none-match") == '"v1"'


class TestSavingRequests:
    """Две ветки, ради которых считался бюджет токенов."""

    def test_fresh_snapshot_skips_request(self, engine, targets):
        """Пока не истёк expires, ESI отдаст то же самое — запрос не нужен."""
        future = (utcnow() + timedelta(minutes=5)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        run(engine, targets, client_returning(ok_response(expires=future)))

        sent: list[httpx.Request] = []
        stats = run(engine, targets, client_returning(ok_response(), record=sent))
        assert sent == []
        assert stats.skipped_fresh == 1
        assert stats.requests_made == 0

    def test_not_modified_writes_nothing(self, engine, targets):
        """304 стоит вдвое дешевле и означает «прежний срез в силе»."""
        run(engine, targets, client_returning(ok_response()))
        stats = run(engine, targets, client_returning(httpx.Response(304)))
        assert stats.not_modified == 1
        assert stats.written == 0
        with session_scope(engine) as session:
            assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 2

    def test_not_modified_confirms_freshness(self, engine, targets):
        """304 — это «твоя копия актуальна», а не «данные протухли».

        Пока время среза оставалось временем первой загрузки, страница
        показывала «собраны сутки назад» и предупреждала об устаревании
        на данных, которые ESI только что подтвердил."""
        run(engine, targets, client_returning(ok_response()))
        with session_scope(engine) as session:
            for row in session.scalars(select(MarketSnapshot)):
                row.collected_at = utcnow() - timedelta(days=1)

        run(engine, targets, client_returning(httpx.Response(304)))

        with session_scope(engine) as session:
            ages = [
                utcnow() - row.collected_at
                for row in session.scalars(select(MarketSnapshot))
            ]
        assert ages and all(age < timedelta(minutes=1) for age in ages)

    def test_not_modified_extends_expires(self, engine, targets):
        """Срок годности из ответа 304 тоже в силе: иначе следующий цикл
        пойдёт за этим срезом впустую."""
        run(engine, targets, client_returning(ok_response()))
        future = (utcnow() + timedelta(minutes=5)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        run(
            engine,
            targets,
            client_returning(httpx.Response(304, headers={"expires": future})),
        )

        sent: list[httpx.Request] = []
        stats = run(engine, targets, client_returning(ok_response(), record=sent))
        assert sent == []
        assert stats.skipped_fresh == 1

    def test_not_modified_without_snapshot_is_harmless(self, engine, targets):
        """Подтверждать нечего — это не повод падать."""
        stats = run(engine, targets, client_returning(httpx.Response(304)))
        assert stats.not_modified == 1
        assert stats.errors == 0


class TestFailures:
    """Частичный сбой не должен ни ронять цикл, ни писать нули."""

    def test_one_failure_does_not_stop_others(self, engine):
        """Сырому газу ESI отвечает 500, сжатому — нормально."""
        pair = collect.build_targets(only_hub="jita", only_gas="fullerite_c320")
        assert len(pair) == 2
        client = client_returning(
            ok_response(),
            by_type={C320.raw_type_id: httpx.Response(500)},
        )
        stats = run(engine, pair, client)
        assert stats.errors == 1        # сырой не дался даже с ретраями
        assert stats.written == 2       # сжатый записан
        assert stats.status == "partial"
        assert stats.exit_code == collect.EXIT_PARTIAL

    def test_5xx_is_retried(self, engine, targets):
        """Правило проекта: до двух повторов на 5xx. Они ничего не стоят
        по токенам — CCP берёт за 5xx ноль."""
        sent: list[httpx.Request] = []
        run(engine, targets, client_returning(httpx.Response(503), record=sent))
        assert len(sent) == 3  # первая попытка и два повтора

    def test_previous_snapshot_survives_failure(self, engine, targets):
        """Ноль вместо цены хуже отсутствия цены: старый срез остаётся."""
        run(engine, targets, client_returning(ok_response()))
        run(engine, targets, client_returning(httpx.Response(404)))
        with session_scope(engine) as session:
            rows = session.scalars(select(MarketSnapshot)).all()
            assert len(rows) == 2
            assert all(row.order_count > 0 for row in rows)

    def test_failure_text_lands_in_run_note(self, engine, targets):
        run(engine, targets, client_returning(httpx.Response(404)))
        with session_scope(engine) as session:
            assert "404" in session.scalar(select(CollectionRun)).note


class TestStopConditions:
    """Когда цикл обязан прекратиться целиком."""

    def test_rate_limit_aborts(self, engine):
        """429 по одной цели означает, что и остальные получат то же самое."""
        pair = collect.build_targets(only_hub="jita", only_gas="fullerite_c320")
        stats = run(engine, pair, client_returning(httpx.Response(429, headers={"retry-after": "60"})))
        assert stats.status == "aborted"
        assert stats.exit_code == collect.EXIT_ABORTED
        assert stats.written == 0

    def test_error_limit_floor_aborts(self, engine):
        """Упереться в лимит ошибок значит, что ESI начнёт отбрасывать всё подряд."""
        pair = collect.build_targets(only_hub="jita", only_gas="fullerite_c320")
        low = ok_response(headers={"x-esi-error-limit-remain": "3"})
        stats = run(engine, pair, client_returning(low))
        assert stats.status == "aborted"
        assert "Лимит ошибок" in stats.aborted_reason

    def test_healthy_error_limit_does_not_abort(self, engine, targets):
        plenty = ok_response(headers={"x-esi-error-limit-remain": "95"})
        assert run(engine, targets, client_returning(plenty)).status == "ok"


class TestDryRun:
    """--dry-run ходит в ESI, но в базу не пишет."""

    def test_nothing_written(self, engine, targets):
        stats = run(engine, targets, client_returning(ok_response()), dry_run=True)
        assert stats.written == 2  # столько было бы записано
        with session_scope(engine) as session:
            assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 0
            assert session.scalar(select(func.count()).select_from(CollectionRun)) == 0


class TestPrune:
    """Уборка старых срезов: 270 строк за цикл иначе копятся вечно."""

    def test_keeps_last_n(self, engine):
        with session_scope(engine) as session:
            for minutes in range(6):
                session.add(
                    MarketSnapshot(
                        hub_key="jita", type_id=1, side="sell",
                        collected_at=utcnow() - timedelta(minutes=minutes),
                        ladder="[]",
                    )
                )
        removed = collect.prune(engine, keep=2)
        assert removed == 4
        with session_scope(engine) as session:
            assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 2

    def test_keeps_newest(self, engine):
        newest = utcnow()
        with session_scope(engine) as session:
            session.add_all([
                MarketSnapshot(hub_key="jita", type_id=1, side="sell", ladder="[]",
                               collected_at=newest - timedelta(hours=1), order_count=1),
                MarketSnapshot(hub_key="jita", type_id=1, side="sell", ladder="[]",
                               collected_at=newest, order_count=42),
            ])
        collect.prune(engine, keep=1)
        with session_scope(engine) as session:
            assert session.scalar(select(MarketSnapshot)).order_count == 42

    def test_separate_per_triple(self, engine):
        """Уборка идёт по каждой тройке отдельно, а не по таблице целиком."""
        with session_scope(engine) as session:
            for side in ("sell", "buy"):
                for minutes in range(3):
                    session.add(
                        MarketSnapshot(hub_key="jita", type_id=1, side=side, ladder="[]",
                                       collected_at=utcnow() - timedelta(minutes=minutes))
                    )
        collect.prune(engine, keep=1)
        with session_scope(engine) as session:
            assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 2


class TestLock:
    """Блокировка от наложения запусков."""

    def test_second_run_refused(self, tmp_path):
        path = tmp_path / "collect.lock"
        with file_lock(path):
            with pytest.raises(AlreadyRunning):
                with file_lock(path):
                    pass

    def test_released_after_block(self, tmp_path):
        path = tmp_path / "collect.lock"
        with file_lock(path):
            assert path.exists()
        assert not path.exists()

    def test_released_after_exception(self, tmp_path):
        """Падение сбора не должно заблокировать все следующие запуски."""
        path = tmp_path / "collect.lock"
        with pytest.raises(ValueError), file_lock(path):
            raise ValueError("сбор упал")
        assert not path.exists()

    def test_broken_lock_file_still_reported(self, tmp_path):
        """Файл блокировки мог оставить кто угодно. Подавиться его кодировкой
        значит уронить сбор вместо честного «уже идёт»."""
        path = tmp_path / "collect.lock"
        path.write_bytes(b"pid=1 started=\xf1\xe5\xe9\xf7\xe0\xf1")
        with pytest.raises(AlreadyRunning):
            with file_lock(path):
                pass

    def test_stale_lock_taken_over(self, tmp_path, monkeypatch):
        """Иначе одно падение процесса остановило бы сбор цен навсегда."""
        from app.jobs import lock as lock_module

        path = tmp_path / "collect.lock"
        path.write_text("pid=999999 started=давно", encoding="utf-8")
        monkeypatch.setattr(lock_module, "_is_stale", lambda _p: True)
        with file_lock(path):
            pass
