from scripts import rail_transfer as rt


def test_parse_rows_maps_every_field(row_factory):
    payload = {"rows": [row_factory(
        "G1224", "北京南", "上海虹桥", "AAA", "DDD", "08:00", "12:30", "04:30",
        wz="12", rw="", yw="", yz="", ze="有", zy="1", swz="无",
        train_no="5l000G12240G", from_no="01", to_no="12", seat_types="O9M",
    )]}
    trains = rt._parse_rows(payload, "AAA", "DDD", "2026-10-20")
    assert len(trains) == 1
    t = trains[0]
    assert t.code == "G1224"
    assert t.start_code == "北京南" and t.end_code == "上海虹桥"
    assert t.from_code == "AAA" and t.to_code == "DDD"
    assert t.dep == "08:00" and t.arr == "12:30" and t.lishi == "04:30"
    assert t.wz == "12" and t.ze == "有" and t.zy == "1" and t.swz == "无"
    assert t.train_no == "5l000G12240G"
    assert t.from_station_no == "01" and t.to_station_no == "12"
    assert t.seat_types == "O9M"


def test_parse_rows_skips_incomplete_rows(row_factory):
    assert rt._parse_rows({"rows": [["G1"]]}, "AAA", "DDD", "2026-10-20") == []
    assert rt._parse_rows({"rows": [row_factory(
        "", "甲", "丁", "AAA", "DDD", "08:00", "09:00", "01:00")]},
        "AAA", "DDD", "2026-10-20") == []


def test_seat_text_hides_standing(row_factory):
    t = rt._parse_rows({"rows": [row_factory(
        "K1", "甲", "丁", "AAA", "DDD", "08:00", "09:00", "01:00",
        wz="12", yz="有", ze="")]}, "AAA", "DDD", "2026-10-20")[0]
    assert "无座12" in t.seats_text()
    assert "无座" not in t.seats_text(show_standing=False)


def test_has_class_and_price_for_classes(train_factory):
    t = train_factory("G1", "AAA", "DDD", "08:00", "09:00", "01:00", ze="有", yw="")
    assert t.has_class({"二等座"})
    assert not t.has_class({"硬卧"})
    assert t.has_class(set())

    t.prices = {"二等座": "¥100.0", "一等座": "¥160.0"}
    assert t.price_for_classes({"二等座"}) == 100.0
    assert t.price_for_classes({"商务座"}) >= 1e9
    assert t.price_for_classes(set()) == 100.0
