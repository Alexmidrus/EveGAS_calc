"""Замер коридора по истории на живых данных (ROADMAP, этап 11.3).

Разовый инструмент разработчика. В рантайме не участвует, приложением
не импортируется — как и `build_gases.py`.

Зачем. Пороги коридора и мягкой пометки изначально были взяты
умозрительно, а коридор умеет оставить хаб вовсе без цены. Включать такой
фильтр, не посмотрев глазами, что именно он выкидывает, нельзя.

Запуск (нужны собранные срезы и история):

    python -m tools.measure_history_band
    python -m tools.measure_history_band --low 3 --high 8 --show 30
"""

import argparse
from collections import defaultdict
from sqlalchemy import select

from app.config import build_config
from app.core import catalog
from app.core.constants import (
    HISTORY_BAND_HIGH_FACTOR,
    HISTORY_BAND_LOW_FACTOR,
    HISTORY_WARN_FACTOR,
)
from app.db import (
    MarketHistory,
    MarketSnapshot,
    create_db_engine,
    load_ladder,
    session_scope,
)
from app.services.history import HistoryDay, summarize


def load_history(session) -> dict[tuple[int, int], list[HistoryDay]]:
    rows = session.scalars(select(MarketHistory)).all()
    history: dict[tuple[int, int], list[HistoryDay]] = defaultdict(list)
    for row in rows:
        history[(int(row.region_id), int(row.type_id))].append(
            HistoryDay(
                date=row.date,
                average=float(row.average),
                highest=float(row.highest),
                lowest=float(row.lowest),
                volume=int(row.volume),
            )
        )
    return history


def latest_snapshots(session) -> dict[tuple[str, int, str], MarketSnapshot]:
    """Последний срез на каждую тройку (хаб, тип, сторона)."""
    rows = session.scalars(
        select(MarketSnapshot).order_by(MarketSnapshot.collected_at.desc())
    ).all()
    latest: dict[tuple[str, int, str], MarketSnapshot] = {}
    for row in rows:
        latest.setdefault((row.hub_key, int(row.type_id), row.side), row)
    return latest


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.measure_history_band")
    parser.add_argument("--low", type=float, default=HISTORY_BAND_LOW_FACTOR)
    parser.add_argument("--high", type=float, default=HISTORY_BAND_HIGH_FACTOR)
    parser.add_argument("--warn", type=float, default=HISTORY_WARN_FACTOR)
    parser.add_argument("--show", type=int, default=15, help="сколько отброшенных показать")
    args = parser.parse_args()

    engine = create_db_engine(build_config())
    hubs = {hub.key: hub for hub in catalog.hubs()}
    names = {}
    for gas in catalog.gases():
        for form, type_id in (("сырой", gas.raw_type_id), ("сжатый", gas.compressed_type_id)):
            if type_id is not None:
                names[int(type_id)] = f"{gas.name} ({form})"

    with session_scope(engine) as session:
        history = load_history(session)
        snapshots = latest_snapshots(session)

        books = 0
        no_history = 0
        levels_total = 0
        dropped_total = 0
        emptied: list[str] = []
        dropped_rows: list[tuple[float, float, str]] = []
        warned = 0

        for (hub_key, type_id, side), row in sorted(snapshots.items()):
            hub = hubs.get(hub_key)
            if hub is None:
                continue
            levels = load_ladder(row.ladder)
            if not levels:
                continue
            books += 1
            levels_total += len(levels)

            stats = summarize(history.get((hub.region_id, type_id), []))
            if not stats.usable:
                no_history += 1
                continue

            kept = [lv for lv in levels if not stats.out_of_band(float(lv[0]), args.low, args.high)]
            dropped = [lv for lv in levels if stats.out_of_band(float(lv[0]), args.low, args.high)]
            dropped_total += len(dropped)
            label = f"{hub_key:8} {side:4} {names.get(type_id, type_id)}"
            for price, volume, _mv in dropped:
                dropped_rows.append((float(price), stats.reference or 0.0, f"{label} × {volume}"))
            if not kept:
                emptied.append(f"{label}: было {len(levels)} уровней")
            elif kept and stats.unconfirmed(float(kept[0][0]), args.warn):
                warned += 1

        print(
            f"Коридор: снизу опора/{args.low:g}, сверху опора×{args.high:g}; "
            f"мягкий порог ×{args.warn:g}"
        )
        print()
        print(f"книг с данными:            {books}")
        print(f"из них без пригодной истории: {no_history}")
        print(f"уровней всего:             {levels_total}")
        print(f"отброшено коридором:       {dropped_total}"
              f" ({dropped_total / levels_total * 100:.2f}%)" if levels_total else "")
        print(f"книг обнулено целиком:     {len(emptied)}")
        print(f"лучших цен под мягкой пометкой: {warned}")

        if emptied:
            print("\nКниги, обнулённые коридором:")
            for line in emptied[: args.show]:
                print("  ", line)

        if dropped_rows:
            print(f"\nСамые далёкие от опоры (из {len(dropped_rows)}):")
            dropped_rows.sort(key=lambda r: abs((r[0] or 1) / (r[1] or 1) - 1), reverse=True)
            for price, reference, label in dropped_rows[: args.show]:
                ratio = price / reference if reference else float("inf")
                print(f"   {price:>14,.2f}  опора {reference:>12,.2f}  ×{ratio:>10.4f}  {label}")

    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
