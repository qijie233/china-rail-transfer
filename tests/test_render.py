from scripts import rail_transfer as rt


def _plan(train_factory):
    t1 = train_factory("G1", "AAA", "BBB", "08:00", "10:00", "02:00",
                       prices={"二等座": "¥100.0"})
    t2 = train_factory("G2", "BBB", "DDD", "10:30", "12:00", "01:30",
                       prices={"二等座": "¥80.0"})
    return rt.Plan(legs=[t1, t2], transfers=[
        rt.Transfer("乙", "BBB", "BBB", True, 30, 16)])


def _res(plan, table, seat_classes=None):
    pj = rt._plan_json(plan, table, seat_classes)
    return {
        "from": "甲站", "to": "丁站", "date": "2026-10-20",
        "min_transfer": 16, "cross_station_buffer": 5, "queries": 3,
        "sort": "total", "direct": [], "plans": [pj], "near_miss_plans": [],
        "warnings": [], "errors": [], "status": "success", "summary": "ok",
        "filters": {"seat_classes": sorted(seat_classes or [])},
        "chain": False, "budget": {"limit": 0, "used": 3, "exhausted": False},
    }


def test_plan_json_keeps_legacy_fields(table, train_factory):
    pj = rt._plan_json(_plan(train_factory), table, {"二等座"})
    assert pj["legs"][0]["code"] == "G1"
    assert pj["legs"][1]["code"] == "G2"
    assert pj["leg1"]["code"] == "G1"
    assert pj["leg2"]["code"] == "G2"
    assert pj["hub_arrive"] == "乙站"
    assert pj["gap_min"] == 30 and pj["required_min"] == 16
    assert pj["min_total"] == "¥180.0"
    assert pj["total_price"] == "¥180.0"
    assert pj["total_price_note"] == "指定席别"
    assert pj["total_price_detail"] == [
        {"code": "G1", "seat": "二等座", "price": "¥100.0"},
        {"code": "G2", "seat": "二等座", "price": "¥80.0"},
    ]
    assert pj["legs"][0]["selected_seat"] == "二等座"
    assert pj["legs"][0]["selected_price"] == "¥100.0"
    assert pj["near_miss"] is False


def test_render_text_shows_buffer_and_price(table, train_factory):
    text = rt.render_text(_res(_plan(train_factory), table, {"二等座"}), table)
    assert "缓冲 30 分钟（要求 ≥16）✓" in text
    assert "合计 ¥180.0" in text
    assert "双程合计 ¥180.0（二等座 ¥100.0 + 二等座 ¥80.0）" in text


def test_render_text_shows_near_miss(table, train_factory):
    plan = _plan(train_factory)
    plan.near_miss = True
    plan.transfers[0].near_miss = True
    plan.transfers[0].gap_min = 12
    res = _res(plan, table)
    res["plans"] = []
    res["near_miss_plans"] = [rt._plan_json(plan, table)]
    text = rt.render_text(res, table)
    assert "差一点点" in text
    assert "最紧处差 4 分钟" in text


def test_render_dates_text(table, train_factory):
    res = _res(_plan(train_factory), table)
    multi = {
        "from": "甲站", "to": "丁站", "dates": ["2026-10-20"],
        "multi": True, "results": [res], "warnings": [],
        "status": "success", "summary": "ok",
    }
    text = rt.render_dates_text(multi, table)
    assert "多日扫描" in text
    assert "2026-10-20" in text
    assert "G1 08:00" in text


def test_collect_watch_matches_all_legs_must_have_seats():
    res = {
        "direct": [{"code": "G9", "from_name": "甲站", "to_name": "丁站",
                    "dep": "08:00", "arr": "12:00", "date": "2026-10-20",
                    "seats": "二等有", "has_seat": True}],
        "plans": [{
            "legs": [
                {"code": "G1", "from_name": "甲站", "to_name": "乙站",
                 "dep": "08:00", "arr": "10:00", "date": "2026-10-20",
                 "seats": "二等有", "has_seat": True},
                {"code": "G2", "from_name": "乙站", "to_name": "丁站",
                 "dep": "10:30", "arr": "12:00", "date": "2026-10-20",
                 "seats": "二等无", "has_seat": False},
            ],
            "total": "4小时",
        }],
    }
    matches = rt._collect_watch_matches(res, set())
    assert [m["code"] for m in matches] == ["G9"]

    matches = rt._collect_watch_matches(res, {"G2"})
    assert matches == []


def test_collect_watch_matches_filters_codes():
    res = {
        "direct": [{"code": "G9", "from_name": "甲站", "to_name": "丁站",
                    "dep": "08:00", "arr": "12:00", "date": "2026-10-20",
                    "seats": "二等有", "has_seat": True}],
        "plans": [],
    }
    assert rt._collect_watch_matches(res, {"G8"}) == []
    assert len(rt._collect_watch_matches(res, {"g9"})) == 1
