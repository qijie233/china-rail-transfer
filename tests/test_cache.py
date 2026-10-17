import json

import pytest

from scripts import rail_transfer as rt


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_cache_roundtrip_and_availability_ttl(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(rt.time, "time", clock)
    path = tmp_path / "cache.json"
    cache = rt.QueryCache(path, ttl=600)
    cache.put(("甲", "丁", "2026-10-20"), [["row"]])
    cache.save()

    loaded = rt.QueryCache(path, ttl=600)
    assert loaded.get(("甲", "丁", "2026-10-20")) == [["row"]]

    clock.now += 601
    assert loaded.get(("甲", "丁", "2026-10-20")) is None


def test_price_cache_keeps_its_own_ttl(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(rt.time, "time", clock)
    path = tmp_path / "cache.json"
    cache = rt.QueryCache(path, ttl=600)
    cache.put(("price", "t", "1", "2", "2026-10-20"), {"二等座": "¥10"}, ttl=86400)
    cache.save()

    clock.now += 601
    loaded = rt.QueryCache(path, ttl=600)
    assert loaded.get(("price", "t", "1", "2", "2026-10-20")) == {"二等座": "¥10"}

    clock.now += 86400
    loaded = rt.QueryCache(path, ttl=600)
    assert loaded.get(("price", "t", "1", "2", "2026-10-20")) is None


def test_cache_schema_mismatch_is_ignored(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"_schema": 999, "k": {"t": 0, "rows": []}}),
                    encoding="utf-8")
    cache = rt.QueryCache(path, ttl=600)
    assert cache.data == {}


def test_disabled_cache_never_reads_or_writes(tmp_path):
    path = tmp_path / "cache.json"
    cache = rt.QueryCache(path, ttl=600, enabled=False)
    cache.put(("a", "b", "c"), [])
    cache.save()
    assert not path.exists()
    assert cache.get(("a", "b", "c")) is None


@pytest.mark.parametrize("limit,expected", [(0, 1 << 30), (3, 3)])
def test_request_budget_remaining(limit, expected):
    budget = rt.RequestBudget(limit)
    assert budget.remaining == expected


def test_request_budget_take_and_exhaust():
    budget = rt.RequestBudget(3)
    assert budget.take(2) == 2
    assert budget.remaining == 1
    assert budget.take(5) == 1
    assert budget.used == 3 and budget.exhausted
    assert budget.take(1) == 0
