from datetime import datetime

import pytest

from scripts import rail_transfer as rt


def test_arr_dt_uses_lishi_for_overnight():
    t = rt.Train(code="G1", from_code="A", to_code="B", dep="23:50",
                 arr="00:10", lishi="00:20", date="2026-10-20")
    assert t.arr_dt() == datetime(2026, 10, 21, 0, 10)


def test_arr_dt_uses_lishi_for_multi_day():
    t = rt.Train(code="Z1", from_code="A", to_code="B", dep="08:00",
                 arr="07:00", lishi="47:00", date="2026-10-20")
    assert t.arr_dt() == datetime(2026, 10, 22, 7, 0)


def test_arr_dt_falls_back_when_lishi_missing():
    t = rt.Train(code="G1", from_code="A", to_code="B", dep="23:50",
                 arr="00:10", lishi="", date="2026-10-20")
    assert t.arr_dt() == datetime(2026, 10, 21, 0, 10)


def test_duration_and_hhmm_parsers():
    assert rt._duration_min("12:34") == 754
    assert rt._duration_min("47:00") == 2820
    assert rt._duration_min("bad") is None
    assert rt._parse_hhmm("08:00") == 480
    assert rt._parse_hhmm("8:05") == 485
    assert rt._parse_hhmm("24:00") is None
    assert rt._parse_hhmm("") is None


def test_expand_dates_range_and_list():
    dates, err = rt._expand_dates("2026-10-20..2026-10-22")
    assert err == "" and dates == ["2026-10-20", "2026-10-21", "2026-10-22"]
    dates, err = rt._expand_dates("2026-10-20,2026-10-22,2026-10-20")
    assert err == "" and dates == ["2026-10-20", "2026-10-22"]
    assert rt._expand_dates("bad")[1]
    assert rt._expand_dates("2026-10-22..2026-10-20")[1]
    assert rt._expand_dates(",")[0] == []


@pytest.mark.parametrize("text,expected", [
    ("二等座", {"二等座"}),
    ("卧铺", {"软卧", "硬卧"}),
    ("二等座,卧铺", {"二等座", "软卧", "硬卧"}),
])
def test_normalize_seat_classes(text, expected):
    classes, bad = rt._normalize_seat_classes(text)
    assert classes == expected and bad == []


def test_normalize_seat_classes_rejects_unknown():
    classes, bad = rt._normalize_seat_classes("火箭座")
    assert classes == set() and bad == ["火箭座"]
