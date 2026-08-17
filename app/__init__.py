"""Фабрика приложения."""

from collections.abc import Mapping
from typing import Any

from flask import Flask

from app.config import BASE_DIR, build_config

__all__ = ["BASE_DIR", "create_app"]


def create_app(config: Mapping[str, Any] | None = None) -> Flask:
    """Собирает и возвращает приложение: конфиг, фильтры, маршруты.

    config — готовая конфигурация вместо сборки из окружения. Нужна тестам,
    чтобы не зависеть от переменных окружения и от чужого config.py.
    """
    app = Flask(__name__)
    app.config.update(build_config() if config is None else config)
    for warning in app.config.get("CONFIG_WARNINGS", ()):
        app.logger.warning(warning)

    # Числа в шаблонах — только через эти фильтры (правило проекта)
    from app import formatting

    app.add_template_filter(formatting.fmt_number, "num")
    app.add_template_filter(formatting.fmt_compact, "compact")
    app.add_template_filter(formatting.fmt_percent, "pct")

    # Кэш ответов ESI — обычный dict в памяти процесса, один на приложение.
    # Умирает вместе с процессом; постоянное хранилище цен появится на этапе 7.
    from app.services.cache import TTLCache
    from app.services.esi import DEFAULT_CACHE_TTL

    app.extensions["gascalc_esi_cache"] = TTLCache(
        default_ttl=float(app.config.get("ESI_CACHE_TTL", DEFAULT_CACHE_TTL))
    )

    from app.routes import bp

    app.register_blueprint(bp)

    return app
