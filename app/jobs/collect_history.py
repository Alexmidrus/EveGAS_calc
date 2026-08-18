"""Сбор истории сделок по расписанию (ROADMAP, этап 11).

Запуск:

    python -m app.jobs.collect_history

Второй сборщик проекта, и путать его с первым не надо. ``collect.py`` ходит
за стаканом — что выставлено прямо сейчас — каждые 30 минут. Этот ходит за
историей сделок: по чему рынок реально торговал. ESI обновляет её раз в сутки,
в 11:05 UTC (docs/ESI.md §5.3), поэтому расписание здесь суточное, а не
получасовое: чаще — значит гарантированно получить тот же самый ответ.

Цена обхода: те же 270 целей, но один раз в день. В группы рейт-лимита этот
эндпоинт не входит вовсе (проверено 18.08.2026), однако лимит ошибок общий,
и следить за ним обязательно.

Данные региональные: разбивки по станциям у истории нет. Поэтому цель здесь —
пара «регион + тип», а не «хаб + тип», и два хаба в одном регионе дали бы
один запрос, а не два.

Коды возврата — для cron:

    0  всё собрано
    1  часть данных не получена, остальное записано
    2  цикл прерван: ESI ограничил частоту или задача уже выполняется
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from sqlalchemy import Engine, delete, select

from app.config import BASE_DIR, build_config
from app.core import catalog
from app.core.constants import HISTORY_KEEP_DAYS
from app.db import (
    CollectionRun,
    MarketHistory,
    MarketHistoryState,
    create_db_engine,
    session_scope,
    utcnow,
)
from app.jobs.lock import AlreadyRunning, file_lock
from app.services.esi import (
    EsiSettings,
    HistorySnapshot,
    RateLimitedError,
    fetch_history_conditional,
)

# Свой файл блокировки: сборщики независимы и не должны мешать друг другу.
LOCK_PATH = BASE_DIR / "var" / "collect_history.lock"

CONCURRENCY = 5

# Тот же порог, что и у сбора стакана: лимит ошибок у ESI общий на приложение,
# и упереться в него означает, что отбрасываться начнут все запросы.
ERROR_LIMIT_FLOOR = 10

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_ABORTED = 2

log = logging.getLogger("collect_history")


@dataclass
class Stats:
    """Счётчики одного запуска. Ложатся в collection_run как есть."""

    requests_made: int = 0
    errors: int = 0
    written: int = 0
    not_modified: int = 0
    skipped_fresh: int = 0
    aborted_reason: str | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.aborted_reason:
            return "aborted"
        return "partial" if self.errors else "ok"

    @property
    def exit_code(self) -> int:
        if self.aborted_reason:
            return EXIT_ABORTED
        return EXIT_PARTIAL if self.errors else EXIT_OK


@dataclass(frozen=True, slots=True)
class Target:
    """Одна пара «регион + тип газа»: ровно один запрос к ESI."""

    region_id: int
    type_id: int
    gas_key: str
    form: str  # raw | compressed

    @property
    def label(self) -> str:
        return f"{self.region_id}/{self.type_id}"


def build_targets(only_hub: str | None = None, only_gas: str | None = None) -> list[Target]:
    """Все пары «регион + тип». Фильтры — только для отладки.

    Регионы берутся из справочника хабов и схлопываются: история регионная,
    и два хаба в одном регионе — это один и тот же ответ ESI.
    """
    hubs = [h for h in catalog.hubs() if only_hub is None or h.key == only_hub]
    if only_hub and not hubs:
        raise SystemExit(f"Неизвестный хаб: {only_hub!r}")

    gases = [g for g in catalog.gases() if only_gas is None or g.key == only_gas]
    if only_gas and not gases:
        raise SystemExit(f"Неизвестный газ: {only_gas!r}")

    seen: set[tuple[int, int]] = set()
    targets: list[Target] = []
    for hub in hubs:
        for gas in gases:
            for form, type_id in (("raw", gas.raw_type_id), ("compressed", gas.compressed_type_id)):
                if type_id is None:
                    continue  # неизвестный ID пропускаем молча: выдумывать нельзя
                key = (hub.region_id, int(type_id))
                if key in seen:
                    continue
                seen.add(key)
                targets.append(
                    Target(
                        region_id=hub.region_id,
                        type_id=int(type_id),
                        gas_key=gas.key,
                        form=form,
                    )
                )
    return targets


def _known_state(engine: Engine) -> dict[tuple[int, int], MarketHistoryState]:
    """ETag, Last-Modified и срок годности по каждой паре — одним запросом."""
    with session_scope(engine) as session:
        rows = session.scalars(select(MarketHistoryState)).all()
        return {
            (int(row.region_id), int(row.type_id)): MarketHistoryState(
                region_id=row.region_id,
                type_id=row.type_id,
                etag=row.etag,
                last_modified=row.last_modified,
                checked_at=row.checked_at,
                expires_at=row.expires_at,
            )
            for row in rows
        }


async def _fetch_one(
    client: httpx.AsyncClient,
    settings: EsiSettings,
    target: Target,
    state: MarketHistoryState | None,
    semaphore: asyncio.Semaphore,
) -> HistorySnapshot:
    async with semaphore:
        return await fetch_history_conditional(
            client,
            settings,
            region_id=target.region_id,
            type_id=target.type_id,
            etag=state.etag if state else None,
            last_modified=state.last_modified if state else None,
        )


def _money(raw: object) -> Decimal:
    """Цена из ответа ESI в том виде, в каком её хранит база: два знака."""
    return Decimal(str(raw)).quantize(Decimal("0.01"))


def parse_days(
    days: Sequence[dict], *, keep_days: int = HISTORY_KEEP_DAYS, today: date | None = None
) -> list[dict]:
    """Отбирает из ответа ESI дни окна и приводит их к виду для базы.

    Ответ приходит целиком, около 412 дней. Хранить их все незачем: опоре
    нужна неделя, остальное — запас на разбор полётов. Лишнее отбрасывается
    здесь, при разборе, а не чистится потом отдельной уборкой.

    День, который не удалось прочитать, пропускается: ломать из-за одной
    кривой строки весь сбор по региону нельзя, а выдумывать её содержимое
    тем более.
    """
    cutoff = (today or utcnow().date()) - timedelta(days=keep_days)
    parsed: list[dict] = []
    for day in days:
        try:
            when = date.fromisoformat(str(day["date"]))
            if when < cutoff:
                continue
            parsed.append(
                {
                    "date": when,
                    "average": _money(day["average"]),
                    "highest": _money(day["highest"]),
                    "lowest": _money(day["lowest"]),
                    "volume": int(day["volume"]),
                    "order_count": int(day.get("order_count", 0)),
                }
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            log.warning("Пропущен нечитаемый день истории: %s (%s)", day, exc)
    return parsed


def _store(engine: Engine, target: Target, snapshot: HistorySnapshot) -> int:
    """Заменяет историю по паре целиком. Возвращает, сколько дней записано.

    Именно заменяет, а не дописывает: ESI отдаёт всю историю разом, и полная
    замена разом же решает три задачи — обновление вчерашнего дня, правку
    задним числом и удаление дней, вышедших за окно хранения.
    """
    assert snapshot.days is not None
    rows = parse_days(snapshot.days)
    now = utcnow()
    with session_scope(engine) as session:
        session.execute(
            delete(MarketHistory).where(
                MarketHistory.region_id == target.region_id,
                MarketHistory.type_id == target.type_id,
            )
        )
        session.add_all(
            [
                MarketHistory(
                    region_id=target.region_id,
                    type_id=target.type_id,
                    fetched_at=now,
                    **row,
                )
                for row in rows
            ]
        )
        _remember(session, target, snapshot, checked_at=now)
    return len(rows)


def _touch(engine: Engine, target: Target, snapshot: HistorySnapshot) -> None:
    """Ответ 304: данные не переписываем, но факт проверки сохраняем.

    Иначе история навсегда осталась бы с датой первой загрузки, а следующий
    цикл пошёл бы за ней впустую — срок годности из ответа тоже применяется.
    """
    with session_scope(engine) as session:
        _remember(session, target, snapshot, checked_at=utcnow())


def _remember(session, target: Target, snapshot: HistorySnapshot, *, checked_at) -> None:
    """Пишет состояние условных запросов по паре."""
    row = session.get(MarketHistoryState, (target.region_id, target.type_id))
    if row is None:
        row = MarketHistoryState(region_id=target.region_id, type_id=target.type_id)
        session.add(row)
    if snapshot.etag:
        row.etag = snapshot.etag
    if snapshot.last_modified:
        row.last_modified = snapshot.last_modified
    if snapshot.expires_at is not None:
        row.expires_at = snapshot.expires_at
    row.checked_at = checked_at


async def run_collection(
    engine: Engine,
    settings: EsiSettings,
    targets: Sequence[Target],
    *,
    dry_run: bool = False,
    client: httpx.AsyncClient | None = None,
) -> Stats:
    """Один суточный обход истории."""
    stats = Stats()
    if not targets:
        return stats

    state = _known_state(engine)
    now = utcnow()

    pending: list[Target] = []
    for target in targets:
        known = state.get((target.region_id, target.type_id))
        if known is not None and known.expires_at is not None and known.expires_at > now:
            # До 11:05 UTC ответ не изменится: запрос был бы выброшенным токеном
            stats.skipped_fresh += 1
            continue
        pending.append(target)

    if not pending:
        log.info("История свежая по всем парам, запросов не потребовалось")
        return stats

    run_id: int | None = None
    if not dry_run:
        with session_scope(engine) as session:
            run = CollectionRun(started_at=utcnow(), status="running", kind="history")
            session.add(run)
            session.flush()
            run_id = run.id

    semaphore = asyncio.Semaphore(CONCURRENCY)
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=settings.timeout)

    try:
        results = await asyncio.gather(
            *(
                _fetch_one(
                    http,
                    settings,
                    target,
                    state.get((target.region_id, target.type_id)),
                    semaphore,
                )
                for target in pending
            ),
            return_exceptions=True,
        )
    finally:
        if own_client:
            await http.aclose()

    for target, result in zip(pending, results, strict=True):
        if isinstance(result, RateLimitedError):
            # Один отказ по лимиту означает, что и остальные получат то же
            stats.aborted_reason = str(result)
            log.error("Цикл прерван: %s", result)
            break
        if isinstance(result, BaseException):
            stats.errors += 1
            stats.failures.append(f"{target.label}: {result}")
            continue

        stats.requests_made += result.requests_made

        if result.error_limit_remain is not None and result.error_limit_remain < ERROR_LIMIT_FLOOR:
            stats.aborted_reason = (
                f"Лимит ошибок ESI почти исчерпан: осталось {result.error_limit_remain}"
            )
            log.error("Цикл прерван: %s", stats.aborted_reason)
            break

        if result.not_modified:
            stats.not_modified += 1
            if not dry_run:
                _touch(engine, target, result)
            continue
        if not result.ok:
            # Прежняя история остаётся в силе: она устаревает медленно,
            # и вчерашние данные честнее пустоты
            stats.errors += 1
            stats.failures.append(f"{target.label}: {result.error}")
            continue
        if dry_run:
            stats.written += len(parse_days(result.days or []))
            continue
        stats.written += _store(engine, target, result)

    if not dry_run and run_id is not None:
        with session_scope(engine) as session:
            run = session.get(CollectionRun, run_id)
            run.finished_at = utcnow()
            run.status = stats.status
            run.requests_made = stats.requests_made
            run.errors_count = stats.errors
            run.snapshots_written = stats.written
            run.note = (stats.aborted_reason or "; ".join(stats.failures[:3]))[:500] or None

    return stats


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.jobs.collect_history",
        description=(
            "Сбор истории сделок из ESI в базу. Запускается системным cron "
            "раз в сутки после 11:05 UTC."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="сходить в ESI, но ничего не писать")
    parser.add_argument("--only-hub", metavar="КЛЮЧ", help="только регион этого хаба, например jita")
    parser.add_argument("--only-gas", metavar="КЛЮЧ", help="только один газ, например fullerite_c320")
    parser.add_argument("--verbose", action="store_true", help="подробный лог")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = build_config()
    settings = EsiSettings.from_config(config)
    targets = build_targets(only_hub=args.only_hub, only_gas=args.only_gas)
    log.info("Профиль %s, целей: %d", config["APP_ENV"], len(targets))

    try:
        with file_lock(Path(LOCK_PATH)):
            engine = create_db_engine(config)
            try:
                stats = asyncio.run(
                    run_collection(engine, settings, targets, dry_run=args.dry_run)
                )
            finally:
                engine.dispose()
    except AlreadyRunning as exc:
        log.error("%s", exc)
        return EXIT_ABORTED

    log.info(
        "Итог: запросов %d, записано дней %d, подтверждено без изменений %d, "
        "пропущено свежих %d, ошибок %d, статус %s",
        stats.requests_made,
        stats.written,
        stats.not_modified,
        stats.skipped_fresh,
        stats.errors,
        stats.status,
    )
    for failure in stats.failures[:10]:
        log.warning("Не собрано: %s", failure)

    return stats.exit_code


if __name__ == "__main__":
    sys.exit(main())
