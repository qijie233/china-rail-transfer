from datetime import datetime

import pytest

from scripts import rail_transfer as rt


def test_departure_window(args_factory, train_factory):
    args = args_factory("--depart-after", "09:00", "--depart-before", "10:00")
    today, now = "2026-10-20", datetime(2026, 10, 20, 7, 0)
    assert not rt._departure_ok(train_factory("G1", "AAA", "DDD", "08:30",
                                              "10:00", "01:30"), args, today, now, today)
    assert rt._departure_ok(train_factory("G2", "AAA", "DDD", "09:30",
                                          "11:00", "01:30"), args, today, now, today)
    assert not rt._departure_ok(train_factory("G3", "AAA", "DDD", "10:30",
                                              "12:00", "01:30"), args, today, now, today)


def test_arrival_window(args_factory, train_factory):
    args = args_factory("--arrive-after", "11:00", "--arrive-before", "13:00")
    assert not rt._arrival_ok(train_factory("G1", "AAA", "DDD", "08:00",
                                            "10:30", "02:30"), args, "2026-10-20")
    assert rt._arrival_ok(train_factory("G2", "AAA", "DDD", "08:00",
                                        "12:00", "04:00"), args, "2026-10-20")
    assert not rt._arrival_ok(train_factory("G3", "AAA", "DDD", "08:00",
                                            "13:30", "05:30"), args, "2026-10-20")


def test_departed_today_is_hidden_unless_requested(args_factory, train_factory):
    today, now = "2026-10-20", datetime(2026, 10, 20, 12, 0)
    past = train_factory("G1", "AAA", "DDD", "11:00", "12:30", "01:30", date=today)
    future = train_factory("G2", "AAA", "DDD", "13:00", "14:30", "01:30", date=today)

    args = args_factory()
    assert not rt._departure_ok(past, args, today, now, today)
    assert rt._departure_ok(future, args, today, now, today)

    shown = args_factory("--show-departed")
    assert rt._departure_ok(past, shown, today, now, today)


def test_plan_filters_seats_and_wait(args_factory, train_factory, table):
    args = args_factory("--seat-class", "二等座", "--max-wait", "20")
    t1 = train_factory("G1", "AAA", "BBB", "08:00", "10:00", "02:00")
    t2 = train_factory("G2", "BBB", "DDD", "10:30", "12:00", "01:30")
    plan = rt.Plan(legs=[t1, t2], transfers=[
        rt.Transfer("乙", "BBB", "BBB", True, 30, 16)])
    today, now = "2026-10-20", datetime(2026, 10, 20, 7, 0)
    assert not rt._plan_matches_filters(plan, args, {"二等座"}, today, now, today)

    easy = args_factory("--seat-class", "二等座", "--max-wait", "45")
    assert rt._plan_matches_filters(plan, easy, {"二等座"}, today, now, today)

    t2_no_seat = train_factory("G2", "BBB", "DDD", "10:30", "12:00", "01:30", ze="无")
    plan2 = rt.Plan(legs=[t1, t2_no_seat], transfers=plan.transfers)
    assert not rt._plan_matches_filters(plan2, easy, {"二等座"}, today, now, today)


def test_plan_filters_departure_and_arrival(args_factory, train_factory):
    t1 = train_factory("G1", "AAA", "BBB", "08:00", "10:00", "02:00")
    t2 = train_factory("G2", "BBB", "DDD", "10:30", "12:00", "01:30")
    plan = rt.Plan(legs=[t1, t2], transfers=[
        rt.Transfer("乙", "BBB", "BBB", True, 30, 16)])
    today, now = "2026-10-20", datetime(2026, 10, 20, 7, 0)

    late = args_factory("--depart-after", "09:00")
    assert not rt._plan_matches_filters(plan, late, set(), today, now, today)

    early_arrival = args_factory("--arrive-before", "11:00")
    assert not rt._plan_matches_filters(plan, early_arrival, set(), today, now, today)


@pytest.mark.parametrize("sort_key", ["total", "arrive", "depart", "gap", "price"])
def test_sort_trains_accepts_all_keys(train_factory, sort_key):
    trains = [
        train_factory("G1", "AAA", "DDD", "08:00", "12:00", "04:00"),
        train_factory("G2", "AAA", "DDD", "09:00", "11:00", "02:00"),
    ]
    out = rt._sort_trains(trains, sort_key)
    assert len(out) == 2


def test_arrival_window_is_relative_to_query_date(args_factory, train_factory):
    # 20:00 发车、次日 01:00 到达：不能被「当天 22:00 前到」误放行
    overnight = train_factory("Z1", "AAA", "DDD", "20:00", "01:00", "05:00")
    before_22 = args_factory("--arrive-before", "22:00")
    assert not rt._arrival_ok(overnight, before_22, "2026-10-20")
    after_18 = args_factory("--arrive-after", "18:00")
    assert rt._arrival_ok(overnight, after_18, "2026-10-20")
