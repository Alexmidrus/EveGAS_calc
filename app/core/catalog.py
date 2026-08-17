"""Загрузка справочника газов из data/gases.json и справочник хабов.

Файл читается один раз за процесс. При загрузке данные сверяются
с константами: расхождение — ошибка, а не молчаливая подстановка.
"""

import json
import math
from functools import lru_cache
from pathlib import Path

from app.core import constants
from app.core.models import Gas, Hub

_GASES_PATH = Path(__file__).resolve().parents[2] / "data" / "gases.json"


@lru_cache(maxsize=1)
def gases() -> tuple[Gas, ...]:
    """Все газы из справочника, в порядке файла."""
    payload = json.loads(_GASES_PATH.read_text(encoding="utf-8"))

    compression = payload["compression"]
    if compression["quantity_ratio"] != constants.COMPRESSION_QUANTITY_RATIO:
        raise ValueError(
            f"quantity_ratio в gases.json ({compression['quantity_ratio']}) "
            f"не совпадает с константой ({constants.COMPRESSION_QUANTITY_RATIO})"
        )
    if compression["volume_ratio"] != constants.COMPRESSION_VOLUME_RATIO:
        raise ValueError(
            f"volume_ratio в gases.json ({compression['volume_ratio']}) "
            f"не совпадает с константой ({constants.COMPRESSION_VOLUME_RATIO})"
        )

    result: list[Gas] = []
    seen: set[str] = set()
    for entry in payload["gases"]:
        gas = Gas(
            key=entry["key"],
            name=entry["name"],
            family=entry["family"],
            volume_raw=float(entry["volume_raw"]),
            volume_compressed=float(entry["volume_compressed"]),
            raw_type_id=entry["raw_type_id"],
            compressed_type_id=entry["compressed_type_id"],
        )
        if gas.key in seen:
            raise ValueError(f"Дубликат ключа газа в gases.json: {gas.key!r}")
        seen.add(gas.key)
        if not math.isclose(
            gas.volume_compressed * constants.COMPRESSION_VOLUME_RATIO, gas.volume_raw
        ):
            raise ValueError(
                f"Объёмы газа {gas.key!r} не согласованы: "
                f"{gas.volume_compressed} * {constants.COMPRESSION_VOLUME_RATIO} "
                f"!= {gas.volume_raw}"
            )
        result.append(gas)

    if not result:
        raise ValueError("gases.json не содержит ни одного газа")
    return tuple(result)


@lru_cache(maxsize=1)
def _gas_index() -> dict[str, Gas]:
    """Индекс газов по ключу."""
    return {gas.key: gas for gas in gases()}


def gas_by_key(key: str) -> Gas:
    """Газ по ключу. Неизвестный ключ — KeyError с внятным сообщением."""
    try:
        return _gas_index()[key]
    except KeyError:
        raise KeyError(f"Неизвестный газ: {key!r}") from None


@lru_cache(maxsize=1)
def hubs() -> tuple[Hub, ...]:
    """Трейд-хабы в порядке интерфейса (constants.HUBS)."""
    return tuple(Hub(**hub) for hub in constants.HUBS)  # type: ignore[arg-type]


@lru_cache(maxsize=1)
def _hub_index() -> dict[str, Hub]:
    """Индекс хабов по ключу."""
    return {hub.key: hub for hub in hubs()}


def hub_by_key(key: str) -> Hub:
    """Хаб по ключу. Неизвестный ключ — KeyError с внятным сообщением."""
    try:
        return _hub_index()[key]
    except KeyError:
        raise KeyError(f"Неизвестный хаб: {key!r}") from None
