"""Shared fixtures for the rail-transfer test suite."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts import rail_transfer as rt


# --- synthetic station table ------------------------------------------------
STATIONS = [
    rt.Station(name="甲站", telecode="AAA", pinyin="jia", abbr="j", city="甲", city_code="0100"),
    rt.Station(name="乙站", telecode="BBB", pinyin="yi", abbr="y", city="乙", city_code="0200"),
    rt.Station(name="乙东", telecode="BBD", pinyin="yidong", abbr="yd", city="乙", city_code="0200"),
    rt.Station(name="丙站", telecode="CCC", pinyin="bing", abbr="b", city="丙", city_code="0300"),
    rt.Station(name="丁站", telecode="DDD", pinyin="ding", abbr="d", city="丁", city_code="0400"),
]


@pytest.fixture
def table() -> rt.StationTable:
    return rt.StationTable(STATIONS)


def train(code: str, from_code: str, to_code: str, dep: str, arr: str,
          lishi: str, date: str = "2026-10-20", **seats) -> rt.Train:
    values = {"ze": "有"}
    values.update(seats)
    return rt.Train(code=code, from_code=from_code, to_code=to_code,
                    dep=dep, arr=arr, lishi=lishi, date=date, **values)


def row(code: str, start: str, end: str, frm: str, to: str,
        dep: str, arr: str, lishi: str, *,
        wz: str = "", rw: str = "", yw: str = "", yz: str = "",
        ze: str = "有", zy: str = "", swz: str = "",
        train_no: str = "", from_no: str = "1", to_no: str = "2",
        seat_types: str = "O") -> list[str]:
    """Build one page-sliced row in the exact order _QUERY_JS returns."""
    return [code, start, end, frm, to, dep, arr, lishi,
            wz, rw, yw, yz, ze, zy, swz,
            train_no or code, from_no, to_no, seat_types]


class FakeEngine:
    """Minimal QueryEngine stand-in for pure planning tests."""

    def __init__(self, mapping: dict[tuple[str, str], list[rt.Train]]):
        self.mapping = mapping
        self.calls = 0

    def query_pairs(self, jobs):
        self.calls += len(jobs)
        return {job: list(self.mapping.get(job[:2], [])) for job in jobs}


_QUERY_RE = re.compile(
    r"leftTicketDTO\.train_date=([0-9-]+)&"
    r"leftTicketDTO\.from_station=([A-Z0-9]+)&"
    r"leftTicketDTO\.to_station=([A-Z0-9]+)")


class FakeSession:
    """Answers the real page-context expressions without a browser."""

    def __init__(self, rows: dict[tuple[str, str, str], list[list[str]]],
                 prices: dict[str, dict] | None = None):
        self.rows = rows
        self.prices = prices or {}
        self.queries: list[tuple[str, str, str]] = []
        self.price_queries: list[str] = []

    def evaluate_many(self, exprs):
        out = []
        for expr in exprs:
            m = _QUERY_RE.search(expr)
            if not m:
                out.append(json.dumps({"_err": "fake session cannot parse query"}))
                continue
            date_str, frm, to = m.groups()
            self.queries.append((frm, to, date_str))
            out.append(json.dumps({
                "_ok": 1,
                "rows": self.rows.get((frm, to, date_str), []),
            }, ensure_ascii=False))
        return out

    def query_price(self, train):
        self.price_queries.append(train.code)
        return {"_ok": 1, "data": self.prices.get(train.code, {})}

    def start(self):
        pass

    def close(self):
        pass


@pytest.fixture
def args_factory():
    def make(*extra: str):
        base = ["--from", "甲站", "--to", "丁站", "--date", "2026-10-20"]
        return rt.build_parser().parse_args(base + list(extra))
    return make


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Keep the suite fast; the throttle only matters against the real API."""
    monkeypatch.setattr(rt.time, "sleep", lambda _seconds: None)


@pytest.fixture
def disabled_cache(tmp_path: Path) -> rt.QueryCache:
    return rt.QueryCache(tmp_path / "cache.json", ttl=600, enabled=False)


# --- factories exposed as fixtures (keeps test modules import-free) ---------
@pytest.fixture
def row_factory():
    return row


@pytest.fixture
def train_factory():
    return train


@pytest.fixture
def fake_engine_cls():
    return FakeEngine


@pytest.fixture
def fake_session_cls():
    return FakeSession
