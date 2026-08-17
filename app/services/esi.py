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
from datetime import UTC, datetime
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


class RateLimitedError(EsiError):
    """ESI отказал по лимиту: 429 либо исчерпанный лимит ошибок (ESI §2).

    Отдельный тип нужен сборщику цен: увидев его, он обязан прекратить весь
    цикл, а не продолжать долбить остальные 269 запросов. retry_after —
    из заголовка Retry-After, секунды; None, если заголовка не было.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return None


def error_limit_remaining(response: httpx.Response) -> int | None:
    """Сколько ошибок ещё можно допустить в текущем окне (заголовок ESI).

    Когда счётчик дойдёт до нуля, ESI начнёт отбрасывать все запросы до конца
    окна. Сборщик обязан тормозить заранее, а не упираться в стену.
    """
    raw = response.headers.get("x-esi-error-limit-remain")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def rate_limit_remaining(response: httpx.Response) -> int | None:
    """Остаток токенов рейт-лимита (заголовок X-Ratelimit-Remaining)."""
    raw = response.headers.get("x-ratelimit-remaining")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


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
    extra_headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Одна страница выдачи с ретраями на 5xx (ESI §2).

    Транспортные ошибки и таймауты не ретраятся: пятнадцати секунд уже ждали,
    ещё три повтора превратят подтяжку в минуту молчания. Пользователю честнее
    сказать сразу.

    Ответ 304 возвращается как есть — это успех условного запроса, а не ошибка.
    """
    url = settings.base_url + ORDERS_PATH.format(region_id=region_id)
    params = {"type_id": type_id, "order_type": "all", "page": page}
    headers = settings.headers() | dict(extra_headers or {})

    last_status = 0
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise EsiError(f"ESI не ответил за {settings.timeout:g} с") from exc
        except httpx.HTTPError as exc:
            raise EsiError(f"Не удалось соединиться с ESI: {exc}") from exc

        if response.status_code in RATE_LIMIT_STATUSES:
            raise RateLimitedError(
                f"ESI ограничил частоту запросов ({response.status_code}). "
                f"Подожди несколько минут и попробуй снова.",
                retry_after=_retry_after(response),
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


@dataclass(frozen=True, slots=True)
class OrdersSnapshot:
    """Ответ на условный запрос — то, что нужно сборщику цен.

    not_modified=True означает «ESI ответил 304, у тебя уже свежие данные»:
    ордеров в этом случае нет и не должно быть, прежний срез остаётся в силе.
    """

    region_id: int
    type_id: int
    orders: list[dict] | None = None
    etag: str | None = None
    expires_at: datetime | None = None
    not_modified: bool = False
    error: str | None = None
    requests_made: int = 0
    error_limit_remain: int | None = None
    rate_limit_remain: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _expires_at(response: httpx.Response) -> datetime | None:
    """Заголовок expires как наивный UTC — в том же виде, в каком время лежит в базе."""
    raw = response.headers.get("expires")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(UTC).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


async def fetch_orders_conditional(
    client: httpx.AsyncClient,
    settings: EsiSettings,
    region_id: int,
    type_id: int,
    *,
    etag: str | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> OrdersSnapshot:
    """Запрос ордеров с условным заголовком If-None-Match.

    Ответ 304 стоит 1 токен вместо 2 и не тратит трафик на неизменившийся
    стакан (ESI §2). Возвращает не только данные, но и ETag, expires и остатки
    лимитов — сборщику они нужны, чтобы вовремя остановиться.

    RateLimitedError наружу не перехватывается намеренно: увидев его, сборщик
    обязан прекратить весь цикл, а не пробовать следующие 269 запросов.

    Упрощение по страницам: условный заголовок ставится только на первую.
    С фильтром по одному type_id выдача почти всегда одностраничная, а если
    страниц несколько — остальные тянутся обычными запросами.
    """
    conditional = {"If-None-Match": etag} if etag else None

    made = 0
    try:
        first = await _request_page(
            client, settings, region_id, type_id, 1,
            sleep=sleep, extra_headers=conditional,
        )
        made += 1

        limits = {
            "error_limit_remain": error_limit_remaining(first),
            "rate_limit_remain": rate_limit_remaining(first),
        }

        if first.status_code == 304:
            return OrdersSnapshot(
                region_id, type_id, not_modified=True, etag=etag,
                expires_at=_expires_at(first), requests_made=made, **limits,
            )

        orders: list[dict] = list(first.json())
        try:
            pages = int(first.headers.get("x-pages", 1))
        except (TypeError, ValueError):
            pages = 1
        for page in range(2, pages + 1):
            extra = await _request_page(client, settings, region_id, type_id, page, sleep=sleep)
            made += 1
            orders.extend(extra.json())
    except RateLimitedError:
        raise
    except EsiError as exc:
        return OrdersSnapshot(region_id, type_id, error=str(exc), requests_made=made)
    except ValueError as exc:
        return OrdersSnapshot(
            region_id, type_id, error=f"ESI вернул нечитаемый ответ: {exc}", requests_made=made
        )

    return OrdersSnapshot(
        region_id,
        type_id,
        orders=orders,
        etag=first.headers.get("etag"),
        expires_at=_expires_at(first),
        requests_made=made,
        **limits,
    )


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
