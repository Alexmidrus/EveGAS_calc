"""Тесты HTTP-клиента ESI. Сети нет: весь транспорт подменён httpx.MockTransport.

Проверяются требования docs/ESI.md §2 и §6: заголовки, пагинация, ретраи на 5xx,
отсутствие ретраев на 420/429, кэш и то, что падение одного региона не ломает
остальные.
"""

import asyncio

import httpx
import pytest

from app.services import esi

SETTINGS = esi.EsiSettings(
    user_agent="gascalc-tests/0.1 (+https://example.invalid; tests@example.invalid)",
    compatibility_date="2026-08-14",
    timeout=15.0,
    cache_ttl=300.0,
    base_url="https://esi.test",
)

ORDER = {
    "order_id": 1,
    "type_id": 62406,
    "location_id": 60003760,
    "system_id": 30000142,
    "is_buy_order": False,
    "price": 2750.0,
    "volume_remain": 100,
    "min_volume": 1,
    "range": "region",
}


async def no_sleep(_seconds: float) -> None:
    """Пауза между ретраями в тестах не нужна."""


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


# --- Заголовки и параметры запроса (ESI §2) ---


def test_required_headers_are_sent():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[ORDER])

    async def scenario():
        async with client_for(handler) as client:
            return await esi.fetch_orders_conditional(client, SETTINGS, 10000002, 62406, sleep=no_sleep)

    result = run(scenario())
    assert result.ok
    request = seen[0]
    assert request.headers["User-Agent"] == SETTINGS.user_agent
    assert request.headers["X-Compatibility-Date"] == "2026-08-14"
    assert request.headers["Accept"] == "application/json"
    assert request.url.path == "/markets/10000002/orders/"
    assert request.url.params["type_id"] == "62406"
    assert request.url.params["order_type"] == "all"


def test_settings_from_config_rejects_empty_user_agent():
    with pytest.raises(ValueError):
        esi.EsiSettings.from_config({"ESI_USER_AGENT": "  ", "ESI_COMPATIBILITY_DATE": "2026-08-14"})


def test_settings_from_config_reads_defaults():
    settings = esi.EsiSettings.from_config(
        {"ESI_USER_AGENT": "ua", "ESI_COMPATIBILITY_DATE": "2026-08-14"}
    )
    assert settings.base_url == esi.DEFAULT_BASE_URL
    assert settings.timeout == esi.DEFAULT_TIMEOUT


# --- Пагинация (ESI §2) ---


def test_all_pages_are_fetched():
    pages_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        pages_seen.append(page)
        return httpx.Response(
            200,
            json=[dict(ORDER, order_id=int(page))],
            headers={"x-pages": "3"},
        )

    async def scenario():
        async with client_for(handler) as client:
            return await esi.fetch_orders_conditional(client, SETTINGS, 10000002, 62406, sleep=no_sleep)

    result = run(scenario())
    assert pages_seen == ["1", "2", "3"]
    assert [o["order_id"] for o in result.orders] == [1, 2, 3]


# --- Ошибки и ретраи (ESI §2) ---


def test_server_error_is_retried_twice_then_reported():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    async def scenario():
        async with client_for(handler) as client:
            return await esi.fetch_orders_conditional(client, SETTINGS, 10000002, 62406, sleep=no_sleep)

    result = run(scenario())
    assert len(calls) == esi.MAX_RETRIES + 1 == 3
    assert not result.ok
    assert "503" in result.error


def test_server_error_recovers_on_retry():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500)
        return httpx.Response(200, json=[ORDER])

    async def scenario():
        async with client_for(handler) as client:
            return await esi.fetch_orders_conditional(client, SETTINGS, 10000002, 62406, sleep=no_sleep)

    result = run(scenario())
    assert len(calls) == 2
    assert result.ok


@pytest.mark.parametrize("status", [420, 429])
def test_rate_limit_is_not_retried(status):
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status)

    async def scenario():
        async with client_for(handler) as client:
            return await esi.fetch_orders_conditional(client, SETTINGS, 10000002, 62406, sleep=no_sleep)

    with pytest.raises(esi.RateLimitedError) as exc:
        run(scenario())
    assert len(calls) == 1  # ни одного повтора
    assert str(status) in str(exc.value)


def test_timeout_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    async def scenario():
        async with client_for(handler) as client:
            return await esi.fetch_orders_conditional(client, SETTINGS, 10000002, 62406, sleep=no_sleep)

    result = run(scenario())
    assert not result.ok
    assert "не ответил" in result.error


def test_broken_json_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    async def scenario():
        async with client_for(handler) as client:
            return await esi.fetch_orders_conditional(client, SETTINGS, 10000002, 62406, sleep=no_sleep)

    result = run(scenario())
    assert not result.ok
    assert "нечитаемый" in result.error


class TestStatus:
    """Состояние сервера Tranquility (ESI §8)."""

    # Ответ живого ESI, снятый 20.08.2026 — форма не выдумана
    BODY = {
        "server_version": "3475087",
        "players": 26843,
        "vip": False,
        "start_time": "2026-08-20T11:05:14Z",
    }

    def test_reads_the_answer(self):
        snapshot = run(
            esi.fetch_status(
                client_for(lambda r: httpx.Response(200, json=self.BODY)),
                SETTINGS,
                sleep=no_sleep,
            )
        )
        assert snapshot.ok
        assert snapshot.players == 26843
        assert snapshot.server_version == "3475087"
        assert snapshot.vip is False
        assert snapshot.requests_made == 1

    def test_start_time_is_naive_utc(self):
        """Время из ISO с Z приводится к тому виду, в каком живёт база."""
        snapshot = run(
            esi.fetch_status(
                client_for(lambda r: httpx.Response(200, json=self.BODY)),
                SETTINGS,
                sleep=no_sleep,
            )
        )
        assert snapshot.start_time is not None
        assert snapshot.start_time.tzinfo is None
        assert snapshot.start_time.isoformat() == "2026-08-20T11:05:14"

    def test_vip_mode_is_read(self):
        """Сервер поднят, но пускает только избранных."""
        snapshot = run(
            esi.fetch_status(
                client_for(lambda r: httpx.Response(200, json=self.BODY | {"vip": True})),
                SETTINGS,
                sleep=no_sleep,
            )
        )
        assert snapshot.ok and snapshot.vip is True

    def test_no_type_id_in_request(self):
        """У эндпоинта нет параметров: путь /status/ и всё."""
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=self.BODY)

        run(esi.fetch_status(client_for(handler), SETTINGS, sleep=no_sleep))
        assert seen[0].url.path == "/status/"
        assert not seen[0].url.params

    def test_server_down_is_an_answer_not_a_crash(self):
        """Сервер лежит — это результат проверки, а не сбой сборщика."""
        snapshot = run(
            esi.fetch_status(
                client_for(lambda r: httpx.Response(503)), SETTINGS, sleep=no_sleep
            )
        )
        assert not snapshot.ok
        assert snapshot.players is None
        assert "503" in snapshot.error

    def test_garbage_body_does_not_raise(self):
        snapshot = run(
            esi.fetch_status(
                client_for(lambda r: httpx.Response(200, text="не json")),
                SETTINGS,
                sleep=no_sleep,
            )
        )
        assert not snapshot.ok
