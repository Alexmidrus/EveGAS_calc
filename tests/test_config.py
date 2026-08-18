"""Тесты профилей запуска (ROADMAP, этап 5).

Везде передаются явные environ и base_dir: сборка конфига не должна зависеть
ни от переменных окружения машины, ни от того, лежит ли рядом чей-то config.py.
"""

import pytest

from app import create_app
from app.config import DEV, PROD, ConfigError, build_config
from app.version import __version__

# Минимально достаточное окружение боевого профиля
PROD_ENV = {
    "APP_ENV": "prod",
    "SECRET_KEY": "x" * 32,
    "DATABASE_URL": "postgresql+psycopg://user:pass@localhost/evegas",
    "ESI_USER_AGENT": "EveGAS_calc/{version} (+https://example.org/app; me@example.org)",
}


class TestProfileChoice:
    """Профиль берётся из APP_ENV и только оттуда."""

    def test_dev_by_default(self, tmp_path):
        """Без APP_ENV — профиль разработки."""
        assert build_config({}, tmp_path)["APP_ENV"] == DEV

    def test_case_and_spaces_ignored(self, tmp_path):
        """« PROD » — тот же прод, а не неизвестный профиль."""
        assert build_config(dict(PROD_ENV, APP_ENV=" PROD "), tmp_path)["APP_ENV"] == PROD

    def test_unknown_profile(self, tmp_path):
        """Опечатка в APP_ENV не должна тихо откатываться в dev."""
        with pytest.raises(ConfigError, match="staging"):
            build_config({"APP_ENV": "staging"}, tmp_path)

    def test_config_file_cannot_override_profile(self, tmp_path):
        """config.py не может подменить профиль: иначе прод молча станет dev."""
        (tmp_path / "config.py").write_text('APP_ENV = "dev"\n', encoding="utf-8")
        assert build_config(PROD_ENV, tmp_path)["APP_ENV"] == PROD


class TestDevProfile:
    """Разработка: SQLite под рукой, отладчик включён, ничего не требуется извне."""

    def test_defaults(self, tmp_path):
        config = build_config({}, tmp_path)
        assert config["DEBUG"] is True
        assert config["PORT"] == 5000
        assert config["DATABASE_URL"].startswith("sqlite:///")
        assert config["DATABASE_URL"].endswith("var/evegas_dev.sqlite3")

    def test_starts_without_secrets(self, tmp_path):
        """Ни SECRET_KEY, ни DATABASE_URL в dev задавать не нужно.

        Ключ для сессии всё же необходим — иначе не заработает вход, — поэтому
        в dev он генерируется на один запуск, о чём говорится вслух."""
        config = build_config({}, tmp_path)
        assert config["SECRET_KEY"]
        assert any("SECRET_KEY" in w for w in config["CONFIG_WARNINGS"])

    def test_given_secret_key_is_kept(self, tmp_path):
        """Заданный ключ не должен подменяться сгенерированным: иначе сессии
        рассыпались бы при каждом перезапуске и на проде тоже."""
        config = build_config({"SECRET_KEY": "z" * 40}, tmp_path)
        assert config["SECRET_KEY"] == "z" * 40

    def test_template_user_agent_warns_but_starts(self, tmp_path):
        """Шаблонный UA в dev — предупреждение, а не отказ работать."""
        config = build_config({}, tmp_path)
        assert any("ESI_USER_AGENT" in w for w in config["CONFIG_WARNINGS"])

    def test_missing_config_file_warns(self, tmp_path):
        """Об отсутствии config.py надо сказать вслух, но не падать."""
        config = build_config({}, tmp_path)
        assert any("config.py" in w for w in config["CONFIG_WARNINGS"])


class TestUserAgentVersion:
    """Версия в User-Agent берётся из одного места и не пишется руками."""

    def test_mark_is_replaced(self, tmp_path):
        config = build_config(
            {"ESI_USER_AGENT": "gascalc/{version} (+https://example.org; me@example.org)"},
            tmp_path,
        )
        assert config["ESI_USER_AGENT"] == (
            f"gascalc/{__version__} (+https://example.org; me@example.org)"
        )

    def test_default_carries_current_version(self, tmp_path):
        config = build_config({}, tmp_path)
        assert f"EveGAS_calc/{__version__}" in config["ESI_USER_AGENT"]

    def test_foreign_user_agent_is_left_alone(self, tmp_path):
        """Чужую строку без метки не трогаем: это выбор владельца."""
        own = "gascalc (+https://example.org; me@example.org)"
        config = build_config({"ESI_USER_AGENT": own}, tmp_path)
        assert config["ESI_USER_AGENT"] == own
        assert not any("версия" in w for w in config["CONFIG_WARNINGS"])

    def test_handwritten_version_warns(self, tmp_path):
        """Записанный руками номер отстанет молча — об этом надо сказать."""
        config = build_config(
            {"ESI_USER_AGENT": "gascalc/0.1 (+https://example.org; me@example.org)"},
            tmp_path,
        )
        assert any("0.1" in w and __version__ in w for w in config["CONFIG_WARNINGS"])

    def test_contact_digits_are_not_a_version(self, tmp_path):
        """Цифры в контакте — не версия клиента, предупреждать не о чем."""
        config = build_config(
            {"ESI_USER_AGENT": "gascalc/{version} (+https://example.org/v2; me2@example.org)"},
            tmp_path,
        )
        assert not any("версия" in w for w in config["CONFIG_WARNINGS"])

class TestProdProfile:
    """Прод: всё, чего не хватает, обязано выясниться при старте."""

    def test_valid_environment(self, tmp_path):
        config = build_config(PROD_ENV, tmp_path)
        assert config["DEBUG"] is False
        assert config["PORT"] == 8080
        assert config["CONFIG_WARNINGS"] == ()

    def test_missing_secret_key(self, tmp_path):
        env = {k: v for k, v in PROD_ENV.items() if k != "SECRET_KEY"}
        with pytest.raises(ConfigError, match="SECRET_KEY"):
            build_config(env, tmp_path)

    def test_short_secret_key(self, tmp_path):
        with pytest.raises(ConfigError, match="SECRET_KEY"):
            build_config(dict(PROD_ENV, SECRET_KEY="короткий"), tmp_path)

    def test_missing_database_url(self, tmp_path):
        env = {k: v for k, v in PROD_ENV.items() if k != "DATABASE_URL"}
        with pytest.raises(ConfigError, match="DATABASE_URL"):
            build_config(env, tmp_path)

    def test_sqlite_rejected(self, tmp_path):
        """SQLite не выдержит запись сборщика цен одновременно с веб-процессом."""
        with pytest.raises(ConfigError, match="SQLite"):
            build_config(dict(PROD_ENV, DATABASE_URL="sqlite:///prod.sqlite3"), tmp_path)

    def test_debug_rejected(self, tmp_path):
        """Отладчик Flask в проде — выполнение кода через браузер."""
        with pytest.raises(ConfigError, match="DEBUG"):
            build_config(dict(PROD_ENV, DEBUG="true"), tmp_path)

    def test_debug_from_config_file_also_rejected(self, tmp_path):
        """Ровно тот случай, что ловим: dev-config.py уехал на прод как есть."""
        (tmp_path / "config.py").write_text("DEBUG = True\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="DEBUG"):
            build_config(PROD_ENV, tmp_path)

    def test_template_user_agent_rejected(self, tmp_path):
        env = {k: v for k, v in PROD_ENV.items() if k != "ESI_USER_AGENT"}
        with pytest.raises(ConfigError, match="ESI_USER_AGENT"):
            build_config(env, tmp_path)


class TestLayers:
    """Приоритет слоёв: умолчания → config.py → окружение."""

    def test_file_overrides_defaults(self, tmp_path):
        (tmp_path / "config.py").write_text("PORT = 5555\n", encoding="utf-8")
        assert build_config({}, tmp_path)["PORT"] == 5555

    def test_environment_overrides_file(self, tmp_path):
        (tmp_path / "config.py").write_text("PORT = 5555\n", encoding="utf-8")
        assert build_config({"PORT": "6666"}, tmp_path)["PORT"] == 6666

    def test_lowercase_names_ignored(self, tmp_path):
        """Как и у Flask, из файла берутся только имена в верхнем регистре."""
        (tmp_path / "config.py").write_text("port = 5555\n", encoding="utf-8")
        assert build_config({}, tmp_path)["PORT"] == 5000

    def test_broken_config_file(self, tmp_path):
        """Синтаксическая ошибка в чужом файле — понятный текст, а не стектрейс."""
        (tmp_path / "config.py").write_text("PORT = = 5\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="config.py"):
            build_config({}, tmp_path)


class TestValueChecks:
    """Числовые значения и границы."""

    @pytest.mark.parametrize("raw", ["0", "70000", "-1"])
    def test_port_out_of_range(self, tmp_path, raw):
        with pytest.raises(ConfigError, match="PORT"):
            build_config({"PORT": raw}, tmp_path)

    def test_port_not_a_number(self, tmp_path):
        with pytest.raises(ConfigError, match="PORT"):
            build_config({"PORT": "пять тысяч"}, tmp_path)

    def test_debug_not_a_boolean(self, tmp_path):
        with pytest.raises(ConfigError, match="DEBUG"):
            build_config({"DEBUG": "может быть"}, tmp_path)

    def test_cache_ttl_below_esi(self, tmp_path):
        """Кэшировать агрессивнее самого ESI нельзя — это правило проекта."""
        with pytest.raises(ConfigError, match="ESI_CACHE_TTL"):
            build_config({"ESI_CACHE_TTL": "60"}, tmp_path)

    def test_zero_timeout(self, tmp_path):
        with pytest.raises(ConfigError, match="ESI_TIMEOUT"):
            build_config({"ESI_TIMEOUT": "0"}, tmp_path)


class TestApplicationFactory:
    """create_app поднимается в обоих профилях."""

    def test_dev(self, tmp_path):
        from app.db import Base

        app = create_app(build_config({}, tmp_path))
        Base.metadata.create_all(app.extensions["db_engine"])
        assert app.config["APP_ENV"] == DEV
        assert app.test_client().get("/").status_code == 200

    def test_dev_survives_missing_schema(self, tmp_path):
        """База есть, а таблиц в ней нет — страница обязана открыться:
        расчёт по ручным ценам от базы не зависит."""
        app = create_app(build_config({}, tmp_path))
        response = app.test_client().get("/")
        assert response.status_code == 200
        assert "База недоступна" in response.get_data(as_text=True)

    def test_prod(self, tmp_path):
        """Приложение собирается с боевым профилем. Запрос сюда не делаем:
        DATABASE_URL указывает на Postgres, которого на машине разработки нет,
        и попытка соединения превратила бы тест в минуту ожидания."""
        app = create_app(build_config(PROD_ENV, tmp_path))
        assert app.config["APP_ENV"] == PROD
        assert app.config["DEBUG"] is False
        assert app.extensions["db_engine"].url.get_backend_name() == "postgresql"
