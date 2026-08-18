"""Вход через EVE SSO (ROADMAP, этап 9).

В сеть не ходим, но подпись токенов настоящая: тесты генерируют собственную
пару ключей RSA и подписывают ей токены. Подделать подпись должно быть
невозможно, и проверяется это на настоящей криптографии, а не на заглушке.
"""

import time
from datetime import timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import create_app
from app import routes
from app.auth import sso
from app.auth.views import CHARACTER_ID_KEY
from app.db import Base, UserAccount, UserFreightRate, UserSettings, session_scope
from app.services import user_settings

SETTINGS = sso.SsoSettings(
    client_id="abc451d512f347ad9854cd5623d4bf20",
    client_secret="секрет только в конфиге",
    redirect_uri="http://localhost:5000/sso/callback",
    user_agent="EveGAS_calc/tests",
)

CHARACTER_ID = 2112625428
CHARACTER_NAME = "Test Pilot"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def make_token(signing_key):
    """Токен, подписанный нашим ключом, с полями как у настоящего."""

    def build(**overrides):
        now = int(time.time())
        claims = {
            "iss": "login.eveonline.com",
            "aud": [SETTINGS.client_id, "EVE Online"],
            "sub": f"CHARACTER:EVE:{CHARACTER_ID}",
            "name": CHARACTER_NAME,
            "exp": now + 1200,
            "iat": now,
        }
        claims.update(overrides)
        for key, value in list(claims.items()):
            if value is None:
                del claims[key]
        return jwt.encode(claims, signing_key, algorithm="RS256")

    return build


class FakeJwks:
    """Подменяет поход за ключами CCP на наш собственный ключ."""

    def __init__(self, key):
        self._key = key
        self.calls = 0

    def signing_key(self, token):
        self.calls += 1
        return type("Key", (), {"key": self._key.public_key(), "algorithm_name": "RS256"})()


@pytest.fixture
def jwks(signing_key):
    return FakeJwks(signing_key)


class TestAuthorizeUrl:
    """Ссылка, по которой уходит пользователь."""

    def test_contains_required_params(self):
        url = sso.build_authorize_url(SETTINGS, "st4te", "ch4llenge")
        assert url.startswith(sso.AUTHORIZE_URL)
        assert "response_type=code" in url
        assert "code_challenge_method=S256" in url
        assert "state=st4te" in url
        assert "client_id=" + SETTINGS.client_id in url

    def test_scope_is_empty(self):
        """Приватные данные персонажа не запрашиваются вообще — это ограничение проекта."""
        url = sso.build_authorize_url(SETTINGS, "s", "c")
        assert "scope=&" in url or url.endswith("scope=")

    def test_redirect_uri_is_exact(self):
        """CCP сверяет колбэк посимвольно, включая слэш в конце."""
        url = sso.build_authorize_url(SETTINGS, "s", "c")
        assert "redirect_uri=http%3A%2F%2Flocalhost%3A5000%2Fsso%2Fcallback" in url


class TestPkce:
    def test_challenge_matches_verifier(self):
        import base64
        import hashlib

        verifier, challenge = sso.new_pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        assert challenge == expected

    def test_pairs_are_unique(self):
        assert sso.new_pkce_pair()[0] != sso.new_pkce_pair()[0]

    def test_states_are_unique(self):
        assert sso.new_state() != sso.new_state()


class TestTokenVerification:
    """Главное место безопасности: чужой токен принимать нельзя."""

    def test_valid_token(self, make_token, jwks):
        character = sso.verify_token(SETTINGS, make_token(), jwks=jwks)
        assert character.character_id == CHARACTER_ID
        assert character.name == CHARACTER_NAME

    def test_issuer_with_scheme_also_accepted(self, make_token, jwks):
        """В метаданных issuer со схемой, в токене — без. Валидны оба."""
        token = make_token(iss="https://login.eveonline.com")
        assert sso.verify_token(SETTINGS, token, jwks=jwks).character_id == CHARACTER_ID

    def test_foreign_issuer_rejected(self, make_token, jwks):
        with pytest.raises(sso.SsoError):
            sso.verify_token(SETTINGS, make_token(iss="https://evil.example"), jwks=jwks)

    def test_foreign_audience_rejected(self, make_token, jwks):
        """Токен, выписанный другому приложению, нам не подходит."""
        with pytest.raises(sso.SsoError):
            sso.verify_token(SETTINGS, make_token(aud=["someone-else"]), jwks=jwks)

    def test_expired_token_rejected(self, make_token, jwks):
        token = make_token(exp=int(time.time()) - 3600)
        with pytest.raises(sso.SsoError, match="просрочен"):
            sso.verify_token(SETTINGS, token, jwks=jwks)

    def test_forged_signature_rejected(self, jwks):
        """Токен, подписанный чужим ключом, обязан отвалиться."""
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": "login.eveonline.com",
                "aud": [SETTINGS.client_id],
                "sub": f"CHARACTER:EVE:{CHARACTER_ID}",
                "name": "Impostor",
                "exp": now + 600,
            },
            other,
            algorithm="RS256",
        )
        with pytest.raises(sso.SsoError):
            sso.verify_token(SETTINGS, token, jwks=jwks)

    def test_unsigned_token_rejected(self, jwks):
        """alg=none — классическая дыра. Список алгоритмов задаём мы, не токен."""
        token = jwt.encode(
            {
                "iss": "login.eveonline.com",
                "aud": [SETTINGS.client_id],
                "sub": f"CHARACTER:EVE:{CHARACTER_ID}",
                "name": "Impostor",
                "exp": int(time.time()) + 600,
            },
            key="",
            algorithm="none",
        )
        with pytest.raises(sso.SsoError):
            sso.verify_token(SETTINGS, token, jwks=jwks)

    def test_missing_name_rejected(self, make_token, jwks):
        with pytest.raises(sso.SsoError, match="имени"):
            sso.verify_token(SETTINGS, make_token(name=None), jwks=jwks)

    def test_broken_subject_rejected(self, make_token, jwks):
        with pytest.raises(sso.SsoError, match="sub"):
            sso.verify_token(SETTINGS, make_token(sub="CORPORATION:EVE:98000001"), jwks=jwks)


class TestExchangeCode:
    """Обмен кода на токен."""

    def test_sends_pkce_verifier_and_basic_auth(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content.decode()
            seen["auth"] = request.headers.get("authorization")
            seen["ua"] = request.headers.get("user-agent")
            return httpx.Response(200, json={"access_token": "t0ken", "refresh_token": "нельзя хранить"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        token = sso.exchange_code(SETTINGS, "the-code", "the-verifier", client=client)

        assert token == "t0ken"
        assert "code_verifier=the-verifier" in seen["body"]
        assert "grant_type=authorization_code" in seen["body"]
        assert seen["auth"].startswith("Basic ")
        assert seen["ua"] == SETTINGS.user_agent  # без него SSO рвёт соединение
        # Регрессия: client_id в теле вместе с Basic — это два способа
        # аутентификации клиента сразу. CCP отвечает 400 «Client credentials
        # should only be provided once», вход не работает вовсе.
        assert "client_id" not in seen["body"]

    def test_refresh_token_is_not_returned(self):
        """Refresh-токен хранить негде и незачем: наружу он не выходит."""

        def handler(request):
            return httpx.Response(200, json={"access_token": "t", "refresh_token": "r"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        assert sso.exchange_code(SETTINGS, "c", "v", client=client) == "t"

    def test_error_status_reported(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400)))
        with pytest.raises(sso.SsoError, match="400"):
            sso.exchange_code(SETTINGS, "c", "v", client=client)

    def test_error_description_is_shown(self):
        """Причина отказа обязана долететь до экрана: одного «400» мало,

        чтобы отличить просроченный код от неправильно собранного запроса.
        На этом месте уже терялся живой баг с дублем client_id."""
        body = {
            "error": "invalid_request",
            "error_description": "Client credentials should only be provided once.",
        }
        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(400, json=body))
        )
        with pytest.raises(sso.SsoError, match="provided once"):
            sso.exchange_code(SETTINGS, "c", "v", client=client)

    def test_unreadable_error_body_does_not_mask_status(self):
        """HTML вместо JSON — тоже ответ. Падать на разборе ошибки нельзя."""
        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(500, text="<html>"))
        )
        with pytest.raises(sso.SsoError, match="500"):
            sso.exchange_code(SETTINGS, "c", "v", client=client)

    def test_missing_token_reported(self):
        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        )
        with pytest.raises(sso.SsoError, match="access_token"):
            sso.exchange_code(SETTINGS, "c", "v", client=client)


# --- Уровень приложения ---


@pytest.fixture
def app(signing_key, monkeypatch):
    application = create_app(
        {
            "APP_ENV": "dev",
            "DATABASE_URL": "sqlite:///:memory:",
            "PRICE_MAX_AGE_MINUTES": 90,
            "SECRET_KEY": "ключ достаточной длины для подписи сессии",
            "ESI_CLIENT_ID": SETTINGS.client_id,
            "ESI_CLIENT_SECRET": SETTINGS.client_secret,
            "SSO_REDIRECT_URI": SETTINGS.redirect_uri,
            "ESI_USER_AGENT": SETTINGS.user_agent,
            "TESTING": True,
        }
    )
    Base.metadata.create_all(application.extensions["db_engine"])
    application.extensions["sso_jwks"] = FakeJwks(signing_key)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, monkeypatch, token, *, state="st4te"):
    """Проходит колбэк с заранее положенным в сессию state."""
    monkeypatch.setattr(sso, "exchange_code", lambda *a, **k: token)
    with client.session_transaction() as s:
        s["sso_state"] = state
        s["sso_verifier"] = "verifier"
    return client.get(f"/sso/callback?code=abc&state={state}", follow_redirects=True)


class TestLoginFlow:
    def test_login_redirects_to_ccp(self, client):
        response = client.get("/login")
        assert response.status_code == 302
        assert response.headers["Location"].startswith(sso.AUTHORIZE_URL)

    def test_login_stores_state_in_session(self, client):
        client.get("/login")
        with client.session_transaction() as s:
            assert s["sso_state"]
            assert s["sso_verifier"]

    def test_callback_logs_in(self, client, monkeypatch, make_token):
        response = login(client, monkeypatch, make_token())
        assert response.status_code == 200
        assert CHARACTER_NAME in response.get_data(as_text=True)
        with client.session_transaction() as s:
            assert s[CHARACTER_ID_KEY] == CHARACTER_ID

    def test_account_created(self, client, app, monkeypatch, make_token):
        login(client, monkeypatch, make_token())
        with session_scope(app.extensions["db_engine"]) as session:
            account = session.get(UserAccount, CHARACTER_ID)
            assert account.character_name == CHARACTER_NAME

    def test_second_login_updates_name(self, client, app, monkeypatch, make_token):
        login(client, monkeypatch, make_token())
        login(client, monkeypatch, make_token(name="Renamed Pilot"))
        with session_scope(app.extensions["db_engine"]) as session:
            assert session.get(UserAccount, CHARACTER_ID).character_name == "Renamed Pilot"

    def test_logout_clears_session(self, client, monkeypatch, make_token):
        login(client, monkeypatch, make_token())
        client.post("/logout")
        with client.session_transaction() as s:
            assert CHARACTER_ID_KEY not in s


class TestCallbackRefusals:
    """Всё, при чём входить нельзя."""

    def test_wrong_state(self, client, monkeypatch, make_token):
        monkeypatch.setattr(sso, "exchange_code", lambda *a, **k: make_token())
        with client.session_transaction() as s:
            s["sso_state"] = "правильный"
            s["sso_verifier"] = "v"
        response = client.get("/sso/callback?code=abc&state=подделка", follow_redirects=True)
        assert "state" in response.get_data(as_text=True)
        with client.session_transaction() as s:
            assert CHARACTER_ID_KEY not in s

    def test_no_state_in_session(self, client, monkeypatch, make_token):
        """Пришли на колбэк без начала входа — это чужой запрос."""
        monkeypatch.setattr(sso, "exchange_code", lambda *a, **k: make_token())
        response = client.get("/sso/callback?code=abc&state=любой", follow_redirects=True)
        with client.session_transaction() as s:
            assert CHARACTER_ID_KEY not in s
        assert response.status_code == 200

    def test_error_from_ccp(self, client):
        with client.session_transaction() as s:
            s["sso_state"] = "s"
            s["sso_verifier"] = "v"
        response = client.get("/sso/callback?error=access_denied&state=s", follow_redirects=True)
        assert "access_denied" in response.get_data(as_text=True)

    def test_bad_token_does_not_log_in(self, client, monkeypatch, make_token):
        """Час просрочки — запас на расхождение часов такое не покрывает."""
        login(client, monkeypatch, make_token(exp=int(time.time()) - 3600))
        with client.session_transaction() as s:
            assert CHARACTER_ID_KEY not in s

    def test_state_is_single_use(self, client, monkeypatch, make_token):
        """Повторный колбэк с тем же state не должен проходить."""
        login(client, monkeypatch, make_token())
        client.post("/logout")
        response = client.get("/sso/callback?code=abc&state=st4te", follow_redirects=True)
        with client.session_transaction() as s:
            assert CHARACTER_ID_KEY not in s
        assert response.status_code == 200


class TestAnonymousStillWorks:
    """Анонимный доступ — основной режим, он не должен пострадать."""

    def test_page_opens_without_login(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "Войти через EVE SSO" in response.get_data(as_text=True)

    def test_calculate_works_without_login(self, client):
        form = {
            "gas": "fullerite_c320", "n_units": "50000", "structure": "athanor",
            "gde_level": "5", "broker_fee": "1.5", "collateral_pct": "0.5",
            "jita_rate": "500", "jita_compressed_sell": "2750",
        }
        assert client.post("/calculate", data=form).status_code == 200

    def test_saving_settings_requires_login(self, client):
        assert client.post("/settings/save", data={"gas": "fullerite_c320"}).status_code == 401

    def test_no_login_link_when_sso_not_configured(self):
        """SSO не настроен — ссылки на вход просто нет, приложение работает."""
        application = create_app(
            {
                "APP_ENV": "dev",
                "DATABASE_URL": "sqlite:///:memory:",
                "PRICE_MAX_AGE_MINUTES": 90,
                "SECRET_KEY": "ключ достаточной длины для подписи сессии",
                "TESTING": True,
            }
        )
        Base.metadata.create_all(application.extensions["db_engine"])
        html = application.test_client().get("/").get_data(as_text=True)
        assert "Войти через EVE SSO" not in html


class TestStoredSettings:
    """Настройки вошедшего живут в базе, анонима — в браузере."""

    FORM = {
        "gas": "fullerite_c50",
        "n_units": "12345",
        "structure": "tatara",
        "gde_level": "4",
        "broker_fee": "2.5",
        "collateral_pct": "1.5",
        "sell_only": "on",
        "jita_rate": "500",
        "amarr_rate": "700.25",
    }

    def test_saved_and_restored(self, client, monkeypatch, make_token):
        login(client, monkeypatch, make_token())
        assert client.post("/settings/save", data=self.FORM).status_code == 200
        html = client.get("/").get_data(as_text=True)
        assert 'value="12345"' in html
        assert "Tatara" in html

    def test_every_saved_field_comes_back(self, client, monkeypatch, make_token):
        """Каждое сохранённое поле обязано отметиться в разметке.

        Три из них молча терялись: уровень навыка шаблон сравнивает с числом,
        а из базы приходила строка; брокерский процент шаблон берёт из ключа
        broker_pct, а хранится он под именем поля формы; галочка «только sell»
        сохранённое состояние не читала вовсе."""
        login(client, monkeypatch, make_token())
        client.post("/settings/save", data=self.FORM)
        html = client.get("/").get_data(as_text=True)

        assert '<option value="4" selected>' in html          # навык
        assert 'name="broker_fee" value="2.5"' in html        # брокер
        assert 'id="sell-only" checked' in html               # только sell
        assert 'value="12345"' in html                        # количество
        assert 'value="1.5"' in html                          # обеспечение

    def test_unchecked_sell_only_stays_unchecked(self, client, monkeypatch, make_token):
        """Снятая галочка обязана остаться снятой: иначе её не выключить."""
        login(client, monkeypatch, make_token())
        client.post("/settings/save", data=self.FORM)
        form = {k: v for k, v in self.FORM.items() if k != "sell_only"}
        client.post("/settings/save", data=form)
        assert "checked" not in client.get("/").get_data(as_text=True)

    def test_broken_gde_level_falls_back(self, client, app, monkeypatch, make_token):
        """В базе оказалось не число — страница обязана открыться на умолчании."""
        login(client, monkeypatch, make_token())
        assert routes._for_template({"gde_level": "пятый"}) == {}
    def test_freight_rates_restored(self, client, monkeypatch, make_token):
        login(client, monkeypatch, make_token())
        client.post("/settings/save", data=self.FORM)
        html = client.get("/").get_data(as_text=True)
        assert 'name="amarr_rate" value="700.25"' in html

    def test_round_rate_is_not_shown_in_exponent(self, client, monkeypatch, make_token):
        """Decimal.normalize() превращает 500 в «5E+2»: в поле ввода это мусор,

        а при следующем сохранении строка уехала бы обратно в базу.
        Круглые ставки обязаны возвращаться цифрами."""
        login(client, monkeypatch, make_token())
        client.post("/settings/save", data=dict(self.FORM, jita_rate="500", dodixie_rate="1000000"))
        html = client.get("/").get_data(as_text=True)
        assert 'name="jita_rate" value="500"' in html
        assert 'name="dodixie_rate" value="1000000"' in html
        assert "E+" not in html

    def test_rates_are_replaced_not_merged(self, client, app, monkeypatch, make_token):
        """Удалённая пользователем ставка не должна вернуться из базы."""
        login(client, monkeypatch, make_token())
        client.post("/settings/save", data=self.FORM)
        client.post("/settings/save", data=dict(self.FORM, amarr_rate=""))
        stored = user_settings.load(app.extensions["db_engine"], CHARACTER_ID)
        assert "amarr" not in stored.freight_rates
        assert "jita" in stored.freight_rates

    def test_empty_form_clears_stored_settings(self, client, app, monkeypatch, make_token):
        """«Сбросить настройки» у вошедшего шлёт пустую форму.

        Если бы она не затирала базу, сброс чистил бы только браузер,
        а после перезагрузки настройки вернулись бы с сервера."""
        login(client, monkeypatch, make_token())
        client.post("/settings/save", data=self.FORM)
        assert client.post("/settings/save", data={}).status_code == 200
        assert user_settings.load(app.extensions["db_engine"], CHARACTER_ID).empty

    def test_settings_are_per_character(self, app, monkeypatch, client, make_token):
        login(client, monkeypatch, make_token())
        client.post("/settings/save", data=self.FORM)
        other = user_settings.load(app.extensions["db_engine"], 90_000_999)
        assert other.empty

    def test_save_ignores_unknown_character(self, app):
        """Настройки без аккаунта не создаются: внешний ключ бы это и не позволил."""
        user_settings.save(app.extensions["db_engine"], 42, {"gas": "fullerite_c50"})
        assert user_settings.load(app.extensions["db_engine"], 42).empty

    def test_stale_gas_falls_back_to_default(self, client, app, monkeypatch, make_token):
        """Сохранённый газ исчез из справочника — страница обязана открыться."""
        login(client, monkeypatch, make_token())
        with session_scope(app.extensions["db_engine"]) as session:
            session.add(UserSettings(character_id=CHARACTER_ID, gas_key="unobtainium"))
        assert client.get("/").status_code == 200

    def test_import_offer_on_first_login(self, client, monkeypatch, make_token):
        """Первый вход: предложить забрать настройки из браузера."""
        response = login(client, monkeypatch, make_token())
        assert "Перенести настройки" in response.get_data(as_text=True)

    def test_no_import_offer_when_settings_exist(self, client, monkeypatch, make_token):
        login(client, monkeypatch, make_token())
        client.post("/settings/save", data=self.FORM)
        client.post("/logout")
        response = login(client, monkeypatch, make_token())
        assert "Перенести настройки" not in response.get_data(as_text=True)


class TestSessionCookie:
    """Куки сессии: настройки безопасности не должны разъехаться с профилем."""

    def test_dev_allows_http(self, app):
        assert app.config["SESSION_COOKIE_SECURE"] is False
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_leeway_forgives_small_clock_skew(self, make_token, jwks):
        """Часы у нас и у CCP не совпадают до секунды: 10 секунд просрочки
        не повод отказать во входе, час — повод."""
        token = make_token(exp=int(time.time()) - 10)
        assert sso.verify_token(SETTINGS, token, jwks=jwks).character_id == CHARACTER_ID

    def test_prod_cookie_is_secure_even_with_raw_config(self):
        """Ловушка: Flask держит SESSION_COOKIE_SECURE=False по умолчанию,
        поэтому setdefault ничего бы не изменил и кука ушла бы по http."""
        application = create_app(
            {
                "APP_ENV": "prod",
                "DATABASE_URL": "postgresql+psycopg://u:p@localhost/db",
                "SECRET_KEY": "x" * 32,
                "PRICE_MAX_AGE_MINUTES": 90,
            }
        )
        assert application.config["SESSION_COOKIE_SECURE"] is True

    def test_prod_requires_https(self, tmp_path):
        from app.config import build_config

        config = build_config(
            {
                "APP_ENV": "prod",
                "SECRET_KEY": "x" * 32,
                "DATABASE_URL": "postgresql+psycopg://u:p@localhost/db",
                "ESI_USER_AGENT": "EveGAS_calc/{version} (+https://example.org; me@example.org)",
            },
            tmp_path,
        )
        assert config["SESSION_COOKIE_SECURE"] is True
