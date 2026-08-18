"""Эндпоинты входа через EVE SSO.

Анонимный доступ остаётся основным режимом: без входа работает всё, кроме
хранения настроек на сервере. Если SSO не настроен, ссылки на вход просто нет,
а обработчики честно отвечают, что вход не настроен, — приложение при этом
продолжает считать.
"""

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    request,
    session,
    url_for,
)

from app.auth import sso
from app.services import user_settings

bp = Blueprint("auth", __name__)

# Ключи в сессии. Всё, что здесь лежит, подписано SECRET_KEY.
STATE_KEY = "sso_state"
VERIFIER_KEY = "sso_verifier"
CHARACTER_ID_KEY = "character_id"
CHARACTER_NAME_KEY = "character_name"
# Показать предложение перенести настройки из браузера — один раз после входа
OFFER_IMPORT_KEY = "offer_settings_import"


def settings_or_none() -> sso.SsoSettings | None:
    """Настройки SSO или None, если вход не сконфигурирован."""
    try:
        return sso.SsoSettings.from_config(current_app.config)
    except sso.SsoError:
        return None


def current_character() -> tuple[int, str] | None:
    """Кто сейчас вошёл. None — аноним, и это нормальный режим работы."""
    character_id = session.get(CHARACTER_ID_KEY)
    if character_id is None:
        return None
    return int(character_id), str(session.get(CHARACTER_NAME_KEY, ""))


def jwks_cache() -> sso.JwksCache:
    """Кэш ключей подписи, общий на процесс: заводится в create_app."""
    return current_app.extensions["sso_jwks"]


@bp.get("/login")
def login():
    """Отправляет пользователя на страницу входа CCP."""
    settings = settings_or_none()
    if settings is None:
        flash("Вход через EVE SSO не настроен на этом сервере.", "error")
        return redirect(url_for("main.index"))

    state = sso.new_state()
    verifier, challenge = sso.new_pkce_pair()
    session[STATE_KEY] = state
    session[VERIFIER_KEY] = verifier
    return redirect(sso.build_authorize_url(settings, state, challenge))


@bp.get("/sso/callback")
def callback():
    """Возврат от CCP: сверяем state, меняем код на токен, проверяем подпись."""
    settings = settings_or_none()
    if settings is None:
        flash("Вход через EVE SSO не настроен на этом сервере.", "error")
        return redirect(url_for("main.index"))

    expected_state = session.pop(STATE_KEY, None)
    verifier = session.pop(VERIFIER_KEY, None)

    if error := request.args.get("error"):
        flash(f"EVE SSO отказал во входе: {error}.", "error")
        return redirect(url_for("main.index"))

    state = request.args.get("state")
    code = request.args.get("code")
    if not expected_state or not state or state != expected_state:
        # Либо чужой запрос, либо сессия потерялась. В обоих случаях
        # продолжать нельзя: state — единственная защита от подмены входа.
        flash("Вход не подтверждён: проверка state не прошла. Попробуйте заново.", "error")
        return redirect(url_for("main.index"))
    if not code or not verifier:
        flash("EVE SSO не вернул код авторизации.", "error")
        return redirect(url_for("main.index"))

    try:
        token = sso.exchange_code(settings, code, verifier)
        character = sso.verify_token(settings, token, jwks=jwks_cache())
    except sso.SsoError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.index"))

    engine = current_app.extensions["db_engine"]
    user_settings.ensure_account(engine, character.character_id, character.name)

    session[CHARACTER_ID_KEY] = character.character_id
    session[CHARACTER_NAME_KEY] = character.name
    # Если сохранённых настроек ещё нет, предложим забрать их из браузера
    session[OFFER_IMPORT_KEY] = user_settings.load(engine, character.character_id).empty

    flash(f"Вы вошли как {character.name}.", "ok")
    return redirect(url_for("main.index"))


@bp.post("/logout")
def logout():
    """Выход. Токен нигде не хранится, поэтому достаточно очистить сессию."""
    session.clear()
    flash("Вы вышли.", "ok")
    return redirect(url_for("main.index"))


@bp.post("/settings/save")
def save_settings():
    """Сохраняет настройки вошедшего. Аноним сюда не попадает — у него localStorage."""
    who = current_character()
    if who is None:
        return {"saved": False, "reason": "не выполнен вход"}, 401
    character_id, _name = who
    user_settings.save(current_app.extensions["db_engine"], character_id, request.form)
    session[OFFER_IMPORT_KEY] = False
    return {"saved": True}, 200
