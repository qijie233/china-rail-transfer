import io

from scripts import rail_transfer as rt


def _rows(row_factory, date):
    return {
        ("AAA", "DDD", date): [],
        ("AAA", "BBB", date): [row_factory(
            "G1", "甲站", "乙站", "AAA", "BBB", "08:00", "10:00", "02:00",
            ze="有", train_no="T1")],
        ("BBB", "DDD", date): [row_factory(
            "G2", "乙站", "丁站", "BBB", "DDD", "10:30", "12:00", "01:30",
            ze="有", train_no="T2")],
    }


def _patch_browser(monkeypatch, tmp_path, session):
    monkeypatch.setattr(rt, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(rt, "find_browser", lambda explicit=None: "fake-browser")
    monkeypatch.setattr(rt, "BrowserSession", lambda *a, **kw: session)


def test_cmd_query_end_to_end(args_factory, table, row_factory,
                              fake_session_cls, monkeypatch, tmp_path):
    session = fake_session_cls(_rows(row_factory, "2026-10-20"))
    _patch_browser(monkeypatch, tmp_path, session)
    out = rt.cmd_query(args_factory("--via", "乙站"), table)
    assert out.status == "success"
    assert len(out.data["plans"]) == 1
    assert out.data["plans"][0]["legs"][0]["code"] == "G1"
    assert out.evidence


def test_cmd_query_dates_scan(args_factory, table, row_factory,
                              fake_session_cls, monkeypatch, tmp_path):
    rows = _rows(row_factory, "2026-10-20")
    rows.update(_rows(row_factory, "2026-10-21"))
    session = fake_session_cls(rows)
    _patch_browser(monkeypatch, tmp_path, session)
    args = args_factory("--via", "乙站", "--dates", "2026-10-20..2026-10-21")
    out = rt.cmd_query(args, table)
    assert out.data["multi"] is True
    assert out.data["dates"] == ["2026-10-20", "2026-10-21"]
    assert len(out.data["results"]) == 2
    assert out.status == "success"


def test_cmd_query_dates_respects_max_days(args_factory, table, monkeypatch, tmp_path):
    _patch_browser(monkeypatch, tmp_path, None)
    args = args_factory("--dates", "2026-10-20..2026-10-22", "--max-days", "2")
    out = rt.cmd_query(args, table)
    assert out.status == "blocked"
    assert "--max-days" in out.summary


def test_cmd_query_reports_failure_when_no_browser(args_factory, table, monkeypatch):
    monkeypatch.setattr(rt, "find_browser", lambda explicit=None: None)
    out = rt.cmd_query(args_factory("--via", "乙站"), table)
    assert out.status == "failure"
    assert out.failure_category == rt.FC_ENV


def test_cmd_watch_notifies_only_once(args_factory, table, row_factory,
                                      fake_session_cls, monkeypatch, tmp_path):
    session = fake_session_cls(_rows(row_factory, "2026-10-20"))
    _patch_browser(monkeypatch, tmp_path, session)
    notified = []
    monkeypatch.setattr(rt, "_notify",
                        lambda title, message, bell=True: notified.append((title, message)))
    args = args_factory("--via", "乙站", "--watch",
                        "--interval", "60", "--watch-cycles", "2")
    out = rt.cmd_watch(args, table)
    assert out.status == "success"
    assert out.data["cycles"] == 2
    assert out.data["notified"] == 1
    assert len(notified) == 1
    assert "12306" in notified[0][0]


def test_cmd_watch_json_lines(args_factory, table, row_factory,
                              fake_session_cls, monkeypatch, tmp_path, capsys):
    session = fake_session_cls(_rows(row_factory, "2026-10-20"))
    _patch_browser(monkeypatch, tmp_path, session)
    monkeypatch.setattr(rt, "_notify", lambda *a, **kw: None)
    args = args_factory("--via", "乙站", "--watch", "--interval", "60",
                        "--watch-cycles", "1", "--format", "json")
    out = rt.cmd_watch(args, table)
    assert out.status == "success"
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines and lines[0].startswith("{")
    assert '"cycle": 1' in lines[0]


def test_cmd_query_collapses_exceptions(args_factory, table,
                                        fake_session_cls, monkeypatch, tmp_path):
    session = fake_session_cls({})
    _patch_browser(monkeypatch, tmp_path, session)
    monkeypatch.setattr(
        rt, "_execute_query",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    out = rt.cmd_query(args_factory("--via", "乙站"), table)
    assert out.status == "failure"
    assert out.failure_category == rt.FC_CONN
    assert "boom" in out.summary


def test_cmd_query_ask_transfer(args_factory, table, row_factory,
                                fake_session_cls, monkeypatch, tmp_path):
    session = fake_session_cls(_rows(row_factory, "2026-10-20"))
    _patch_browser(monkeypatch, tmp_path, session)
    monkeypatch.setattr("sys.stdin", io.StringIO("25" + chr(10)))
    out = rt.cmd_query(args_factory("--via", "乙站", "--ask-transfer"), table)
    assert out.status == "success"
    assert out.data["min_transfer"] == 25
    assert out.data["plans"][0]["transfers"][0]["required_min"] == 25
