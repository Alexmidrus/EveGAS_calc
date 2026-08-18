"""Вход через EVE SSO: сборка ссылки, обмен кода, проверка токена.

Только протокол, без Flask: так модуль целиком покрывается тестами без поднятия
приложения. HTTP-обработчики — в app/auth/views.py.

Что здесь важно понимать про безопасность.

**Токен проверяется по подписи, а не «на глазок».** Полученный от CCP JWT —
это утверждение «вошёл такой-то персонаж». Принять его без проверки подписи
значит позволить кому угодно назваться кем угодно. Ключи берутся из JWKS
самого CCP, ключ выбирается по ``kid`` из заголовка токена.

**Алгоритмов два.** CCP публикует RS256 и ES256 и может подписать любым из них,
поэтому список алгоритмов задаётся по выбранному ключу. Принимать ``alg``
из самого токена нельзя — это классическая дыра.

**Refresh-токен не сохраняется нигде.** Он не нужен: приватных данных персонажа
мы не читаем, а вход нужен ровно для того, чтобы узнать, чьи настройки грузить.
"""

import base64
import hashlib
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

# Значения выверены по метаданным CCP 18.08.2026:
# https://login.eveonline.com/.well-known/oauth-authorization-server
AUTHORIZE_URL = "https://login.eveonline.com/v2/oauth/authorize"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
JWKS_URL = "https://login.eveonline.com/oauth/jwks"

# В метаданных issuer записан со схемой, а в самом токене CCP исторически
# кладёт домен без неё. Принимаем оба варианта — и только их.
VALID_ISSUERS = ("login.eveonline.com", "https://login.eveonline.com")

# Токены подписываются одним из двух: RSA или EC. Конкретный алгоритм
# определяется найденным ключом, а не полем alg из токена.
ALLOWED_ALGORITHMS = ("RS256", "ES256")

# Префикс поля sub: «CHARACTER:EVE:2112625428»
SUBJECT_PREFIX = "CHARACTER:EVE:"

# Сколько держать ключи JWKS. Ключи меняются редко, но не никогда;
# при неизвестном kid кэш сбрасывается досрочно.
JWKS_TTL_SECONDS = 3600

# Запас на расхождение часов между нами и CCP
LEEWAY_SECONDS = 30

TIMEOUT = 15.0


class SsoError(Exception):
    """Вход не удался. Текст пригоден для показа пользователю."""


@dataclass(frozen=True, slots=True)
class SsoSettings:
    """Настройки приложения EVE SSO. Секрет живёт только в конфиге."""

    client_id: str
    client_secret: str
    redirect_uri: str
    user_agent: str

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "SsoSettings":
        client_id = str(config.get("ESI_CLIENT_ID") or "").strip()
        client_secret = str(config.get("ESI_CLIENT_SECRET") or "").strip()
        redirect_uri = str(config.get("SSO_REDIRECT_URI") or "").strip()
        if not client_id or not client_secret:
            raise SsoError(
                "Вход через EVE SSO не настроен: нет ESI_CLIENT_ID или ESI_CLIENT_SECRET."
            )
        if not redirect_uri:
            raise SsoError("Вход через EVE SSO не настроен: нет SSO_REDIRECT_URI.")
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            user_agent=str(config.get("ESI_USER_AGENT") or "EveGAS_calc"),
        )

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)


@dataclass(frozen=True, slots=True)
class Character:
    """Кто вошёл. Больше про персонажа мы ничего не знаем и знать не хотим."""

    character_id: int
    name: str


def new_state() -> str:
    """Случайный state против CSRF. Живёт в подписанной сессии, не в базе."""
    return secrets.token_urlsafe(24)


def new_pkce_pair() -> tuple[str, str]:
    """(verifier, challenge) для PKCE S256 — единственный метод, который принимает CCP."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(settings: SsoSettings, state: str, challenge: str) -> str:
    """Ссылка, на которую отправляем пользователя.

    scope пустой намеренно: приложению нужно только имя вошедшего. Меньше
    запросить невозможно, и это прямо записано в ограничениях проекта.
    """
    params = {
        "response_type": "code",
        "redirect_uri": settings.redirect_uri,
        "client_id": settings.client_id,
        "scope": "",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(
    settings: SsoSettings,
    code: str,
    verifier: str,
    *,
    client: httpx.Client | None = None,
) -> str:
    """Меняет код на access-токен. Возвращает только access_token.

    Refresh-токен из ответа сознательно игнорируется: хранить его негде
    и незачем, а любой сохранённый секрет — это то, что можно потерять.
    """
    own = client is None
    http = client or httpx.Client(timeout=TIMEOUT)
    try:
        response = http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
            },
            # client_id идёт только сюда. Продублировать его в теле запроса
            # значит предъявить два способа аутентификации клиента сразу,
            # что запрещено (RFC 6749 §2.3), и CCP отвечает 400
            # «Client credentials should only be provided once».
            auth=(settings.client_id, settings.client_secret),
            headers={
                # Без осмысленного User-Agent login.eveonline.com рвёт соединение
                "User-Agent": settings.user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
                "Host": "login.eveonline.com",
            },
        )
    except httpx.HTTPError as exc:
        raise SsoError(f"Не удалось связаться с EVE SSO: {exc}") from exc
    finally:
        if own:
            http.close()

    if response.status_code != 200:
        raise SsoError(
            f"EVE SSO отказал при обмене кода ({response.status_code}): {_error_detail(response)}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise SsoError("EVE SSO вернул нечитаемый ответ на обмен кода.") from exc

    token = payload.get("access_token")
    if not token:
        raise SsoError("В ответе EVE SSO нет access_token.")
    return str(token)


def _error_detail(response: httpx.Response) -> str:
    """Текст ошибки OAuth2 из ответа.

    Один код состояния ничего не объясняет: и просроченный код, и кривой
    запрос — оба 400. CCP пишет причину в error_description, и молчать о ней
    значит оставить и себя, и пользователя без единой зацепки."""
    try:
        payload = response.json()
    except ValueError:
        return "ответ без разбираемого тела"
    if not isinstance(payload, dict):
        return "ответ неожиданного вида"
    detail = str(payload.get("error_description") or "").strip()
    code = str(payload.get("error") or "").strip()
    return " — ".join(part for part in (code, detail) if part) or "причина не указана"

class JwksCache:
    """Ключи подписи CCP с ограниченным сроком жизни.

    Тянуть JWKS на каждый вход — лишний поход в сеть на ровном месте.
    Держать вечно — не пережить смену ключей. Отсюда TTL и сброс кэша,
    когда в токене пришёл незнакомый kid.
    """

    def __init__(self, url: str = JWKS_URL, ttl: float = JWKS_TTL_SECONDS) -> None:
        self._url = url
        self._ttl = ttl
        self._client: PyJWKClient | None = None
        self._fetched_at = 0.0

    def _fresh_client(self) -> PyJWKClient:
        return PyJWKClient(self._url, cache_keys=False, timeout=int(TIMEOUT))

    def signing_key(self, token: str) -> Any:
        """Ключ, которым подписан этот токен. Незнакомый kid — повод перечитать JWKS."""
        now = time.monotonic()
        if self._client is None or now - self._fetched_at > self._ttl:
            self._client = self._fresh_client()
            self._fetched_at = now
        try:
            return self._client.get_signing_key_from_jwt(token)
        except Exception:
            # Ключи могли смениться раньше, чем истёк наш TTL
            self._client = self._fresh_client()
            self._fetched_at = now
            return self._client.get_signing_key_from_jwt(token)


def verify_token(
    settings: SsoSettings, token: str, *, jwks: JwksCache | None = None
) -> Character:
    """Проверяет подпись и содержимое токена, возвращает вошедшего персонажа.

    Проверяется всё: подпись, издатель, получатель и срок. Пропустить любую
    из проверок значит принять чужой или просроченный токен.
    """
    cache = jwks or JwksCache()
    try:
        key = cache.signing_key(token)
    except Exception as exc:  # noqa: BLE001 — сеть, разбор JWKS, неизвестный kid
        raise SsoError(f"Не удалось получить ключи подписи EVE SSO: {exc}") from exc

    algorithm = getattr(key, "algorithm_name", None)
    algorithms = [algorithm] if algorithm in ALLOWED_ALGORITHMS else list(ALLOWED_ALGORITHMS)

    try:
        claims = jwt.decode(
            token,
            key.key,
            algorithms=algorithms,
            audience=settings.client_id,
            issuer=list(VALID_ISSUERS),
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise SsoError("Токен EVE SSO просрочен. Попробуйте войти ещё раз.") from exc
    except jwt.InvalidTokenError as exc:
        raise SsoError(f"Токен EVE SSO не прошёл проверку: {exc}") from exc

    return _character_from_claims(claims)


def _character_from_claims(claims: Mapping[str, Any]) -> Character:
    """Достаёт персонажа из разобранных полей токена."""
    subject = str(claims.get("sub", ""))
    if not subject.startswith(SUBJECT_PREFIX):
        raise SsoError(f"Неожиданный формат sub в токене: {subject!r}")
    try:
        character_id = int(subject[len(SUBJECT_PREFIX) :])
    except ValueError as exc:
        raise SsoError(f"В sub не число: {subject!r}") from exc

    name = str(claims.get("name") or "").strip()
    if not name:
        raise SsoError("В токене нет имени персонажа.")
    return Character(character_id=character_id, name=name)
