"""Тесты кэша с TTL. Часы подменяются, тесты не спят."""

import pytest

from app.services.cache import TTLCache


def make_cache(ttl: float = 300.0):
    """Кэш с управляемыми часами: возвращает (кэш, список-с-текущим-временем)."""
    now = [0.0]
    return TTLCache[str, int](default_ttl=ttl, clock=lambda: now[0]), now


def test_value_survives_until_ttl():
    cache, now = make_cache(300)
    cache.set("a", 1)
    now[0] = 299.9
    assert cache.get("a") == 1


def test_value_expires_after_ttl():
    cache, now = make_cache(300)
    cache.set("a", 1)
    now[0] = 300.0
    assert cache.get("a") is None


def test_missing_key():
    cache, _ = make_cache()
    assert cache.get("нет такого") is None


def test_explicit_ttl_overrides_default():
    cache, now = make_cache(300)
    cache.set("a", 1, ttl=10)
    now[0] = 11
    assert cache.get("a") is None


def test_nonpositive_ttl_drops_previous_value():
    """Ответ, который уже протух, не должен подменить собой свежий."""
    cache, _ = make_cache(300)
    cache.set("a", 1)
    cache.set("a", 2, ttl=0)
    assert cache.get("a") is None


def test_clear():
    cache, _ = make_cache()
    cache.set("a", 1)
    cache.clear()
    assert cache.get("a") is None


def test_len_counts_only_alive():
    cache, now = make_cache(300)
    cache.set("a", 1, ttl=10)
    cache.set("b", 2, ttl=100)
    now[0] = 50
    assert len(cache) == 1


def test_ttl_must_be_positive():
    with pytest.raises(ValueError):
        TTLCache[str, int](default_ttl=0)
