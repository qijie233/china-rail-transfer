import json

from scripts import rail_transfer as rt


def make_inter(tmp_path):
    return rt.InterchangeTable(tmp_path / "missing-interchange.json")


def make_buffers(tmp_path, data=None):
    if data is None:
        return rt.TransferBufferTable(tmp_path / "missing-buffers.json")
    path = tmp_path / "buffers.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return rt.TransferBufferTable(path)


def test_two_leg_plan(table, train_factory, fake_engine_cls, tmp_path):
    t1 = train_factory("G1", "AAA", "BBB", "08:00", "10:00", "02:00")
    t2 = train_factory("G2", "BBB", "DDD", "10:30", "12:00", "01:30")
    engine = fake_engine_cls({("AAA", "BBB"): [t1], ("BBB", "DDD"): [t2]})
    plans = rt.build_plans(engine, ["AAA"], ["DDD"], [("乙", ["BBB"])],
                           "2026-10-20", table, make_inter(tmp_path),
                           make_buffers(tmp_path), 16, 5, True, False)
    assert len(plans) == 1
    plan = plans[0]
    assert [leg.code for leg in plan.legs] == ["G1", "G2"]
    assert plan.transfers[0].gap_min == 30
    assert plan.transfers[0].required_min == 16
    assert not plan.near_miss


def test_near_miss_is_separate(table, train_factory, fake_engine_cls, tmp_path):
    t1 = train_factory("G1", "AAA", "BBB", "08:00", "10:00", "02:00")
    t2 = train_factory("G2", "BBB", "DDD", "10:10", "11:40", "01:30")
    engine = fake_engine_cls({("AAA", "BBB"): [t1], ("BBB", "DDD"): [t2]})
    kwargs = dict(from_codes=["AAA"], to_codes=["DDD"],
                  hub_groups=[("乙", ["BBB"])], date_str="2026-10-20",
                  table=table, inter=make_inter(tmp_path),
                  buffers=make_buffers(tmp_path), min_transfer=16,
                  cross_buffer=5, allow_cross_station=True,
                  allow_overnight=False)
    assert rt.build_plans(engine, **kwargs) == []
    near = rt.build_plans(engine, near_miss=10, **kwargs)
    assert len(near) == 1 and near[0].near_miss
    assert near[0].transfers[0].near_miss


def test_station_buffer_raises_floor(table, train_factory, fake_engine_cls, tmp_path):
    t1 = train_factory("G1", "AAA", "BBB", "08:00", "10:00", "02:00")
    t2 = train_factory("G2", "BBB", "DDD", "10:19", "11:50", "01:30")
    engine = fake_engine_cls({("AAA", "BBB"): [t1], ("BBB", "DDD"): [t2]})
    buffers = make_buffers(tmp_path, {"_default": 16, "stations": {"乙站": 20}})
    plans = rt.build_plans(engine, ["AAA"], ["DDD"], [("乙", ["BBB"])],
                           "2026-10-20", table, make_inter(tmp_path), buffers,
                           16, 5, True, False)
    assert plans == []

    t3 = train_factory("G3", "BBB", "DDD", "10:20", "11:50", "01:30")
    engine = fake_engine_cls({("AAA", "BBB"): [t1], ("BBB", "DDD"): [t3]})
    plans = rt.build_plans(engine, ["AAA"], ["DDD"], [("乙", ["BBB"])],
                           "2026-10-20", table, make_inter(tmp_path), buffers,
                           16, 5, True, False)
    assert len(plans) == 1 and plans[0].transfers[0].required_min == 20


def test_cross_station_commute(table, train_factory, fake_engine_cls, tmp_path):
    t1 = train_factory("G1", "AAA", "BBB", "08:00", "10:00", "02:00")
    t2 = train_factory("G2", "BBD", "DDD", "11:30", "13:00", "01:30")
    engine = fake_engine_cls({("AAA", "BBB"): [t1], ("BBB", "DDD"): [t2]})
    plans = rt.build_plans(engine, ["AAA"], ["DDD"], [("乙", ["BBB", "BBD"])],
                           "2026-10-20", table, make_inter(tmp_path),
                           make_buffers(tmp_path), 16, 5, True, False)
    assert len(plans) == 1
    tr = plans[0].transfers[0]
    assert not tr.same_station
    assert tr.interstation_min == 60 and tr.required_min == 81
    assert tr.interstation_estimated


def test_cross_station_can_be_forbidden(table, train_factory, fake_engine_cls, tmp_path):
    t1 = train_factory("G1", "AAA", "BBB", "08:00", "10:00", "02:00")
    t2 = train_factory("G2", "BBD", "DDD", "11:30", "13:00", "01:30")
    engine = fake_engine_cls({("AAA", "BBB"): [t1], ("BBB", "DDD"): [t2]})
    plans = rt.build_plans(engine, ["AAA"], ["DDD"], [("乙", ["BBB", "BBD"])],
                           "2026-10-20", table, make_inter(tmp_path),
                           make_buffers(tmp_path), 16, 5, False, False)
    assert plans == []


def test_same_train_is_not_a_transfer(table, train_factory, fake_engine_cls, tmp_path):
    t1 = train_factory("G1", "AAA", "BBB", "08:00", "10:00", "02:00")
    engine = fake_engine_cls({("AAA", "BBB"): [t1], ("BBB", "DDD"): [t1]})
    plans = rt.build_plans(engine, ["AAA"], ["DDD"], [("乙", ["BBB"])],
                           "2026-10-20", table, make_inter(tmp_path),
                           make_buffers(tmp_path), 16, 5, True, False)
    assert plans == []


def test_three_leg_chain(table, train_factory, fake_engine_cls, tmp_path):
    a = train_factory("G1", "AAA", "BBB", "08:00", "10:00", "02:00")
    b = train_factory("G2", "BBB", "CCC", "10:30", "12:00", "01:30")
    c = train_factory("G3", "CCC", "DDD", "12:30", "14:00", "01:30")
    engine = fake_engine_cls({("AAA", "BBB"): [a],
                              ("BBB", "CCC"): [b],
                              ("CCC", "DDD"): [c]})
    plans = rt.build_chain_plans(
        engine, ["AAA"], ["DDD"], [("乙", ["BBB"]), ("丙", ["CCC"])],
        "2026-10-20", table, make_inter(tmp_path), make_buffers(tmp_path),
        16, 5, True, False)
    assert len(plans) == 1
    plan = plans[0]
    assert [leg.code for leg in plan.legs] == ["G1", "G2", "G3"]
    assert [tr.gap_min for tr in plan.transfers] == [30, 30]
    assert plan.transfers[0].hub_city == "乙"
    assert plan.transfers[1].hub_city == "丙"


def test_dedupe_and_rank_sorts(table, train_factory):
    t1 = train_factory("G1", "AAA", "BBB", "08:00", "10:00", "02:00")
    t2 = train_factory("G2", "BBB", "DDD", "10:30", "12:00", "01:30")
    t3 = train_factory("G3", "BBB", "DDD", "10:45", "12:15", "01:30")
    def plan(leg1, leg2, gap):
        return rt.Plan(legs=[leg1, leg2], transfers=[rt.Transfer(
            "乙", leg1.to_code, leg2.from_code, True, gap, 16)])
    p1 = plan(t1, t2, 30)
    p2 = plan(t1, t3, 45)
    ranked = rt.dedupe_and_rank([p2, p1], limit=10, sort_key="depart")
    assert [p.legs[1].code for p in ranked] == ["G2", "G3"]
    ranked = rt.dedupe_and_rank([p2, p1], limit=1, sort_key="gap")
    assert ranked[0].leg2.code == "G2"
