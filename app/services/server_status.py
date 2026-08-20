"""Состояние игрового сервера, прочитанное из базы.

Приложение в ESI не ходит: /status/ спрашивает сборщик раз в цикл и кладёт
строку в ``esi_status`` (CLAUDE.md, ESI §8). Здесь — чтение последней строки
и превращение её в то, что показывает чип в шапке.

Модуль намеренно маленький и без Flask: свежесть и формулировки проверяются
тестами без поднятого приложения.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, select

from app.db import EsiStatus, session_scope, utcnow

# Через сколько минут после проверки её результат перестаёт что-либо значить.
# Сборщик ходит раз в полчаса, и один пропущенный цикл — ещё не повод объявлять
# состояние неизвестным; два подряд — уже повод. Показывать «online» по строке
# трёхдневной давности нельзя: это ровно то враньё, которого проект избегает.
STATUS_MAX_AGE_MINUTES = 70


@dataclass(frozen=True, slots=True)
class ServerStatus:
    """То, что чип показывает на экране.

    ``state`` — одно из четырёх, и они не сводятся друг к другу:

    * ``online``  — сервер отвечает, игроки заходят;
    * ``vip``     — сервер поднят, но пускает только избранных: после патча
                    он уже отвечает, а обычный игрок войти ещё не может;
    * ``offline`` — мы спросили и получили отказ: ESI молчит или сервер лежит;
    * ``unknown`` — мы давно не спрашивали или не спрашивали никогда.
      Это не «сервер в порядке» и не «сервер лежит», а «мы не знаем», и
      подменять незнание одним из двух других состояний нельзя.
    """

    state: str = "unknown"
    players: int | None = None
    checked_at: datetime | None = None
    error: str | None = None

    @property
    def known(self) -> bool:
        return self.state != "unknown"

    @property
    def label(self) -> str:
        """Короткая подпись в чипе."""
        return {
            "online": "online",
            "vip": "VIP",
            "offline": "не отвечает",
        }.get(self.state, "неизвестно")

    @property
    def title(self) -> str:
        """Подсказка: чип узкий, а сказать нужно и что это, и откуда взято."""
        base = "Состояние сервера Tranquility. Снимает сборщик по расписанию — "
        if self.state == "online":
            return base + "приложение в ESI по действию пользователя не ходит."
        if self.state == "vip":
            return (
                "Сервер поднят, но в режиме VIP: обычные игроки войти пока "
                "не могут. " + base + "приложение в ESI по действию пользователя не ходит."
            )
        if self.state == "offline":
            reason = f" Причина: {self.error}" if self.error else ""
            return f"ESI не ответил на запрос о состоянии сервера.{reason}"
        return (
            "Состояние сервера неизвестно: последняя проверка слишком старая "
            "или сборщик ещё ни разу не отработал."
        )


def load(
    engine: Engine,
    *,
    max_age_minutes: int = STATUS_MAX_AGE_MINUTES,
    now: datetime | None = None,
) -> ServerStatus:
    """Последняя проверка состояния сервера.

    Недоступная база здесь молчит, а не роняет страницу: чип — украшение
    шапки, а расчёт обязан работать всегда, в том числе анонимно и без данных.
    """
    try:
        with session_scope(engine) as session:
            row = session.scalar(
                select(EsiStatus).order_by(EsiStatus.checked_at.desc()).limit(1)
            )
            if row is None:
                return ServerStatus()
            checked_at, reachable, players, vip, error = (
                row.checked_at,
                row.reachable,
                row.players,
                row.vip,
                row.error,
            )
    except Exception:  # noqa: BLE001 — важен факт недоступности, а не тип
        return ServerStatus()

    moment = now or utcnow()
    if checked_at is None or moment - checked_at > timedelta(minutes=max_age_minutes):
        # Строка есть, но она устарела: «мы не знаем» честнее, чем показания
        # позавчерашней проверки
        return ServerStatus(checked_at=checked_at)

    if not reachable:
        return ServerStatus(state="offline", checked_at=checked_at, error=error)
    if vip:
        return ServerStatus(state="vip", players=players, checked_at=checked_at)
    return ServerStatus(state="online", players=players, checked_at=checked_at)
