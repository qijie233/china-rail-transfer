from scripts import rail_transfer as rt


def _setup(args, table):
    setup, err = rt._prepare_query(args, table)
    assert err is None
    return setup


def _run(args, table, session, cache, budget_limit=0):
    setup = _setup(args, table)
    return rt._execute_query(args, table, setup, args.date, session, cache,
                             rt.RequestBudget(budget_limit))


def _simple_rows(row_factory, date="2026-10-20"):
    return {
        ("AAA", "DDD", date): [],
        ("AAA", "BBB", date): [row_factory(
            "G1", "甲站", "乙站", "AAA", "BBB", "08:00", "10:00", "02:00",
            ze="有", train_no="T1")],
        ("BBB", "DDD", date): [row_factory(
            "G2", "乙站", "丁站", "BBB", "DDD", "10:30", "12:00", "01:30",
            ze="有", train_no="T2")],
    }


def test_execute_query_builds_plan(args_factory, table, disabled_cache,
                                   row_factory, fake_session_cls):
    args = args_factory("--via", "乙站")
    session = fake_session_cls(_simple_rows(row_factory))
    res, engine = _run(args, table, session, disabled_cache)
    assert res["status"] == "success"
    assert len(res["plans"]) == 1
    plan = res["plans"][0]
    assert [leg["code"] for leg in plan["legs"]] == ["G1", "G2"]
    assert plan["transfers"][0]["gap_min"] == 30
    assert plan["leg1"]["code"] == "G1"  # 向后兼容字段
    assert engine.calls >= 3


def test_execute_query_with_price(args_factory, table, disabled_cache,
                                  row_factory, fake_session_cls):
    args = args_factory("--via", "乙站", "--with-price")
    prices = {"G1": {"O": "¥100.0"}, "G2": {"O": "¥80.0"}}
    session = fake_session_cls(_simple_rows(row_factory), prices)
    res, _ = _run(args, table, session, disabled_cache)
    assert res["plans"][0]["min_total"] == "¥180.0"
    assert res["plans"][0]["legs"][0]["prices"] == {"二等座": "¥100.0"}
    assert res["price_calls"] == 2


def test_execute_query_seat_filter(args_factory, table, disabled_cache,
                                   row_factory, fake_session_cls):
    args = args_factory("--via", "乙站", "--seat-class", "二等座")
    rows = _simple_rows(row_factory)
    rows[("BBB", "DDD", "2026-10-20")][0][12] = "无"  # 二等座无票
    res, _ = _run(args, table, fake_session_cls(rows), disabled_cache)
    assert res["plans"] == []


def test_execute_query_time_filter(args_factory, table, disabled_cache,
                                   row_factory, fake_session_cls):
    args = args_factory("--via", "乙站", "--depart-after", "09:00")
    res, _ = _run(args, table, fake_session_cls(_simple_rows(row_factory)),
                  disabled_cache)
    assert res["plans"] == []


def test_execute_query_max_wait(args_factory, table, disabled_cache,
                                row_factory, fake_session_cls):
    args = args_factory("--via", "乙站", "--max-wait", "20")
    res, _ = _run(args, table, fake_session_cls(_simple_rows(row_factory)),
                  disabled_cache)
    assert res["plans"] == []


def test_execute_query_near_miss(args_factory, table, disabled_cache,
                                 row_factory, fake_session_cls):
    rows = _simple_rows(row_factory)
    rows[("BBB", "DDD", "2026-10-20")][0][5] = "10:10"  # 只差 10 分钟
    args = args_factory("--via", "乙站", "--near-miss", "10")
    res, _ = _run(args, table, fake_session_cls(rows), disabled_cache)
    assert res["plans"] == []
    assert len(res["near_miss_plans"]) == 1
    assert res["near_miss_plans"][0]["near_miss"] is True
    assert res["status"] == "partial_success"


def test_execute_query_budget(args_factory, table, disabled_cache,
                              row_factory, fake_session_cls):
    args = args_factory("--via", "乙站")
    res, engine = _run(args, table, fake_session_cls(_simple_rows(row_factory)),
                       disabled_cache, budget_limit=1)
    assert res["budget"]["exhausted"] is True
    assert engine.calls == 1
    assert res["status"] == "partial_success"


def test_execute_query_three_legs(args_factory, table, disabled_cache,
                                  row_factory, fake_session_cls):
    rows = _simple_rows(row_factory)
    rows[("BBB", "CCC", "2026-10-20")] = [row_factory(
        "G2", "乙站", "丙站", "BBB", "CCC", "10:30", "12:00", "01:30",
        ze="有", train_no="T2")]
    rows[("CCC", "DDD", "2026-10-20")] = [row_factory(
        "G3", "丙站", "丁站", "CCC", "DDD", "12:30", "14:00", "01:30",
        ze="有", train_no="T3")]
    args = args_factory("--legs", "3", "--via", "乙站,丙站")
    res, _ = _run(args, table, fake_session_cls(rows), disabled_cache)
    assert res["chain"] is True
    assert len(res["plans"]) == 1
    assert [leg["code"] for leg in res["plans"][0]["legs"]] == ["G1", "G2", "G3"]


def test_execute_query_direct_only(args_factory, table, disabled_cache,
                                   row_factory, fake_session_cls):
    rows = {("AAA", "DDD", "2026-10-20"): [row_factory(
        "G9", "甲站", "丁站", "AAA", "DDD", "08:00", "12:00", "04:00",
        ze="有", train_no="T9")]}
    args = args_factory()  # 自动枢纽在合成站点表里找不到，只返回直达
    res, _ = _run(args, table, fake_session_cls(rows), disabled_cache)
    assert res["status"] == "success"
    assert [t["code"] for t in res["direct"]] == ["G9"]
    assert res["plans"] == []


def test_execute_query_sort_by_price(args_factory, table, disabled_cache,
                                     row_factory, fake_session_cls):
    rows = {
        ("AAA", "DDD", "2026-10-20"): [],
        ("AAA", "BBB", "2026-10-20"): [row_factory(
            "G1", "甲站", "乙站", "AAA", "BBB", "08:00", "10:00", "02:00",
            ze="有", train_no="T1")],
        ("BBB", "DDD", "2026-10-20"): [
            row_factory("G2", "乙站", "丁站", "BBB", "DDD", "10:30", "12:00",
                        "01:30", ze="有", train_no="T2"),
            row_factory("G3", "乙站", "丁站", "BBB", "DDD", "10:20", "11:50",
                        "01:30", ze="有", train_no="T3"),
        ],
    }
    prices = {"G1": {"O": "¥100.0"}, "G2": {"O": "¥80.0"}, "G3": {"O": "¥200.0"}}
    args = args_factory("--via", "乙站", "--with-price", "--sort", "price")
    res, _ = _run(args, table, fake_session_cls(rows, prices), disabled_cache)
    assert len(res["plans"]) == 2
    assert res["plans"][0]["legs"][1]["code"] == "G2"
    assert res["plans"][0]["min_total"] == "¥180.0"


def test_execute_query_price_limit_zero_skips(args_factory, table, disabled_cache,
                                              row_factory, fake_session_cls):
    args = args_factory("--via", "乙站", "--with-price", "--price-limit", "0")
    session = fake_session_cls(_simple_rows(row_factory))
    res, _ = _run(args, table, session, disabled_cache)
    assert res["price_calls"] == 0
    assert session.price_queries == []
    assert any("跳过票价" in w for w in res["warnings"])


def test_price_disk_cache_avoids_repeat(args_factory, table, row_factory,
                                        fake_session_cls, tmp_path):
    cache = rt.QueryCache(tmp_path / "cache.json", ttl=600)
    prices = {"G1": {"O": "¥100.0"}, "G2": {"O": "¥80.0"}}
    session = fake_session_cls(_simple_rows(row_factory), prices)
    args = args_factory("--via", "乙站", "--with-price")
    setup, err = rt._prepare_query(args, table)
    assert err is None

    first, _ = rt._execute_query(args, table, setup, args.date, session, cache,
                                 rt.RequestBudget(0))
    sent_after_first = len(session.price_queries)
    assert sent_after_first == 2

    second, _ = rt._execute_query(args, table, setup, args.date, session, cache,
                                  rt.RequestBudget(0))
    assert len(session.price_queries) == sent_after_first
    assert second["plans"][0]["min_total"] == first["plans"][0]["min_total"]
