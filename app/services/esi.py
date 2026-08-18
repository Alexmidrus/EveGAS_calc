"""HTTP-клиент ESI: заголовки, ретраи, пагинация, условные запросы.

Только транспорт. Разбор стакана — в orderbook.py, он про HTTP ничего не знает.
Все правила — docs/ESI.md §2.

**Единственный, кто сюда ходит, — сборщик цен по расписанию.** Веб-часть
приложения этот модуль не импортирует: пользователь не может вызвать обращение
к ESI ни одним действием. Ради параллельного обхода 270 целей здесь и нужен
async; результат каждого запроса приходит отдельным OrdersSnapshot с полем
error, поэтому падение одного хаба не отменяет остальные.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

# Значения по умолчанию. Переопределяются через config.py — см. from_config.
DEFAULT_BASE_URL = "https://esi.evetech.net"
DEFAULT_TIMEOUT = 15.0
DEFAULT_CACHE_TTL = 300.0

ORDERS_PATH = "/markets/{region_id}/orders/"
HISTORY_PATH = "/markets/{region_id}/history/"

# Ретраи: только на 5xx, до двух повторов с экспоненциальной паузой (ESI §2).
MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 0.5  # секунды: 0.5, затем 1.0

# 420 «error limited» и 429 «too many requests» не ретраятся никогда:
# повтор только усугубит, пользователю нужно сказать правду.
RATE_LIMIT_STATUSES = frozenset({420, 429})


@dataclass(frozen=True, slots=True)
class EsiSettings:
    """Настройки клиента. Живут в config.py, чтобы развернувший подставил свой контакт."""

    user_agent: str
    compatibility_date: str
    timeout: float = DEFAULT_TIMEOUT
    cache_ttl: float = DEFAULT_CACHE_TTL  # для сборщика: не запрашивать чаще
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
    return await _request(
        client,
        settings,
        settings.base_url + ORDERS_PATH.format(region_id=region_id),
        {"type_id": type_id, "order_type": "all", "page": page},
        sleep=sleep,
        extra_headers=extra_headers,
        what="ордеров",
    )


async def _request(
    client: httpx.AsyncClient,
    settings: EsiSettings,
    url: str,
    params: Mapping[str, object],
    *,
    sleep: Callable[[float], Awaitable[None]],
    extra_headers: Mapping[str, str] | None = None,
    what: str = "данных",
) -> httpx.Response:
    """Единственный путь к сети: заголовки, ретраи на 5xx, разбор лимитов.

    Через него ходят и ордера, и история. Дублировать здесь логику ретраев
    нельзя: ровно на этом уже терялись повторы для первой страницы.
    """
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
            raise EsiError(f"ESI ответил {response.status_code} на запрос {what}")
        return response

    raise EsiError(f"ESI недоступен ({last_status})")  # недостижимо, но без «а вдруг»


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


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    """Ответ на условный запрос истории сделок (ESI §5).

    days — дневные итоги как их отдал ESI, от старых к новым. not_modified
    означает «с прошлого раза ничего не изменилось»: за сутки история меняется
    ровно один раз, в 11:05 UTC, поэтому 304 здесь норма, а не редкость.
    """

    region_id: int
    type_id: int
    days: list[dict] | None = None
    etag: str | None = None
    last_modified: str | None = None
    expires_at: datetime | None = None
    not_modified: bool = False
    error: str | None = None
    requests_made: int = 0
    error_limit_remain: int | None = None
    rate_limit_remain: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


async def fetch_history_conditional(
    client: httpx.AsyncClient,
    settings: EsiSettings,
    region_id: int,
    type_id: int,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> HistorySnapshot:
    """История сделок по типу в регионе (ESI §5.1).

    Пагинации у эндпоинта нет: ответ приходит целиком, около 412 дней сразу.
    Условных заголовков два — ETag здесь слабый (``W/"..."``), и это нормально,
    но вместе с ним ESI отдаёт Last-Modified, который тоже стоит использовать.

    В группы рейт-лимита эндпоинт не входит (проверено 18.08.2026, ESI §5.3),
    но лимит ошибок общий, и заголовки лимитов возвращаются наверх так же,
    как для ордеров.
    """
    conditional: dict[str, str] = {}
    if etag:
        conditional["If-None-Match"] = etag
    if last_modified:
        conditional["If-Modified-Since"] = last_modified

    try:
        response = await _request(
            client,
            settings,
            settings.base_url + HISTORY_PATH.format(region_id=region_id),
            {"type_id": type_id},
            sleep=sleep,
            extra_headers=conditional or None,
            what="истории сделок",
        )
    except RateLimitedError:
        raise
    except EsiError as exc:
        return HistorySnapshot(region_id, type_id, error=str(exc), requests_made=1)

    limits = {
        "error_limit_remain": error_limit_remaining(response),
        "rate_limit_remain": rate_limit_remaining(response),
    }

    if response.status_code == 304:
        return HistorySnapshot(
            region_id,
            type_id,
            not_modified=True,
            etag=etag,
            last_modified=last_modified,
            expires_at=_expires_at(response),
            requests_made=1,
            **limits,
        )

    try:
        days = list(response.json())
    except ValueError as exc:
        return HistorySnapshot(
            region_id, type_id, error=f"ESI вернул нечитаемый ответ: {exc}", requests_made=1
        )

    return HistorySnapshot(
        region_id,
        type_id,
        days=days,
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
        expires_at=_expires_at(response),
        requests_made=1,
        **limits,
    )


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
