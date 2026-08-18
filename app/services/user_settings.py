"""Настройки расчёта вошедшего пользователя.

Ровно тот же набор, что аноним держит в localStorage. Разница только в месте
хранения: у анонима браузер, у вошедшего база. Приложение обязано работать
в обоих режимах, и анонимный — основной.

Значения хранятся в том виде, в каком их вводят в форму: проценты процентами,
ставки доставки числами. Пересчёт в доли — забота разбора формы, а не хранилища.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import Engine, delete, select

from app.db import UserAccount, UserFreightRate, UserSettings, session_scope, utcnow

# Поля формы, которые сохраняются. Цены не сохраняются никогда: они устаревают
# за минуты, и подсунуть вчерашнюю цену вместо свежей — худшее, что можно
# сделать в калькуляторе денег.
SIMPLE_FIELDS = ("gas", "n_units", "structure", "gde_level", "broker_fee", "collateral_pct")


@dataclass(frozen=True, slots=True)
class StoredSettings:
    """Настройки в виде, готовом для подстановки в форму."""

    values: dict[str, str] = field(default_factory=dict)
    freight_rates: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.values and not self.freight_rates


def _decimal(raw: str | None) -> Decimal | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return Decimal(str(raw).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _plain(value: object) -> str:
    """Decimal(«1.50») в форме должен выглядеть как 1.5, а не 1.50.

    Одного normalize() мало: он схлопывает нули и в показатель степени,
    и ставка 500 возвращается из базы как «5E+2». В поле ввода это мусор,
    а при следующем сохранении такая строка ещё и уедет обратно в базу.
    Формат «f» экспоненту не использует никогда."""
    if isinstance(value, Decimal):
        return f"{value.normalize():f}"
    return str(value)


def ensure_account(engine: Engine, character_id: int, name: str) -> None:
    """Заводит аккаунт при первом входе, дальше только обновляет время входа."""
    with session_scope(engine) as session:
        account = session.get(UserAccount, character_id)
        if account is None:
            session.add(
                UserAccount(character_id=character_id, character_name=name)
            )
        else:
            account.character_name = name
            account.last_login_at = utcnow()


def load(engine: Engine, character_id: int) -> StoredSettings:
    """Настройки персонажа. Пусто — значит он ещё ничего не сохранял."""
    with session_scope(engine) as session:
        row = session.get(UserSettings, character_id)
        values: dict[str, str] = {}
        if row is not None:
            raw = {
                "gas": row.gas_key,
                "n_units": row.n_units,
                "structure": row.structure,
                "gde_level": row.gde_level,
                "broker_fee": row.broker_fee,
                "collateral_pct": row.collateral_pct,
            }
            values = {k: _plain(v) for k, v in raw.items() if v is not None}
            if row.sell_only:
                values["sell_only"] = "on"

        rates = session.scalars(
            select(UserFreightRate).where(UserFreightRate.character_id == character_id)
        ).all()
        freight = {r.hub_key: _plain(r.rate) for r in rates}

    return StoredSettings(values=values, freight_rates=freight)


def save(engine: Engine, character_id: int, form: Mapping[str, str]) -> None:
    """Сохраняет настройки из формы. Незаполненные поля затирают прежние на None.

    Ставки доставки переписываются целиком: иначе удалённая пользователем
    ставка осталась бы в базе и вернулась при следующем входе.
    """
    with session_scope(engine) as session:
        if session.get(UserAccount, character_id) is None:
            return  # чужой или удалённый персонаж — молча не сохраняем

        row = session.get(UserSettings, character_id)
        if row is None:
            row = UserSettings(character_id=character_id)
            session.add(row)

        row.gas_key = (form.get("gas") or "").strip() or None
        row.structure = (form.get("structure") or "").strip() or None
        row.n_units = _as_int(form.get("n_units"))
        row.gde_level = _as_int(form.get("gde_level"))
        row.broker_fee = _decimal(form.get("broker_fee"))
        row.collateral_pct = _decimal(form.get("collateral_pct"))
        row.sell_only = form.get("sell_only") is not None
        row.updated_at = utcnow()

        session.execute(
            delete(UserFreightRate).where(UserFreightRate.character_id == character_id)
        )
        for key, value in form.items():
            if not key.endswith("_rate"):
                continue
            rate = _decimal(value)
            if rate is None:
                continue
            session.add(
                UserFreightRate(
                    character_id=character_id,
                    hub_key=key[: -len("_rate")],
                    rate=rate,
                )
            )


def _as_int(raw: str | None) -> int | None:
    value = _decimal(raw)
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, OverflowError):
        return None
