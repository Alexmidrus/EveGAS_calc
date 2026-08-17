"""Блокировка от одновременного запуска задачи.

Задача про cron: расписание не знает, закончился ли прошлый запуск. Если сбор
занял больше периода, второй cron поднимет ещё один — и оба пойдут в ESI,
удвоив расход токенов.

Реализация намеренно простая: файл, создаваемый атомарно через O_EXCL. Никакой
СУБД-блокировки, потому что сбор должен уметь сказать «уже идёт» ещё до того,
как открыл соединение с базой.
"""

import os
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

# Через сколько считать оставшийся файл мусором от упавшего процесса.
# Полный обход укладывается в минуты, час — заведомо больше любого нормального.
STALE_AFTER_SECONDS = 3600


class AlreadyRunning(RuntimeError):
    """Задача уже выполняется другим процессом."""


def _describe(path: Path) -> str:
    """Содержимое файла блокировки для сообщения.

    Читается терпимо: файл мог оставить кто угодно, и подавиться его
    кодировкой означало бы уронить сбор цен вместо честного «уже идёт».
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip() or "без подробностей"
    except OSError:
        return "без подробностей"


def _is_stale(path: Path) -> bool:
    """Файл остался от процесса, который давно умер, не убрав за собой."""
    try:
        age = datetime.now(UTC).timestamp() - path.stat().st_mtime
    except OSError:
        return False
    return age > STALE_AFTER_SECONDS


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Захватывает блокировку на время блока. Занято — AlreadyRunning.

    Ожидания нет намеренно: cron придёт снова через период, а копящаяся
    очередь ждущих процессов — худшее, что может случиться с задачей,
    которая ходит во внешний API.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and _is_stale(path):
        # Процесс, создавший файл, умер больше часа назад: забираем блокировку,
        # иначе одно падение остановило бы сбор цен навсегда
        path.unlink(missing_ok=True)

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise AlreadyRunning(
            f"Сбор уже идёт ({_describe(path)}). Файл блокировки: {path}"
        ) from None

    try:
        started = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
        os.write(fd, f"pid={os.getpid()} started={started}Z".encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)
