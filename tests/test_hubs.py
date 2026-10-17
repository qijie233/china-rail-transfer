import json

from scripts import rail_transfer as rt


def test_pick_hubs_short_route_prefers_nearby(table):
    # 甲 0100、乙 0200、丙 0300、丁 0400；短途应优先两端附近的枢纽
    hubs = ["丙站", "乙站", "丁站", "甲站"]
    picked = rt._pick_hubs_geographically(table, hubs, ["AAA"], ["DDD"], 3)
    assert "甲站" not in picked and "丁站" not in picked
    assert picked[:2] == ["乙站", "丙站"]


def test_pick_hubs_excludes_start_and_end_city(table):
    picked = rt._pick_hubs_geographically(table, ["甲站", "乙站"], ["AAA"], ["DDD"], 5)
    assert "甲站" not in picked and "丁站" not in picked


def test_build_hub_groups_via_city_expands_siblings(table, tmp_path):
    hubs = tmp_path / "hubs.json"
    hubs.write_text(json.dumps({"hubs": ["乙站"]}, ensure_ascii=False), encoding="utf-8")
    groups, warnings = rt.build_hub_groups(table, ["AAA"], ["DDD"], ["乙"], hubs, 8)
    assert groups == [("乙", ["BBB", "BBD"])]
    assert warnings == []


def test_build_hub_groups_via_station_uses_one_station(table, tmp_path):
    hubs = tmp_path / "hubs.json"
    hubs.write_text(json.dumps({"hubs": ["乙站"]}, ensure_ascii=False), encoding="utf-8")
    groups, _ = rt.build_hub_groups(table, ["AAA"], ["DDD"], ["乙站"], hubs, 8)
    assert groups == [("乙", ["BBB"])]


def test_build_hub_groups_avoid_and_same_city(table, tmp_path):
    hubs = tmp_path / "hubs.json"
    hubs.write_text(json.dumps({"hubs": ["乙站"]}, ensure_ascii=False), encoding="utf-8")

    groups, warnings = rt.build_hub_groups(
        table, ["AAA"], ["DDD"], ["乙站"], hubs, 8, avoid=["乙"])
    assert groups == []
    assert any("--avoid" in w for w in warnings)

    groups, warnings = rt.build_hub_groups(
        table, ["AAA"], ["DDD"], ["甲站"], hubs, 8)
    assert groups == []
    assert any("出发/到达城市相同" in w for w in warnings)


def test_build_chain_groups_merges_same_city(table):
    groups, warnings = rt.build_chain_groups(
        table, ["AAA"], ["DDD"], ["乙站", "乙东"])
    assert len(groups) == 1
    assert groups[0][0] == "乙"
    assert sorted(groups[0][1]) == ["BBB", "BBD"]
    assert any("已合并" in w for w in warnings)


def test_build_chain_groups_honours_avoid(table):
    groups, warnings = rt.build_chain_groups(
        table, ["AAA"], ["DDD"], ["乙站", "丙站"], avoid=["乙"])
    assert [g[0] for g in groups] == ["丙"]
    assert any("--avoid" in w for w in warnings)


def test_build_hub_groups_auto_honours_avoid(table, tmp_path):
    hubs = tmp_path / "hubs.json"
    hubs.write_text(json.dumps({"hubs": ["乙站"]}, ensure_ascii=False), encoding="utf-8")

    groups, _ = rt.build_hub_groups(table, ["AAA"], ["DDD"], None, hubs, 8)
    assert groups and groups[0][0] == "乙"

    groups, warnings = rt.build_hub_groups(table, ["AAA"], ["DDD"], None, hubs, 8,
                                           avoid=["乙"])
    assert groups == []
    assert not warnings  # 自动模式是静默排除，不刷警告
