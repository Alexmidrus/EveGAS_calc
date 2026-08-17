"""Тесты HTTP-клиента ESI. Сети нет: весь транспорт подменён httpx.MockTransport.

Проверяются требования docs/ESI.md §2 и §6: заголовки, пагинация, ретраи на 5xx,
отсутствие ретраев на 420/429, кэш и то, что падение одного региона не ломает
остальные.
"""

import asyncio

import httpx
import pytest

from app.services import esi
from app.services.cache import TTLCache

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
            return await esi.fetch_orders(client, SETTINGS, 10000002, 62406, sleep=no_sleep)

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
            return await esi.fetch_orders(client, SETTINGS, 10000002, 62406, sleep=no_sleep)

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
            return await esi.fetch_orders(client, SETTINGS, 10000002, 62406, sleep=no_sleep)

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
            return await esi.fetch_orders(client, SETTINGS, 10000002, 62406, sleep=no_sleep)

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
            return await esi.fetch_orders(client, SETTINGS, 10000002, 62406, sleep=no_sleep)

    result = run(scenario())
    assert len(calls) == 1  # ни одного повтора
    assert not result.ok
    assert str(status) in result.error


def test_timeout_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    async def scenario():
        async with client_for(handler) as client:
            return await esi.fetch_orders(client, SETTINGS, 10000002, 62406, sleep=no_sleep)

    result = run(scenario())
    assert not result.ok
    assert "не ответил" in result.error


def test_broken_json_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    async def scenario():
        async with client_for(handler) as client:
            return await esi.fetch_orders(client, SETTINGS, 10000002, 62406, sleep=no_sleep)

    result = run(scenario())
    assert not result.ok
    assert "нечитаемый" in result.error


# --- Кэш ---


def test_second_call_comes_from_cache():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=[ORDER])

    cache: TTLCache[esi.CacheKey, list[dict]] = TTLCache(default_ttl=300)

    async def scenario():
        async with client_for(handler) as client:
            first = await esi.fetch_orders(
                client, SETTINGS, 10000002, 62406, cache=cache, sleep=no_sleep
            )
            second = await esi.fetch_orders(
                client, SETTINGS, 10000002, 62406, cache=cache, sleep=no_sleep
            )
            return first, second

    first, second = run(scenario())
    assert len(calls) == 1
    assert not first.from_cache
    assert second.from_cache
    assert second.orders == first.orders


def test_failed_request_is_not_cached():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    cache: TTLCache[esi.CacheKey, list[dict]] = TTLCache(default_ttl=300)

    async def scenario():
        async with client_for(handler) as client:
            await esi.fetch_orders(client, SETTINGS, 10000002, 62406, cache=cache, sleep=no_sleep)
            await esi.fetch_orders(client, SETTINGS, 10000002, 62406, cache=cache, sleep=no_sleep)

    run(scenario())
    assert len(cache) == 0
    assert len(calls) == 6  # два раза по три попытки, ошибка не закэширована


def test_cache_ttl_follows_expires_header():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[ORDER],
            headers={
                "date": "Fri, 14 Aug 2026 05:00:00 GMT",
                "expires": "Fri, 14 Aug 2026 05:04:00 GMT",
            },
        )

    now = [1000.0]
    cache: TTLCache[esi.CacheKey, list[dict]] = TTLCache(
        default_ttl=300, clock=lambda: now[0]
    )

    async def scenario():
        async with client_for(handler) as client:
            await esi.fetch_orders(client, SETTINGS, 10000002, 62406, cache=cache, sleep=no_sleep)

    run(scenario())
    now[0] += 239
    assert cache.get((10000002, 62406)) is not None  # 240 секунд из заголовка ещё не вышли
    now[0] += 2
    assert cache.get((10000002, 62406)) is None


# --- Параллельная выдача (ESI §6) ---


def test_one_failing_region_does_not_break_the_others():
    """Требование ESI §6: падение одного хаба не роняет остальные девять запросов."""
    broken_region = 10000030

    def handler(request: httpx.Request) -> httpx.Response:
        if f"/markets/{broken_region}/" in request.url.path:
            raise httpx.ConnectError("сеть отвалилась", request=request)
        return httpx.Response(200, json=[ORDER])

    pairs = [
        (10000002, 62406),
        (10000043, 62406),
        (10000032, 62406),
        (broken_region, 62406),
        (10000042, 62406),
    ]

    async def scenario():
        async with client_for(handler) as client:
            return await esi.fetch_many(pairs, SETTINGS, client=client, sleep=no_sleep)

    results = run(scenario())
    assert len(results) == 5
    assert not results[(broken_region, 62406)].ok
    assert results[(broken_region, 62406)].error
    assert all(results[pair].ok for pair in pairs if pair[0] != broken_region)


def test_unexpected_exception_becomes_a_result_not_a_crash():
    """Даже неожиданное исключение внутри gather превращается в ошибку по хабу."""

    async def boom(*args, **kwargs):
        raise RuntimeError("что-то совсем неожиданное")

    pairs = [(10000002, 62406)]

    async def scenario(monkeypatched):
        return await esi.fetch_many(pairs, SETTINGS, client=monkeypatched, sleep=no_sleep)

    original = esi.fetch_orders
    esi.fetch_orders = boom  # type: ignore[assignment]
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[ORDER])

        async def wrapper():
            async with client_for(handler) as client:
                return await scenario(client)

        results = run(wrapper())
    finally:
        esi.fetch_orders = original  # type: ignore[assignment]

    assert not results[(10000002, 62406)].ok
    assert "неожиданное" in results[(10000002, 62406)].error
