import io

from scripts import rail_transfer as rt


def test_parser_defaults():
    args = rt.build_parser().parse_args(["--from", "甲", "--to", "丁"])
    assert args.legs == 2
    assert args.sort == "total"
    assert args.near_miss == 0
    assert args.max_queries == 0
    assert args.price_cache_ttl == 86400
    assert args.watch is False
    assert args.interval == 180
    assert args.show_departed is False
    assert args.no_standing is False


def test_parser_accepts_new_features():
    args = rt.build_parser().parse_args([
        "--from", "甲", "--to", "丁",
        "--dates", "2026-10-20..2026-10-22",
        "--seat-class", "二等座",
        "--depart-after", "08:00", "--depart-before", "20:00",
        "--arrive-before", "23:00", "--max-wait", "90",
        "--near-miss", "5", "--sort", "arrive",
        "--avoid", "北京", "--max-queries", "40",
        "--legs", "3", "--via", "乙,丙",
        "--price-cache-ttl", "7200",
        "--watch", "--interval", "120", "--watch-cycles", "3",
    ])
    assert args.dates == "2026-10-20..2026-10-22"
    assert args.seat_class == "二等座"
    assert args.max_wait == 90
    assert args.sort == "arrive"
    assert args.max_queries == 40
    assert args.legs == 3
    assert args.watch is True


def test_prepare_query_rejects_bad_seat(args_factory, table):
    args = args_factory("--seat-class", "火箭座")
    setup, err = rt._prepare_query(args, table)
    assert setup is None and err is not None
    assert "无法识别的席别" in err.summary


def test_prepare_query_rejects_bad_time(args_factory, table):
    args = args_factory("--depart-after", "25:00")
    setup, err = rt._prepare_query(args, table)
    assert setup is None and err is not None
    assert "时间格式不对" in err.summary


def test_prepare_query_requires_two_via_for_three_legs(args_factory, table):
    args = args_factory("--legs", "3")
    setup, err = rt._prepare_query(args, table)
    assert setup is None and err is not None
    assert "--legs 3" in err.summary

    args = args_factory("--legs", "3", "--via", "乙站")
    setup, err = rt._prepare_query(args, table)
    assert setup is None and err.status == "blocked"


def test_prepare_query_warns_price_sort_without_price(args_factory, table):
    args = args_factory("--sort", "price")
    setup, err = rt._prepare_query(args, table)
    assert err is None and setup is not None
    assert any("--with-price" in w for w in setup["warnings"])


def test_prepare_query_ignore_station_buffers(args_factory, table):
    args = args_factory("--min-transfer", "10", "--ignore-station-buffers")
    setup, err = rt._prepare_query(args, table)
    assert err is None and setup is not None
    assert setup["buffers"].default == 0
    assert setup["buffers"].stations == {}


def test_watch_rejects_short_interval(args_factory, table):
    args = args_factory("--watch", "--interval", "10")
    out = rt.cmd_watch(args, table)
    assert out.status == "failure"
    assert "不能小于" in out.summary


def test_watch_rejects_dates(args_factory, table):
    args = args_factory("--watch", "--dates", "2026-10-20..2026-10-21")
    out = rt.cmd_watch(args, table)
    assert out.status == "failure"
    assert "--watch" in out.summary


def test_script_ref_finds_reference_files():
    for name in ("hubs.json", "interchange-times.json",
                 "transfer-buffers.json"):
        path = rt._script_ref(name)
        assert path.is_file(), path


def test_prepare_query_rejects_negative_numbers(args_factory, table):
    args = args_factory("--min-transfer", "-1")
    setup, err = rt._prepare_query(args, table)
    assert setup is None and err is not None
    assert "不能是负数" in err.summary

    args = args_factory("--max-days", "0")
    setup, err = rt._prepare_query(args, table)
    assert setup is None and "至少为 1" in err.summary


def test_ask_transfer_reads_value(args_factory, monkeypatch):
    args = args_factory("--ask-transfer")
    monkeypatch.setattr("sys.stdin", io.StringIO("25" + chr(10)))
    rt._ask_transfer_minutes(args)
    assert args.min_transfer == 25


def test_ask_transfer_empty_uses_default(args_factory, monkeypatch):
    args = args_factory("--ask-transfer", "--min-transfer", "30")
    monkeypatch.setattr("sys.stdin", io.StringIO(chr(10)))
    rt._ask_transfer_minutes(args)
    assert args.min_transfer == 30


def test_ask_transfer_retries_invalid_input(args_factory, monkeypatch):
    args = args_factory("--ask-transfer")
    monkeypatch.setattr("sys.stdin", io.StringIO("abc" + chr(10) + "-1" + chr(10) + "20" + chr(10)))
    rt._ask_transfer_minutes(args)
    assert args.min_transfer == 20


def test_ask_transfer_eof_uses_default(args_factory, monkeypatch):
    args = args_factory("--ask-transfer")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rt._ask_transfer_minutes(args)
    assert args.min_transfer == 16
