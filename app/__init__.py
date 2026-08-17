"""Фабрика приложения."""

from pathlib import Path

from flask import Flask

# Корень репозитория — на уровень выше пакета app/
BASE_DIR = Path(__file__).resolve().parent.parent


def create_app() -> Flask:
    """Собирает и возвращает приложение: конфиг, фильтры, маршруты."""
    app = Flask(__name__)
    _load_config(app)

    # Числа в шаблонах — только через эти фильтры (правило проекта)
    from app import formatting

    app.add_template_filter(formatting.fmt_number, "num")
    app.add_template_filter(formatting.fmt_compact, "compact")
    app.add_template_filter(formatting.fmt_percent, "pct")

    # Кэш ответов ESI — обычный dict в памяти процесса, один на приложение.
    # Базы данных в проекте нет, кэш умирает вместе с процессом (CLAUDE.md).
    from app.services.cache import TTLCache
    from app.services.esi import DEFAULT_CACHE_TTL

    app.extensions["gascalc_esi_cache"] = TTLCache(
        default_ttl=float(app.config.get("ESI_CACHE_TTL", DEFAULT_CACHE_TTL))
    )

    from app.routes import bp

    app.register_blueprint(bp)

    return app


def _load_config(app: Flask) -> None:
    """Читает config.py, а при его отсутствии — config.example.py.

    Работать без своего конфига можно, но об этом надо сказать вслух:
    в шаблоне лежит безликий ESI_USER_AGENT, который на этапе 3 попадёт
    под рейт-лимит API.
    """
    config = BASE_DIR / "config.py"
    example = BASE_DIR / "config.example.py"

    if config.exists():
        app.config.from_pyfile(str(config))
        return

    if not example.exists():
        raise RuntimeError(
            f"Не найден ни {config.name}, ни {example.name} в {BASE_DIR}. "
            f"Приложению неоткуда взять настройки."
        )

    app.config.from_pyfile(str(example))
    app.logger.warning(
        "config.py не найден — взяты значения из config.example.py. "
        "Скопируй шаблон в config.py и подставь свой контакт в ESI_USER_AGENT."
    )
