"""Сбор цен по расписанию (ROADMAP, этап 7).

Запуск:

    python -m app.jobs.collect

Расписание задаёт системный cron, а не приложение. Пользователи к ESI не
обращаются вообще: они читают готовые срезы из базы. Смысл именно в этом —
нагрузка на ESI перестаёт зависеть от числа пользователей.

Бюджет запросов, полностью посчитанный в docs/ESI.md §2: полный обход это
27 газов × 2 формы × 5 хабов = 270 запросов, то есть 540 токенов из 12 000
за пятнадцатиминутное окно. При обходе раз в 30 минут это 4.5% лимита.

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
from datetime import timedelta
from pathlib import Path

import httpx
from sqlalchemy import Engine, delete, select

from app.config import BASE_DIR, build_config
from app.core import catalog
from app.core.models import Hub, OrderSide
from app.db import (
    CollectionRun,
    MarketSnapshot,
    create_db_engine,
    dump_ladder,
    session_scope,
    utcnow,
)
from app.jobs.lock import AlreadyRunning, file_lock
from app.services import orderbook
from app.services.esi import (
    EsiSettings,
    OrdersSnapshot,
    RateLimitedError,
    fetch_orders_conditional,
)

LOCK_PATH = BASE_DIR / "var" / "collect.lock"

# Сколько запросов к ESI держим в воздухе одновременно. Пять — по числу
# хабов: параллелить сильнее незачем, полный обход и так укладывается
# в минуту, а вежливость к чужому API стоит дороже пары секунд.
CONCURRENCY = 5

# Сколько ошибок должно остаться в окне ESI, чтобы продолжать. Ниже этого
# порога цикл прекращается сам: упереться в лимит означает, что ESI начнёт
# отбрасывать все запросы, включая чужие запросы этого же приложения.
ERROR_LIMIT_FLOOR = 10

# Сколько срезов хранить на каждую тройку (хаб, тип, сторона). Один нужен
# для работы, остальные — короткая история на случай разбора полётов.
KEEP_SNAPSHOTS = 3

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_ABORTED = 2

log = logging.getLogger("collect")


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
    """Одна пара «хаб + тип газа»: ровно один запрос к ESI."""

    hub: Hub
    type_id: int
    gas_key: str
    form: str  # raw | compressed


def build_targets(only_hub: str | None = None, only_gas: str | None = None) -> list[Target]:
    """Все пары, которые нужно обойти. Фильтры — только для отладки."""
    hubs = [h for h in catalog.hubs() if only_hub is None or h.key == only_hub]
    if only_hub and not hubs:
        raise SystemExit(f"Неизвестный хаб: {only_hub!r}")

    gases = [g for g in catalog.gases() if only_gas is None or g.key == only_gas]
    if only_gas and not gases:
        raise SystemExit(f"Неизвестный газ: {only_gas!r}")

    targets: list[Target] = []
    for hub in hubs:
        for gas in gases:
            for form, type_id in (("raw", gas.raw_type_id), ("compressed", gas.compressed_type_id)):
                if type_id is None:
                    continue  # неизвестный ID пропускаем молча: выдумывать нельзя
                targets.append(Target(hub=hub, type_id=int(type_id), gas_key=gas.key, form=form))
    return targets


def _known_state(engine: Engine, targets: Sequence[Target]) -> dict[tuple[str, int], tuple[str | None, object]]:
    """ETag и expires последнего среза по каждой паре (хаб, тип).

    Один запрос вместо 270: срезов на цикл ровно столько же, и ходить в базу
    за каждым было бы расточительно.
    """
    if not targets:
        return {}
    state: dict[tuple[str, int], tuple[str | None, object]] = {}
    with session_scope(engine) as session:
        rows = session.execute(
            select(
                MarketSnapshot.hub_key,
                MarketSnapshot.type_id,
                MarketSnapshot.etag,
                MarketSnapshot.expires_at,
                MarketSnapshot.collected_at,
            ).order_by(MarketSnapshot.collected_at.desc())
        ).all()
    for hub_key, type_id, etag, expires_at, _collected in rows:
        state.setdefault((hub_key, int(type_id)), (etag, expires_at))
    return state


async def _fetch_one(
    client: httpx.AsyncClient,
    settings: EsiSettings,
    target: Target,
    etag: str | None,
    semaphore: asyncio.Semaphore,
) -> OrdersSnapshot:
    async with semaphore:
        return await fetch_orders_conditional(
            client,
            settings,
            region_id=target.hub.region_id,
            type_id=target.type_id,
            etag=etag,
        )


def _store(
    engine: Engine,
    run_id: int | None,
    target: Target,
    snapshot: OrdersSnapshot,
) -> int:
    """Пишет два среза — sell и buy. Возвращает, сколько записал."""
    assert snapshot.orders is not None
    now = utcnow()
    written = 0
    with session_scope(engine) as session:
        for side in (OrderSide.SELL, OrderSide.BUY):
            levels = orderbook.ladder_from_orders(snapshot.orders, target.hub, side)
            session.add(
                MarketSnapshot(
                    hub_key=target.hub.key,
                    type_id=target.type_id,
                    side=side.value,
                    collected_at=now,
                    expires_at=snapshot.expires_at,
                    ladder=dump_ladder(levels),
                    total_volume=sum(volume for _price, volume, _mv in levels),
                    order_count=len(levels),
                    etag=snapshot.etag,
                    run_id=run_id,
                )
            )
            written += 1
    return written


def prune(engine: Engine, keep: int = KEEP_SNAPSHOTS) -> int:
    """Оставляет последние keep срезов на каждую тройку. Возвращает число удалённых.

    Без уборки таблица растёт на 270 строк за цикл — за сутки это 13 тысяч
    строк ради данных, которые устарели через пять минут.
    """
    removed = 0
    with session_scope(engine) as session:
        keys = session.execute(
            select(MarketSnapshot.hub_key, MarketSnapshot.type_id, MarketSnapshot.side).distinct()
        ).all()
        for hub_key, type_id, side in keys:
            survivors = session.scalars(
                select(MarketSnapshot.id)
                .where(
                    MarketSnapshot.hub_key == hub_key,
                    MarketSnapshot.type_id == type_id,
                    MarketSnapshot.side == side,
                )
                .order_by(MarketSnapshot.collected_at.desc(), MarketSnapshot.id.desc())
                .limit(keep)
            ).all()
            result = session.execute(
                delete(MarketSnapshot).where(
                    MarketSnapshot.hub_key == hub_key,
                    MarketSnapshot.type_id == type_id,
                    MarketSnapshot.side == side,
                    MarketSnapshot.id.notin_(survivors),
                )
            )
            removed += result.rowcount or 0
    return removed


async def run_collection(
    engine: Engine,
    settings: EsiSettings,
    targets: Sequence[Target],
    *,
    dry_run: bool = False,
    client: httpx.AsyncClient | None = None,
) -> Stats:
    """Один полный цикл сбора."""
    stats = Stats()
    if not targets:
        return stats

    state = _known_state(engine, targets)
    now = utcnow()

    pending: list[Target] = []
    for target in targets:
        etag, expires_at = state.get((target.hub.key, target.type_id), (None, None))
        if expires_at is not None and expires_at > now:
            # ESI отдаст ровно то же самое: запрос был бы выброшенным токеном
            stats.skipped_fresh += 1
            continue
        pending.append(target)

    if not pending:
        log.info("Все срезы свежие, запросов не потребовалось")
        return stats

    run_id: int | None = None
    if not dry_run:
        with session_scope(engine) as session:
            run = CollectionRun(started_at=utcnow(), status="running")
            session.add(run)
            session.flush()
            run_id = run.id

    semaphore = asyncio.Semaphore(CONCURRENCY)
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=settings.timeout)

    try:
        results = await asyncio.gather(
            *(
                _fetch_one(http, settings, target, state.get((target.hub.key, target.type_id), (None, None))[0], semaphore)
                for target in pending
            ),
            return_exceptions=True,
        )
    finally:
        if own_client:
            await http.aclose()

    for target, result in zip(pending, results, strict=True):
        if isinstance(result, RateLimitedError):
            # Один отказ по лимиту означает, что и остальные получат то же.
            # Продолжать — значит гарантированно упереться в стену.
            stats.aborted_reason = str(result)
            log.error("Цикл прерван: %s", result)
            break
        if isinstance(result, BaseException):
            stats.errors += 1
            stats.failures.append(f"{target.hub.key}/{target.type_id}: {result}")
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
            continue
        if not result.ok:
            # Прежний срез остаётся в силе: ноль вместо цены хуже отсутствия цены
            stats.errors += 1
            stats.failures.append(f"{target.hub.key}/{target.type_id}: {result.error}")
            continue
        if dry_run:
            stats.written += 2
            continue
        stats.written += _store(engine, run_id, target, result)

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
        prog="python -m app.jobs.collect",
        description="Сбор цен на газ из ESI в базу. Запускается системным cron.",
    )
    parser.add_argument("--dry-run", action="store_true", help="сходить в ESI, но ничего не писать")
    parser.add_argument("--only-hub", metavar="КЛЮЧ", help="только один хаб, например jita")
    parser.add_argument("--only-gas", metavar="КЛЮЧ", help="только один газ, например fullerite_c320")
    parser.add_argument("--verbose", action="store_true", help="подробный лог")
    parser.add_argument("--no-prune", action="store_true", help="не удалять старые срезы")
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
                if not args.dry_run and not args.no_prune:
                    removed = prune(engine)
                    log.info("Удалено устаревших срезов: %d", removed)
            finally:
                engine.dispose()
    except AlreadyRunning as exc:
        log.error("%s", exc)
        return EXIT_ABORTED

    log.info(
        "Итог: запросов %d, записано срезов %d, без изменений %d, "
        "пропущено свежих %d, ошибок %d, статус %s",
        stats.requests_made,
        stats.written,
        stats.not_modified,
        stats.skipped_fresh,
        stats.errors,
        stats.status,
    )
    for failure in stats.failures[:10]:
        log.warning("не получено: %s", failure)
    return stats.exit_code


if __name__ == "__main__":
    sys.exit(main())
