"""Профили запуска: dev для разработки, prod для боевой работы.

Конфигурация собирается слоями, от низшего приоритета к высшему:

1. значения по умолчанию выбранного профиля (этот модуль — источник истины);
2. ``config.py`` в корне репозитория, если он есть: локальные правки, в git не попадает;
3. переменные окружения — в них на проде приходят секреты.

Валидация выполняется после сборки всех слоёв и **до** создания приложения.
Правило простое: непригодный конфиг обязан уронить запуск сразу и с внятным
текстом, а не всплыть на первом запросе пользователя посреди рабочего дня.

Модуль не импортирует Flask: его можно позвать из CLI-сборщика цен и из тестов.
"""

import os
import re
import runpy
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.version import __version__

# Корень репозитория — на уровень выше пакета app/
BASE_DIR = Path(__file__).resolve().parent.parent

DEV = "dev"
PROD = "prod"
PROFILES = (DEV, PROD)

# Минимальная длина SECRET_KEY на проде. Короткий ключ подписи сессии
# подбирается, а сессией у нас будет удостоверяться вход через EVE SSO.
SECRET_KEY_MIN_LENGTH = 32

# Столько же кэширует сам ESI. Кэшировать агрессивнее нельзя (CLAUDE.md).
ESI_MIN_CACHE_TTL = 300

# Куски незаполненного шаблона User-Agent. Безликий UA — прямой путь
# под рейт-лимит ESI, включённый 24.02.2026 (docs/ESI.md §2).
_UA_PLACEHOLDERS = ("USER", "you@example.com", "contact@example.com")

# Метка версии в User-Agent. Записанный руками номер устаревает в тот же день,
# когда версию подняли, и CCP видит враньё о том, какой клиент к ним ходит.
# Поэтому в строке пишется {version}, а подставляет его приложение.
VERSION_MARK = "{version}"

# Первый токен User-Agent по RFC 9110 — «Продукт/Версия». Разбираем только его:
# цифры дальше по строке принадлежат контакту, а не клиенту.
_UA_PRODUCT = re.compile(r"^[^/\s]+/(\d[\w.\-]*)$")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(RuntimeError):
    """Конфигурация непригодна для запуска."""


def _dev_database_url(base_dir: Path) -> str:
    """SQLite-файл рядом с репозиторием. as_posix — чтобы URL был одинаков на Windows."""
    return f"sqlite:///{(base_dir / 'var' / 'evegas_dev.sqlite3').as_posix()}"


def _defaults(profile: str, base_dir: Path) -> dict[str, Any]:
    """Значения по умолчанию профиля. None означает «обязано прийти извне»."""
    common: dict[str, Any] = {
        "APP_ENV": profile,
        "ESI_USER_AGENT": (
            f"GasLens/{VERSION_MARK} (+https://github.com/USER/GasLens; you@example.com)"
        ),
        # Дата не должна быть «в будущем»: ESI сравнивает её со своим днём
        # по UTC-11 и отвечает 400. Подробности — docs/ESI.md §2.
        "ESI_COMPATIBILITY_DATE": "2026-08-13",
        "ESI_TIMEOUT": 15.0,
        "ESI_CACHE_TTL": ESI_MIN_CACHE_TTL,
        # Со скольки минут срез считается устаревшим и помечается на экране.
        # Сбор идёт раз в 30 минут, так что 90 — это три пропущенных цикла
        # подряд: разовая осечка не должна пугать пользователя красной плашкой.
        "PRICE_MAX_AGE_MINUTES": 90,
        # Вход через EVE SSO. Без них приложение работает анонимно.
        "ESI_CLIENT_ID": None,
        "ESI_CLIENT_SECRET": None,
        "SECRET_KEY": None,
    }
    if profile == DEV:
        return common | {
            "DEBUG": True,
            "HOST": "127.0.0.1",
            "PORT": 5000,
            "DATABASE_URL": _dev_database_url(base_dir),
            # Колбэк должен совпадать с зарегистрированным у CCP посимвольно
            "SSO_REDIRECT_URI": "http://localhost:5000/sso/callback",
            # Сессия по http допустима только на своей машине
            "SESSION_COOKIE_SECURE": False,
        }
    return common | {
        "DEBUG": False,
        "HOST": "127.0.0.1",  # снаружи должен стоять nginx, наружу не слушаем
        "PORT": 8080,
        "DATABASE_URL": None,  # только из окружения, см. _validate
        "SSO_REDIRECT_URI": None,  # свой домен, приходит из окружения
        "SESSION_COOKIE_SECURE": True,
    }


def _as_bool(raw: str, name: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(
        f"Переменная окружения {name}={raw!r} не похожа на да/нет. "
        f"Допустимо: {', '.join(sorted(_TRUE | _FALSE))}."
    )


def _as_int(raw: str, name: str) -> int:
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigError(f"Переменная окружения {name}={raw!r} должна быть целым числом.") from None


def _as_float(raw: str, name: str) -> float:
    try:
        return float(raw.strip())
    except ValueError:
        raise ConfigError(f"Переменная окружения {name}={raw!r} должна быть числом.") from None


def _as_str(raw: str, name: str) -> str:  # noqa: ARG001 — единый интерфейс с остальными
    return raw


# Что можно задать переменной окружения и как это разобрать
_ENV_KEYS: dict[str, Any] = {
    "SECRET_KEY": _as_str,
    "DATABASE_URL": _as_str,
    "ESI_CLIENT_ID": _as_str,
    "ESI_CLIENT_SECRET": _as_str,
    "SSO_REDIRECT_URI": _as_str,
    "ESI_USER_AGENT": _as_str,
    "ESI_COMPATIBILITY_DATE": _as_str,
    "ESI_TIMEOUT": _as_float,
    "ESI_CACHE_TTL": _as_int,
    "PRICE_MAX_AGE_MINUTES": _as_int,
    "HOST": _as_str,
    "PORT": _as_int,
    "DEBUG": _as_bool,
}


def _load_file(path: Path) -> dict[str, Any]:
    """Читает config.py. Берёт только имена в ВЕРХНЕМ регистре, как это делает Flask."""
    try:
        namespace = runpy.run_path(str(path))
    except Exception as exc:  # noqa: BLE001 — любая ошибка в чужом файле должна быть понятной
        raise ConfigError(f"Не удалось прочитать {path}: {exc}") from exc
    return {key: value for key, value in namespace.items() if key.isupper()}


def _resolve_profile(environ: Mapping[str, str]) -> str:
    raw = environ.get("APP_ENV", DEV).strip().lower()
    if raw not in PROFILES:
        raise ConfigError(
            f"APP_ENV={raw!r} — неизвестный профиль. Допустимо: {' | '.join(PROFILES)}."
        )
    return raw


def _validate(config: dict[str, Any]) -> list[str]:
    """Проверяет собранный конфиг. Возвращает предупреждения, ошибки — исключением."""
    profile = config["APP_ENV"]
    warnings: list[str] = []

    port = config["PORT"]
    if not 1 <= port <= 65535:
        raise ConfigError(f"PORT={port} вне диапазона 1..65535.")
    if config["ESI_TIMEOUT"] <= 0:
        raise ConfigError(f"ESI_TIMEOUT={config['ESI_TIMEOUT']} должен быть больше нуля.")
    if config["PRICE_MAX_AGE_MINUTES"] <= 0:
        raise ConfigError(
            f"PRICE_MAX_AGE_MINUTES={config['PRICE_MAX_AGE_MINUTES']} должен быть больше нуля."
        )
    if config["ESI_CACHE_TTL"] < ESI_MIN_CACHE_TTL:
        raise ConfigError(
            f"ESI_CACHE_TTL={config['ESI_CACHE_TTL']} меньше {ESI_MIN_CACHE_TTL} секунд. "
            f"Столько кэширует сам ESI, кэшировать агрессивнее нельзя."
        )

    sso_pieces = (config.get("ESI_CLIENT_ID"), config.get("ESI_CLIENT_SECRET"))
    if any(sso_pieces) and not all(sso_pieces):
        raise ConfigError(
            "Для входа через EVE SSO нужны и ESI_CLIENT_ID, и ESI_CLIENT_SECRET. "
            "Задан только один — вход всё равно не заработает."
        )
    if all(sso_pieces) and not config.get("SSO_REDIRECT_URI"):
        raise ConfigError(
            "Заданы ключи EVE SSO, но нет SSO_REDIRECT_URI. Он обязан совпадать "
            "с колбэком, зарегистрированным в приложении у CCP, посимвольно."
        )

    ua = config["ESI_USER_AGENT"] or ""
    ua_is_template = any(mark in ua for mark in _UA_PLACEHOLDERS)

    # Номер версии, записанный в User-Agent руками, отстаёт молча: подняли
    # версию — а CCP по-прежнему видит прошлую. Это не ошибка запуска,
    # но сказать об этом обязаны.
    product = _UA_PRODUCT.match(ua.split(" ", 1)[0])
    if product and product.group(1) != __version__:
        warnings.append(
            f"В ESI_USER_AGENT записана версия {product.group(1)}, "
            f"а приложение имеет версию {__version__}. Поставь на её место {VERSION_MARK} — "
            "подставится текущая."
        )

    if profile == DEV:
        if not config.get("SECRET_KEY"):
            # Ключ на один запуск: сессии не переживут перезапуск, и это
            # нормально для разработки. Требовать ключ в dev — лишний обряд.
            config["SECRET_KEY"] = secrets.token_urlsafe(32)
            warnings.append(
                "SECRET_KEY не задан — сгенерирован временный. После перезапуска "
                "вход придётся повторить."
            )
        if ua_is_template:
            warnings.append(
                "ESI_USER_AGENT остался шаблонным. Для разработки сойдёт, "
                "но перед выходом в прод подставь свой контакт."
            )
        return warnings

    # Дальше — только прод: здесь молчать об этом нельзя
    if config["DEBUG"]:
        raise ConfigError(
            "DEBUG=True в профиле prod. Отладчик Flask даёт выполнение "
            "произвольного кода через браузер — в прод его пускать нельзя."
        )

    secret = config["SECRET_KEY"]
    if not secret:
        raise ConfigError(
            "В профиле prod не задан SECRET_KEY. Сгенерируй ключ и положи "
            'в переменную окружения: python -c "import secrets; '
            "print(secrets.token_urlsafe(32))\"."
        )
    if len(secret) < SECRET_KEY_MIN_LENGTH:
        raise ConfigError(
            f"SECRET_KEY короче {SECRET_KEY_MIN_LENGTH} символов. "
            f"Им подписывается сессия входа через EVE SSO."
        )

    url = config["DATABASE_URL"]
    if not url:
        raise ConfigError(
            "В профиле prod не задан DATABASE_URL. Ожидается адрес MariaDB, "
            "MySQL или Postgres в переменной окружения."
        )
    if url.startswith("sqlite"):
        raise ConfigError(
            "DATABASE_URL в профиле prod указывает на SQLite. Сборщик цен пишет "
            "из отдельного процесса одновременно с веб-приложением, а SQLite "
            "такую запись не выдерживает. Нужны MariaDB, MySQL или Postgres."
        )

    if ua_is_template:
        raise ConfigError(
            "ESI_USER_AGENT остался шаблонным. CCP требует осмысленный контакт, "
            "безликие клиенты первыми попадают под рейт-лимит."
        )

    return warnings



def _stamp_version(user_agent: object) -> str:
    """Подставляет текущую версию в User-Agent на месте метки {version}.

    Строка без метки остаётся как есть: чужой User-Agent — это чужой выбор,
    ломать его нельзя. Но тогда номер в нём живёт своей жизнью и устаревает
    молча — об этом предупреждает _validate."""
    return str(user_agent or "").replace(VERSION_MARK, __version__)

def build_config(
    environ: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Собирает и проверяет конфигурацию.

    Аргументы нужны тестам: с ними сборка не зависит ни от реального окружения,
    ни от того, лежит ли рядом чей-то config.py.

    Предупреждения складываются в ключ CONFIG_WARNINGS — вызывающий код их логирует.
    """
    environ = os.environ if environ is None else environ
    base_dir = BASE_DIR if base_dir is None else base_dir

    profile = _resolve_profile(environ)
    config = _defaults(profile, base_dir)

    warnings: list[str] = []
    config_file = base_dir / "config.py"
    if config_file.exists():
        config.update(_load_file(config_file))
    elif profile == DEV:
        warnings.append(
            "config.py не найден — взяты значения по умолчанию профиля dev. "
            "Скопируй config.example.py в config.py и подставь свой контакт."
        )

    for key, parse in _ENV_KEYS.items():
        if key in environ:
            config[key] = parse(environ[key], key)

    # Профиль задаётся только окружением: config.py не должен его переопределять
    config["APP_ENV"] = profile

    config["ESI_USER_AGENT"] = _stamp_version(config.get("ESI_USER_AGENT"))

    warnings.extend(_validate(config))
    config["CONFIG_WARNINGS"] = tuple(warnings)
    return config
