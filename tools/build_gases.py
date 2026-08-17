#!/usr/bin/env python3
"""Разовая генерация data/gases.json из SDE.

Запускается разработчиком вручную и в рантайме не участвует: SDE не является
зависимостью приложения (CLAUDE.md). Приложение читает готовый data/gases.json.

Зависимостей нет вообще — только стандартная библиотека. PyYAML сюда не тянем:
в requirements.txt ровно четыре пакета, а пятый ради скрипта, который запускают
раз в год, того не стоит. Формат fsd-файлов SDE машинно-порождённый и предсказуемый,
поэтому нужные поля вынимаются построчным сканером.

Где взять SDE:

    https://eve-static-data-export.s3-eu-west-1.amazonaws.com/tranquility/sde.zip

Запуск (архив распаковывать не нужно):

    python tools/build_gases.py --sde путь/к/sde.zip
    python tools/build_gases.py --sde путь/к/распакованному/каталогу --check

--check ничего не пишет: только сверяет SDE с текущим data/gases.json и
показывает расхождения. Полезно после патчей CCP.

Что скрипт делает:

1. Находит в SDE все сжатые газы (группа COMPRESSED_GAS_GROUP_ID) и их сырые
   исходники через typeMaterials: у сжатого газа ровно один материал в количестве
   1 — соответствующий сырой газ. Это и есть источник raw_type_id, который
   запрещено заполнять вручную.
2. Сверяет объёмы: объём сырого = объём сжатого * COMPRESSION_VOLUME_RATIO.
3. Сохраняет порядок газов из существующего файла — чтобы диff был читаемым,
   а не перетасовкой 25 записей. Новые типы дописываются в конец с предупреждением.

Любое расхождение — ошибка и ненулевой код возврата. Файл при этом не пишется:
лучше не обновить справочник, чем записать в него правдоподобную чушь.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.core.constants import (  # noqa: E402  (после правки sys.path)
    COMPRESSION_QUANTITY_RATIO,
    COMPRESSION_VOLUME_RATIO,
)

# --- Константы SDE ---
#
# Идентификаторы групп нужны только этому скрипту и в приложение не попадают,
# поэтому живут здесь, а не в app/core/constants.py. Оба выверены по самому SDE:
# все 25 сырых газов лежат в группе 711, все 25 сжатых — в 4168.
RAW_GAS_GROUP_ID = 711
COMPRESSED_GAS_GROUP_ID = 4168

# Семейства газов (DOMAIN §2). Определяются по названию типа.
FAMILIES = ("fullerite", "mykoserocin", "cytoserocin")

FSD_TYPES = "fsd/types.yaml"
FSD_TYPE_MATERIALS = "fsd/typeMaterials.yaml"

# Верхнеуровневый ключ fsd-файла — целочисленный type_id в нулевой колонке.
# Именно так, а не «строка без отступа»: внутри многоязычных описаний
# встречаются пустые строки в нулевой колонке, и наивный сканер на них ломается.
TOP_LEVEL = re.compile(r"^(\d+):\s*$")

MATERIAL_ID = re.compile(r"^\s*-\s*materialTypeID:\s*(\d+)\s*$")
MATERIAL_QTY = re.compile(r"^\s*quantity:\s*(\d+)\s*$")


class BuildError(Exception):
    """Расхождение в данных: справочник не обновляем."""


@dataclass(frozen=True, slots=True)
class TypeInfo:
    """Всё, что нам нужно знать о типе из SDE."""

    type_id: int
    name: str
    volume: float
    group_id: int


# --- Чтение SDE ---


def read_lines(source: Path, member: str) -> Iterator[str]:
    """Строки fsd-файла — из zip-архива или из распакованного каталога."""
    if source.is_dir():
        path = source / member
        if not path.exists():  # распакованный SDE иногда лежит без верхней папки
            path = source / Path(member).name
        if not path.exists():
            raise BuildError(f"В каталоге {source} не найден {member}")
        with path.open(encoding="utf-8") as handle:
            yield from handle
        return

    if not zipfile.is_zipfile(source):
        raise BuildError(f"{source} — не zip-архив и не каталог с SDE")
    with zipfile.ZipFile(source) as archive:
        try:
            entry = archive.open(member)
        except KeyError:
            raise BuildError(f"В архиве {source.name} нет {member}") from None
        with entry as raw:
            for line in raw:
                yield line.decode("utf-8")


def unquote(value: str) -> str:
    """Снимает кавычки со скалярного значения YAML."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        inner = value[1:-1]
        return inner.replace("''", "'") if value[0] == "'" else inner
    return value


def parse_type_materials(lines: Iterator[str]) -> dict[int, list[tuple[int, int]]]:
    """typeMaterials.yaml → {type_id: [(material_type_id, quantity), ...]}."""
    result: dict[int, list[tuple[int, int]]] = {}
    current: int | None = None
    pending_material: int | None = None

    for line in lines:
        top = TOP_LEVEL.match(line)
        if top:
            current = int(top.group(1))
            result[current] = []
            pending_material = None
            continue
        if current is None:
            continue
        material = MATERIAL_ID.match(line)
        if material:
            pending_material = int(material.group(1))
            continue
        quantity = MATERIAL_QTY.match(line)
        if quantity and pending_material is not None:
            result[current].append((pending_material, int(quantity.group(1))))
            pending_material = None
    return result


def parse_types(lines: Iterator[str], group_ids: set[int]) -> dict[int, TypeInfo]:
    """types.yaml → типы нужных групп с названием и объёмом.

    Файл на 146 МБ, поэтому читается потоком и в память попадает только нужное.
    Английское название берётся из блока name, а не description: у обоих есть
    ключ en, и перепутать их легко.

    Неопубликованные типы пропускаются: в группе сырых газов лежит служебная
    заглушка «Blockout Cone» без объёма, и она не газ. Если из-за этого фильтра
    выпадет настоящий газ, сборка всё равно упадёт — на этапе поиска пары.
    """
    result: dict[int, TypeInfo] = {}
    current: int | None = None
    fields: dict[str, str] = {}
    in_name_block = False

    def flush() -> None:
        if current is None:
            return
        group_id = fields.get("groupID")
        if group_id is None or int(group_id) not in group_ids:
            return
        if fields.get("published") != "true":
            return
        name = fields.get("name")
        volume = fields.get("volume")
        if name is None or volume is None:
            raise BuildError(f"У типа {current} в SDE нет названия или объёма")
        result[current] = TypeInfo(
            type_id=current, name=name, volume=float(volume), group_id=int(group_id)
        )

    for line in lines:
        top = TOP_LEVEL.match(line)
        if top:
            flush()
            current = int(top.group(1))
            fields = {}
            in_name_block = False
            continue
        if current is None:
            continue

        stripped = line.rstrip("\n")
        indent = len(stripped) - len(stripped.lstrip(" "))
        if indent == 2:
            key, _, value = stripped.strip().partition(":")
            in_name_block = key == "name"
            if key in ("volume", "groupID", "published"):
                fields[key] = value.strip()
        elif indent == 4 and in_name_block:
            key, _, value = stripped.strip().partition(":")
            if key == "en":
                fields["name"] = unquote(value)
    flush()
    return result


# --- Сборка справочника ---


def gas_key(name: str) -> str:
    """«Fullerite-C320» → fullerite_c320, «Amber Mykoserocin» → amber_mykoserocin."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def gas_family(name: str) -> str:
    """Семейство газа по названию (DOMAIN §2)."""
    lowered = name.lower()
    for family in FAMILIES:
        if family in lowered:
            return family
    raise BuildError(f"Не удалось определить семейство газа по названию {name!r}")


def pair_gases(
    materials: dict[int, list[tuple[int, int]]],
    types: dict[int, TypeInfo],
) -> list[dict[str, object]]:
    """Сжатый газ + его сырой исходник → записи справочника.

    Пары строятся только через typeMaterials: сжатый тип обязан иметь ровно один
    материал в количестве COMPRESSION_QUANTITY_RATIO, и этот материал обязан быть
    сырым газом. Всё остальное — повод упасть, а не догадываться.
    """
    entries: list[dict[str, object]] = []
    compressed_ids = sorted(
        type_id for type_id, info in types.items() if info.group_id == COMPRESSED_GAS_GROUP_ID
    )
    if not compressed_ids:
        raise BuildError(
            f"В SDE не найдено ни одного типа группы {COMPRESSED_GAS_GROUP_ID} — "
            f"группа сжатых газов могла поменяться"
        )

    for compressed_id in compressed_ids:
        compressed = types[compressed_id]
        recipe = materials.get(compressed_id, [])
        if len(recipe) != 1:
            raise BuildError(
                f"У сжатого газа {compressed.name} ({compressed_id}) "
                f"{len(recipe)} материалов вместо одного"
            )
        raw_id, quantity = recipe[0]
        if quantity != COMPRESSION_QUANTITY_RATIO:
            raise BuildError(
                f"{compressed.name}: соотношение по количеству {quantity}, "
                f"а не {COMPRESSION_QUANTITY_RATIO}"
            )
        raw = types.get(raw_id)
        if raw is None or raw.group_id != RAW_GAS_GROUP_ID:
            raise BuildError(
                f"{compressed.name}: материал {raw_id} не является сырым газом "
                f"группы {RAW_GAS_GROUP_ID}"
            )
        if abs(raw.volume - compressed.volume * COMPRESSION_VOLUME_RATIO) > 1e-9:
            raise BuildError(
                f"{raw.name}: объёмы не согласованы — сырой {raw.volume}, "
                f"сжатый {compressed.volume}, ожидалось соотношение "
                f"1:{COMPRESSION_VOLUME_RATIO}"
            )

        entries.append(
            {
                "key": gas_key(raw.name),
                "name": raw.name,
                "family": gas_family(raw.name),
                "volume_raw": raw.volume,
                "volume_compressed": compressed.volume,
                "raw_type_id": raw_id,
                "compressed_type_id": compressed_id,
            }
        )
    return entries


def order_like(entries: list[dict[str, object]], previous: list[dict]) -> list[dict[str, object]]:
    """Раскладывает записи в порядке прежнего файла; новые — в конец."""
    order = {gas["key"]: index for index, gas in enumerate(previous)}
    known = [entry for entry in entries if entry["key"] in order]
    fresh = [entry for entry in entries if entry["key"] not in order]
    known.sort(key=lambda entry: order[entry["key"]])
    for entry in fresh:
        print(f"  новый тип газа: {entry['name']}", file=sys.stderr)
    return known + fresh


def diff_against(previous: list[dict], entries: list[dict[str, object]]) -> list[str]:
    """Человекочитаемые расхождения между старым файлом и тем, что даёт SDE."""
    old = {gas["key"]: gas for gas in previous}
    new = {entry["key"]: entry for entry in entries}
    problems: list[str] = []

    for key in sorted(set(old) - set(new)):
        problems.append(f"{key}: есть в справочнике, но не найден в SDE")
    for key in sorted(set(new) - set(old)):
        problems.append(f"{key}: новый тип в SDE, в справочнике его нет")
    for key in sorted(set(old) & set(new)):
        for field in ("name", "family", "volume_raw", "volume_compressed",
                      "raw_type_id", "compressed_type_id"):
            was, now = old[key].get(field), new[key][field]
            if was != now and not (field == "raw_type_id" and was is None):
                problems.append(f"{key}.{field}: было {was!r}, в SDE {now!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Генерирует data/gases.json из SDE. В рантайме не используется.",
    )
    parser.add_argument("--sde", required=True, type=Path,
                        help="путь к sde.zip или к распакованному каталогу SDE")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data" / "gases.json",
                        help="куда писать справочник (по умолчанию data/gases.json)")
    parser.add_argument("--version", default=date.today().isoformat(),
                        help="значение поля version в файле (по умолчанию сегодняшняя дата)")
    parser.add_argument("--check", action="store_true",
                        help="ничего не писать, только показать расхождения")
    args = parser.parse_args(argv)

    if not args.sde.exists():
        print(f"Не найден SDE: {args.sde}", file=sys.stderr)
        return 2

    try:
        print("Читаю typeMaterials.yaml...", file=sys.stderr)
        materials = parse_type_materials(read_lines(args.sde, FSD_TYPE_MATERIALS))
        print("Читаю types.yaml (146 МБ, это займёт несколько секунд)...", file=sys.stderr)
        types = parse_types(
            read_lines(args.sde, FSD_TYPES),
            {RAW_GAS_GROUP_ID, COMPRESSED_GAS_GROUP_ID},
        )
        entries = pair_gases(materials, types)
    except BuildError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1

    previous: dict = {}
    if args.out.exists():
        previous = json.loads(args.out.read_text(encoding="utf-8"))
    previous_gases = previous.get("gases", [])

    if previous_gases:
        entries = order_like(entries, previous_gases)
        problems = diff_against(previous_gases, entries)
        if problems:
            print("Расхождения с текущим справочником:", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
        else:
            print("Расхождений с текущим справочником нет.", file=sys.stderr)

    payload = {
        "version": args.version,
        "note": previous.get(
            "note",
            "Сгенерировано tools/build_gases.py из SDE. Вручную не править.",
        ),
        "compression": {
            "quantity_ratio": COMPRESSION_QUANTITY_RATIO,
            "volume_ratio": COMPRESSION_VOLUME_RATIO,
        },
        "gases": entries,
    }

    if args.check:
        print(f"--check: файл не записан. Газов в SDE: {len(entries)}.", file=sys.stderr)
        return 0

    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Записано {len(entries)} газов в {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
