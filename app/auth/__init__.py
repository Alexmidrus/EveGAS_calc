"""Вход через EVE SSO.

Приложение остаётся анонимным по умолчанию: вход нужен ровно для того, чтобы
хранить настройки расчёта на сервере, а не в браузере. Приватные данные
персонажа не запрашиваются — scope пустой.
"""

from app.auth.sso import Character, JwksCache, SsoError, SsoSettings
from app.auth.views import bp, current_character, settings_or_none

__all__ = [
    "Character",
    "JwksCache",
    "SsoError",
    "SsoSettings",
    "bp",
    "current_character",
    "settings_or_none",
]
