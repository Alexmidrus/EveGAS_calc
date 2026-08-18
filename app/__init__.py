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

    # Защита куки сессии задаётся здесь, а не в конфиге: она не должна зависеть
    # от того, каким путём конфигурация приехала. Скрипт до куки не дотянется,
    # на чужой сайт она не уедет, а по http уйдёт только в dev-профиле.
    #
    # Именно присваивание, а не setdefault: Flask уже держит все три ключа
    # со своими значениями, и setdefault оказался бы пустышкой — в проде
    # это оставило бы куку сессии ходить по открытому http.
    provided = dict(config or {})
    if "SESSION_COOKIE_HTTPONLY" not in provided:
        app.config["SESSION_COOKIE_HTTPONLY"] = True
    if "SESSION_COOKIE_SAMESITE" not in provided:
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if "SESSION_COOKIE_SECURE" not in provided:
        app.config["SESSION_COOKIE_SECURE"] = app.config.get("APP_ENV") != "dev"
    for warning in app.config.get("CONFIG_WARNINGS", ()):
        app.logger.warning(warning)

    # Числа в шаблонах — только через эти фильтры (правило проекта)
    from app import formatting

    app.add_template_filter(formatting.fmt_number, "num")
    app.add_template_filter(formatting.fmt_compact, "compact")
    app.add_template_filter(formatting.fmt_percent, "pct")

    from app import db

    db.init_app(app)

    # Ключи подписи EVE SSO: один кэш на процесс, а не поход в сеть на каждый вход
    from app.auth.sso import JwksCache

    app.extensions["sso_jwks"] = JwksCache()

    from app.auth.views import bp as auth_bp
    from app.routes import bp

    app.register_blueprint(bp)
    app.register_blueprint(auth_bp)

    return app
