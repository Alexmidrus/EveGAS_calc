"""HTTP-клиент ESI: заголовки, ретраи, пагинация, параллельные запросы.

Только транспорт. Разбор стакана — в orderbook.py, он про HTTP ничего не знает.
Все правила — docs/ESI.md §2.

Единственное место в проекте, ради которого подключён async: одна подтяжка —
до десяти независимых запросов (2 type_id × 5 регионов). Последовательно это
3–5 секунд, через gather — меньше секунды.

Падение одного региона не должно ронять остальные: gather вызывается с
return_exceptions=True, а результат каждого запроса приходит отдельным
объектом OrdersResult с полем error. Молчаливых нулей нет — хаб помечается
как «данные не получены».
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import httpx

from app.services.cache import TTLCache

# Значения по умолчанию. Переопределяются через config.py — см. from_config.
DEFAULT_BASE_URL = "https://esi.evetech.net"
DEFAULT_TIMEOUT = 15.0
DEFAULT_CACHE_TTL = 300.0

ORDERS_PATH = "/markets/{region_id}/orders/"

# Ретраи: только на 5xx, до двух повторов с экспоненциальной паузой (ESI §2).
MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 0.5  # секунды: 0.5, затем 1.0

# 420 «error limited» и 429 «too many requests» не ретраятся никогда:
# повтор только усугубит, пользователю нужно сказать правду.
RATE_LIMIT_STATUSES = frozenset({420, 429})

CacheKey = tuple[int, int]  # (region_id, type_id)


@dataclass(frozen=True, slots=True)
class EsiSettings:
    """Настройки клиента. Живут в config.py, чтобы развернувший подставил свой контакт."""

    user_agent: str
    compatibility_date: str
    timeout: float = DEFAULT_TIMEOUT
    cache_ttl: float = DEFAULT_CACHE_TTL
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> "EsiSettings":
        """Собирает настройки из app.config.

        Оба заголовка обязательны, поэтому пустой User-Agent или дата — ошибка,
        а не молчаливая подстановка: безликие клиенты попадают под рейт-лимит.
        """
        user_agent = str(config.get("ESI_USER_AGENT", "")).strip()
        compatibility_date = str(config.get("ESI_COMPATIBILITY_DATE", "")).strip()
        if not user_agent:
            raise ValueError("ESI_USER_AGENT не задан в конфиге — ESI требует осмысленный User-Agent")
        if not compatibility_date:
            raise ValueError("ESI_COMPATIBILITY_DATE не задан в конфиге")
        return cls(
            user_agent=user_agent,
            compatibility_date=compatibility_date,
            timeout=float(config.get("ESI_TIMEOUT", DEFAULT_TIMEOUT)),
            cache_ttl=float(config.get("ESI_CACHE_TTL", DEFAULT_CACHE_TTL)),
            base_url=str(config.get("ESI_BASE_URL", DEFAULT_BASE_URL)).rstrip("/"),
        )

    def headers(self) -> dict[str, str]:
        """Обязательные заголовки ESI (§2). Оба — не опциональные."""
        return {
            "User-Agent": self.user_agent,
            "X-Compatibility-Date": self.compatibility_date,
            "Accept": "application/json",
        }


@dataclass(frozen=True, slots=True)
class OrdersResult:
    """Ответ по одной паре (регион, тип). Либо ордера, либо текст ошибки."""

    region_id: int
    type_id: int
    orders: list[dict] | None = None
    error: str | None = None  # готовый текст для интерфейса, на русском
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.orders is not None


class EsiError(Exception):
    """Ошибка запроса с готовым текстом для пользователя."""


def _cache_ttl_from_headers(response: httpx.Response, default: float) -> float:
    """TTL из заголовка expires; при его отсутствии или мусоре — значение по умолчанию.

    Свой кэш держим не агрессивнее серверного: сколько ESI просит хранить,
    столько и храним.
    """
    raw = response.headers.get("expires")
    if not raw:
        return default
    try:
        expires = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return default
    date_header = response.headers.get("date")
    try:
        now = parsedate_to_datetime(date_header) if date_header else None
    except (TypeError, ValueError):
        now = None
    if now is None:
        return default
    return max((expires - now).total_seconds(), 0.0)


async def _request_page(
    client: httpx.AsyncClient,
    settings: EsiSettings,
    region_id: int,
    type_id: int,
    page: int,
    *,
    sleep: Callable[[float], Awaitable[None]],
) -> httpx.Response:
    """Одна страница выдачи с ретраями на 5xx (ESI §2).

    Транспортные ошибки и таймауты не ретраятся: пятнадцати секунд уже ждали,
    ещё три повтора превратят подтяжку в минуту молчания. Пользователю честнее
    сказать сразу.
    """
    url = settings.base_url + ORDERS_PATH.format(region_id=region_id)
    params = {"type_id": type_id, "order_type": "all", "page": page}

    last_status = 0
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.get(url, params=params, headers=settings.headers())
        except httpx.TimeoutException as exc:
            raise EsiError(f"ESI не ответил за {settings.timeout:g} с") from exc
        except httpx.HTTPError as exc:
            raise EsiError(f"Не удалось соединиться с ESI: {exc}") from exc

        if response.status_code in RATE_LIMIT_STATUSES:
            raise EsiError(
                f"ESI ограничил частоту запросов ({response.status_code}). "
                f"Подожди несколько минут и попробуй снова."
            )
        if response.status_code >= 500:
            last_status = response.status_code
            if attempt < MAX_RETRIES:
                await sleep(RETRY_BACKOFF_BASE * 2**attempt)
                continue
            raise EsiError(
                f"ESI недоступен ({last_status}), "
                f"{MAX_RETRIES + 1} попытки подряд не прошли"
            )
        if response.status_code >= 400:
            raise EsiError(f"ESI ответил {response.status_code} на запрос ордеров")
        return response

    raise EsiError(f"ESI недоступен ({last_status})")  # недостижимо, но без «а вдруг»


async def fetch_orders(
    client: httpx.AsyncClient,
    settings: EsiSettings,
    region_id: int,
    type_id: int,
    *,
    cache: TTLCache[CacheKey, list[dict]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> OrdersResult:
    """Все ордера по паре (регион, тип), со всеми страницами и кэшем.

    Пагинация — по заголовку X-Pages. С фильтром по одному type_id страница
    почти всегда одна, но молча брать только первую нельзя.
    """
    key: CacheKey = (region_id, type_id)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return OrdersResult(region_id, type_id, orders=cached, from_cache=True)

    try:
        first = await _request_page(client, settings, region_id, type_id, 1, sleep=sleep)
        orders: list[dict] = list(first.json())

        try:
            pages = int(first.headers.get("x-pages", 1))
        except (TypeError, ValueError):
            pages = 1
        for page in range(2, pages + 1):
            extra = await _request_page(client, settings, region_id, type_id, page, sleep=sleep)
            orders.extend(extra.json())
    except EsiError as exc:
        return OrdersResult(region_id, type_id, error=str(exc))
    except ValueError as exc:  # тело не разобралось как JSON
        return OrdersResult(region_id, type_id, error=f"ESI вернул нечитаемый ответ: {exc}")

    if cache is not None:
        cache.set(key, orders, _cache_ttl_from_headers(first, settings.cache_ttl))
    return OrdersResult(region_id, type_id, orders=orders)


async def fetch_many(
    pairs: Sequence[CacheKey],
    settings: EsiSettings,
    *,
    cache: TTLCache[CacheKey, list[dict]] | None = None,
    client: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[CacheKey, OrdersResult]:
    """Параллельно тянет все пары (регион, тип).

    Падение одного запроса не должно ронять остальные девять: gather вызывается
    с return_exceptions=True, а неожиданное исключение превращается в OrdersResult
    с ошибкой — интерфейс покажет по этому хабу «данные не получены».
    """
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=settings.timeout)
    try:
        results = await asyncio.gather(
            *(
                fetch_orders(http, settings, region_id, type_id, cache=cache, sleep=sleep)
                for region_id, type_id in pairs
            ),
            return_exceptions=True,
        )
    finally:
        if own_client:
            await http.aclose()

    out: dict[CacheKey, OrdersResult] = {}
    for pair, result in zip(pairs, results, strict=True):
        if isinstance(result, OrdersResult):
            out[pair] = result
        else:
            out[pair] = OrdersResult(
                region_id=pair[0],
                type_id=pair[1],
                error=f"Внутренняя ошибка запроса: {result}",
            )
    return out
