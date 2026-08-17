"""Кэш ответов ESI: обычный dict с TTL, без внешних зависимостей.

База данных проекту запрещена (CLAUDE.md), поэтому кэш живёт в памяти процесса
и умирает вместе с ним. Ключ — пара (region_id, type_id), значение — разобранный
список ордеров.

Кэшировать агрессивнее самого ESI нельзя: он отдаёт заголовок expires и держит
ответ 5 минут. Срок жизни записи берётся из этого заголовка, а когда его нет —
из значения по умолчанию (config.ESI_CACHE_TTL).
"""

import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """Словарь с временем жизни записи.

    Часы вынесены параметром, чтобы тесты не спали по-настоящему.
    Класс потокобезопасен: waitress обслуживает запросы в нескольких потоках.
    """

    def __init__(
        self,
        default_ttl: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if default_ttl <= 0:
            raise ValueError(f"TTL должен быть больше нуля, получено: {default_ttl}")
        self._default_ttl = default_ttl
        self._clock = clock
        self._data: dict[K, tuple[V, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: K) -> V | None:
        """Значение или None, если записи нет либо она протухла."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if self._clock() >= expires_at:
                del self._data[key]
                return None
            return value

    def set(self, key: K, value: V, ttl: float | None = None) -> None:
        """Кладёт значение. ttl=None — время жизни по умолчанию.

        Неположительный ttl означает «уже протухло»: запись не сохраняем,
        а прежнюю выкидываем, чтобы не отдать устаревшее.
        """
        lifetime = self._default_ttl if ttl is None else ttl
        with self._lock:
            if lifetime <= 0:
                self._data.pop(key, None)
                return
            self._data[key] = (value, self._clock() + lifetime)

    def clear(self) -> None:
        """Полная очистка — нужна тестам и кнопке принудительного обновления."""
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        """Число живых записей; заодно вычищает протухшие."""
        with self._lock:
            now = self._clock()
            self._data = {k: v for k, v in self._data.items() if now < v[1]}
            return len(self._data)
