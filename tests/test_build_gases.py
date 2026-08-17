"""Тесты генератора справочника tools/build_gases.py.

SDE здесь не нужен: разбор проверяется на коротких фрагментах в формате fsd.
Скрипт в рантайме не участвует, но ошибка в нём попадает прямо в data/gases.json,
из которого приложение берёт type_id для запросов к ESI.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import build_gases as bg

# Фрагмент типа в формате fsd. Внутри описания намеренно оставлены пустые строки
# в нулевой колонке — на них ломается наивный сканер (реальный случай из SDE).
TYPES_YAML = """\
30377:
  description:
    de: Fullerite besteht aus verdichteten Fullerenmolekülen
      und ist üblicherweise Bestandteil interstellarer Gaswolken.

    es: 'La fullerita es la manifestación sólida.

      Aunque todos los imperios.'
    en: "Fullerite is the solid-state manifestation of fullerene molecules."
  groupID: 711
  name:
    en: Fullerite-C320
  portionSize: 1
  published: true
  volume: 5.0
50175:
  groupID: 711
  name:
    en: Blockout Cone
  published: false
62406:
  description:
    en: Compressed gas.
  groupID: 4168
  name:
    en: Compressed Fullerite-C320
  portionSize: 1
  published: true
  volume: 0.5
99999:
  groupID: 18
  name:
    en: Tritanium
  published: true
  volume: 0.01
"""

MATERIALS_YAML = """\
62406:
  materials:
  - materialTypeID: 30377
    quantity: 1
99999:
  materials:
  - materialTypeID: 34
    quantity: 175
  - materialTypeID: 36
    quantity: 70
"""


def lines(text: str):
    return iter(text.splitlines(keepends=True))


class TestParseTypes:
    def test_reads_name_volume_and_group(self):
        types = bg.parse_types(lines(TYPES_YAML), {711, 4168})
        assert types[30377].name == "Fullerite-C320"
        assert types[30377].volume == 5.0
        assert types[30377].group_id == 711
        assert types[62406].name == "Compressed Fullerite-C320"
        assert types[62406].volume == 0.5

    def test_name_not_taken_from_description(self):
        """У description тоже есть ключ en — перепутать легко, но нельзя."""
        types = bg.parse_types(lines(TYPES_YAML), {711})
        assert "solid-state" not in types[30377].name

    def test_blank_lines_inside_description_do_not_break_parsing(self):
        """Пустая строка в нулевой колонке — не начало нового типа."""
        types = bg.parse_types(lines(TYPES_YAML), {711, 4168})
        assert set(types) == {30377, 62406}  # 50175 не опубликован, 99999 чужой группы

    def test_unpublished_types_are_skipped(self):
        """«Blockout Cone» лежит в группе газов, но это служебная заглушка без объёма."""
        types = bg.parse_types(lines(TYPES_YAML), {711})
        assert 50175 not in types

    def test_other_groups_ignored(self):
        types = bg.parse_types(lines(TYPES_YAML), {711})
        assert 99999 not in types

    def test_published_type_without_volume_is_an_error(self):
        broken = "700:\n  groupID: 711\n  name:\n    en: Strange Gas\n  published: true\n"
        with pytest.raises(bg.BuildError):
            bg.parse_types(lines(broken), {711})


class TestParseTypeMaterials:
    def test_single_material(self):
        materials = bg.parse_type_materials(lines(MATERIALS_YAML))
        assert materials[62406] == [(30377, 1)]

    def test_multiple_materials(self):
        materials = bg.parse_type_materials(lines(MATERIALS_YAML))
        assert materials[99999] == [(34, 175), (36, 70)]


class TestKeysAndFamilies:
    @pytest.mark.parametrize(
        ("name", "key"),
        [
            ("Fullerite-C320", "fullerite_c320"),
            ("Amber Mykoserocin", "amber_mykoserocin"),
            ("Chartreuse Cytoserocin", "chartreuse_cytoserocin"),
        ],
    )
    def test_gas_key(self, name, key):
        assert bg.gas_key(name) == key

    @pytest.mark.parametrize(
        ("name", "family"),
        [
            ("Fullerite-C50", "fullerite"),
            ("Golden Mykoserocin", "mykoserocin"),
            ("Gamboge Cytoserocin", "cytoserocin"),
        ],
    )
    def test_gas_family(self, name, family):
        assert bg.gas_family(name) == family

    def test_unknown_family_is_an_error(self):
        with pytest.raises(bg.BuildError):
            bg.gas_family("Veldspar")


class TestPairGases:
    def test_pairs_compressed_with_raw(self):
        types = bg.parse_types(lines(TYPES_YAML), {711, 4168})
        materials = bg.parse_type_materials(lines(MATERIALS_YAML))
        entries = bg.pair_gases(materials, types)
        assert entries == [
            {
                "key": "fullerite_c320",
                "name": "Fullerite-C320",
                "family": "fullerite",
                "volume_raw": 5.0,
                "volume_compressed": 0.5,
                "raw_type_id": 30377,
                "compressed_type_id": 62406,
            }
        ]

    def test_two_materials_is_an_error(self):
        """Сжатый газ обязан иметь ровно один материал — иначе это не 1:1."""
        types = bg.parse_types(lines(TYPES_YAML), {711, 4168})
        materials = {62406: [(30377, 1), (34, 5)]}
        with pytest.raises(bg.BuildError, match="материал"):
            bg.pair_gases(materials, types)

    def test_wrong_quantity_is_an_error(self):
        types = bg.parse_types(lines(TYPES_YAML), {711, 4168})
        with pytest.raises(bg.BuildError, match="количеству"):
            bg.pair_gases({62406: [(30377, 2)]}, types)

    def test_material_outside_gas_group_is_an_error(self):
        types = bg.parse_types(lines(TYPES_YAML), {711, 4168, 18})
        with pytest.raises(bg.BuildError, match="сырым газом"):
            bg.pair_gases({62406: [(99999, 1)]}, types)

    def test_volume_mismatch_is_an_error(self):
        """Объёмы обязаны сходиться 10:1 — иначе справочник врёт про доставку."""
        types = bg.parse_types(
            lines(TYPES_YAML.replace("  volume: 0.5", "  volume: 0.7")), {711, 4168}
        )
        with pytest.raises(bg.BuildError, match="объёмы не согласованы"):
            bg.pair_gases(bg.parse_type_materials(lines(MATERIALS_YAML)), types)

    def test_no_compressed_types_is_an_error(self):
        """Группа сжатых газов могла поменяться — молчать об этом нельзя."""
        types = bg.parse_types(lines(TYPES_YAML), {711})
        with pytest.raises(bg.BuildError, match="группа сжатых газов"):
            bg.pair_gases({}, types)


class TestOrderAndDiff:
    def test_existing_order_is_preserved(self):
        entries = [{"key": "b", "name": "B"}, {"key": "a", "name": "A"},
                   {"key": "new", "name": "New"}]
        previous = [{"key": "a"}, {"key": "b"}]
        assert [e["key"] for e in bg.order_like(entries, previous)] == ["a", "b", "new"]

    def test_diff_reports_new_and_missing(self):
        previous = [{"key": "gone", "name": "Gone"}]
        entries = [{"key": "fresh", "name": "Fresh"}]
        problems = bg.diff_against(previous, entries)
        assert any("gone" in p for p in problems)
        assert any("fresh" in p for p in problems)

    def test_filled_raw_type_id_is_not_a_discrepancy(self):
        """Заполнение прежнего null — это цель скрипта, а не расхождение."""
        previous = [{"key": "g", "name": "G", "family": "fullerite", "volume_raw": 5.0,
                     "volume_compressed": 0.5, "raw_type_id": None,
                     "compressed_type_id": 62406}]
        entries = [{"key": "g", "name": "G", "family": "fullerite", "volume_raw": 5.0,
                    "volume_compressed": 0.5, "raw_type_id": 30377,
                    "compressed_type_id": 62406}]
        assert bg.diff_against(previous, entries) == []

    def test_changed_id_is_a_discrepancy(self):
        previous = [{"key": "g", "compressed_type_id": 1}]
        entries = [{"key": "g", "name": "G", "family": "fullerite", "volume_raw": 5.0,
                    "volume_compressed": 0.5, "raw_type_id": 2, "compressed_type_id": 62406}]
        assert any("compressed_type_id" in p for p in bg.diff_against(previous, entries))


class TestGeneratedCatalogIsInSync:
    """Файл в репозитории должен быть согласован сам с собой."""

    def test_catalog_matches_its_own_rules(self):
        payload = json.loads(
            (Path(__file__).resolve().parents[1] / "data" / "gases.json").read_text(
                encoding="utf-8"
            )
        )
        for gas in payload["gases"]:
            assert bg.gas_key(gas["name"]) == gas["key"]
            assert bg.gas_family(gas["name"]) == gas["family"]
            assert gas["raw_type_id"] != gas["compressed_type_id"]
