#!/usr/bin/env python
"""火车票中转换乘查询工具。

这个脚本为 `rail-transfer` skill 提供可重复调用的执行入口，支持：

- 解析 12306 站点表，把「站名 / 城市名 / 拼音」解析成电报码
- 通过无头浏览器（Edge + CDP）在真实页面上下文里查询 12306 余票接口
- 先查直达；无直达或需要更多选择时，自行组合两段车次计算换乘方案
- 强制最小换乘缓冲（默认 16 分钟），避免赶不上第二程
- 同城异站换乘时，额外计入两站之间的通勤时间
- 输出人读报告或 JSON 结果

关键实现约束（2026-10 实测，改动前务必先读）：

1. 余票接口 `leftTicket/queryG` **必须从真实浏览器页面上下文发起**。
   curl 或裸请求一律被 302 到 `error.html`；伪造 `RAIL_DEVICEID`、补
   `X-Requested-With`、改用有头模式均无效。
2. 接口参数必须是 **`purpose_codes=ADULT`**。用旧的
   `purpose_corporation=0` 会被静默 302，且不报任何错误。
3. 站点表 `station_name.js` 是静态资源，不受风控影响，可以直接用 urllib 拉。
4. 官方中转接口 `lcquery/queryG` 目前恒返回「系统忙」，不可用，
   因此换乘方案一律由本脚本自行组合。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from typing import Any

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# --- 复用 shared/ 中的既有约定 -------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILLS_DIR = _SCRIPT_DIR.parent.parent
for _candidate in [_SKILLS_DIR / "shared", _SKILLS_DIR.parent / "shared"]:
    if (_candidate / "tool_config.py").exists():
        sys.path.insert(0, str(_candidate))
        break

try:
    from tool_config import get_tool_path, set_tool_path
except ImportError:  # shared/ 不可用时的兜底
    def get_tool_path(tool_name: str, workspace: Any = None) -> str | None:  # type: ignore
        return None

    def set_tool_path(tool_name: str, tool_path: str, workspace: Any = None,
                      global_: bool = False) -> Path:  # type: ignore
        raise RuntimeError("shared/tool_config.py 不可用，无法写入工具路径配置")


# --- 常量 -----------------------------------------------------------------
STATION_TABLE_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
API_BASE = "https://kyfw.12306.cn"
INIT_PAGE = "https://kyfw.12306.cn/otn/leftTicket/init"

STATION_CACHE_TTL_DAYS = 7
BROWSER_TOOL_KEY = "edge_browser"

DEFAULT_MIN_TRANSFER = 16          # 主人的硬要求：换乘缓冲至少 16 分钟
DEFAULT_CROSS_BUFFER = 5           # 跨站额外缓冲
DEFAULT_AUTO_HUBS = 16            # 自动模式枢纽数上限。实测 12306 会在累计请求到一百多次
                                  # 后开始拒绝（约 20% Failed to fetch），所以默认只挑
                                  # 地理就近的十几个；要更全可以调大，代价是可能被限流。
DEFAULT_CONCURRENCY = 2           # 并发越高越容易触发限流，默认保守
DEFAULT_CACHE_TTL = 600           # 查询结果磁盘缓存秒数：反复微调参数时不必重复打 12306
DEFAULT_PRICE_LIMIT = 8           # --with-price 时最多为多少趟车补票价（每趟一次请求）
DEFAULT_PRICE_CACHE_TTL = 86400   # 公布票价基本稳定，单独缓存一天，避免反复查价
DEFAULT_MAX_DAYS = 7              # --dates 一次最多扫描多少天
DEFAULT_WATCH_INTERVAL = 180      # 余票监控默认间隔（秒）
MIN_WATCH_INTERVAL = 60           # 再快就很容易触发 12306 限流
CACHE_SCHEMA = 2                  # 缓存里存的是「按下标裁剪过的行」，
                                  # 一旦下标映射变了，旧缓存就会错位，必须作废重来。
QUERY_RETRIES = 2

# 返回行（58 列，`|` 分隔）中本脚本用得到的下标，均为 2026-10 实测确认。
#
# ★ 席别余票的下标曾经整体错位一格（把 idx30 当成硬座，实际是二等座），
#   导致高铁显示「硬座有票」。现已改用页面渲染出来的 aria-label 做基准逐列校正：
#   例如 G1224 页面显示「商务座候补 / 一等座1 / 二等座有 / 无座12」，
#   对应原始字段 idx32=无、idx31=1、idx30=有、idx26=12，一一对上。
#   普速同样校正过：T132 页面「硬座1 / 无座无」对应 idx29=1、idx26=无。
IDX_TRAIN_NO = 2          # 车次内部编号（查票价要用）
IDX_TRAIN_CODE = 3        # 车次
IDX_START_STATION = 4     # 始发站电报码
IDX_END_STATION = 5       # 终到站电报码
IDX_FROM = 6              # 乘车站电报码
IDX_TO = 7                # 下车站电报码
IDX_DEPART = 8            # 发车时刻
IDX_ARRIVE = 9            # 到达时刻
IDX_LISHI = 10            # 历时
IDX_FROM_NO = 16          # 乘车站序号（查票价要用）
IDX_TO_NO = 17            # 下车站序号（查票价要用）
IDX_RW = 23               # 软卧
IDX_WZ = 26               # 无座
IDX_YW = 28               # 硬卧
IDX_YZ = 29               # 硬座
IDX_ZE = 30               # 二等座
IDX_ZY = 31               # 一等座
IDX_SWZ = 32              # 商务座
IDX_SEAT_TYPES = 35       # 席别代码串（查票价要用）

# 席别代码 → 中文名。9/M/O/WZ/1/3/4/6 为 2026-10 实测确认，
# 其余为 12306 通用代码，遇到不认识的会原样显示成「席别X」而不是瞎猜。
SEAT_NAMES = {
    "9": "商务座", "P": "特等座", "M": "一等座", "O": "二等座", "S": "商务座",
    "1": "硬座", "2": "软座", "3": "硬卧", "4": "软卧", "5": "包厢硬卧",
    "6": "高级软卧", "W": "高级动卧", "WZ": "无座",
}

# 席别筛选：用户可写的简体中文名 → Train 上对应的余票字段。
# 只覆盖脚本能从余票行判断的席别；特等座等没有独立字段的席别不在其中。
SEAT_CLASS_FIELDS = {
    "商务座": "swz",
    "一等座": "zy",
    "二等座": "ze",
    "软卧": "rw",
    "硬卧": "yw",
    "硬座": "yz",
    "无座": "wz",
}
SEAT_CLASS_ALIASES = {
    "卧铺": ("软卧", "硬卧"),
    "坐票": ("商务座", "一等座", "二等座", "硬座"),
    "座位": ("商务座", "一等座", "二等座", "硬座"),
}

# 12306 的「城市编码」是沿铁路地理顺序编排的（黑龙江 00xx → 新疆 24xx），
# 详见 _pick_hubs_geographically 的注释。

# 失败分类（对齐 shared/failure-taxonomy.md）
FC_ENV = "environment-missing"
FC_CONN = "connection-failure"
FC_AMBIGUOUS = "ambiguous-context"
FC_TARGET = "target-response-abnormal"
FC_ARTIFACT = "artifact-missing"


# --- 数据模型 -------------------------------------------------------------
@dataclass
class Station:
    name: str
    telecode: str
    pinyin: str
    abbr: str
    city: str
    city_code: str


@dataclass
class Resolution:
    """用户输入（站名/城市名/拼音/电报码）的解析结果。

    kind:
      city     —— 命中的是城市，且该城有多个车站，需要主人指定具体车站
      station  —— 唯一命中一个车站，可以直接查询
      multiple —— 模糊匹配到多个互不同城的车站，同样需要主人指定
      none     —— 解析不出来
    """
    kind: str
    city: str = ""
    stations: list[Station] = field(default_factory=list)
    matched: str = ""

    @property
    def needs_choice(self) -> bool:
        return self.kind in ("city", "multiple") and len(self.stations) > 1


@dataclass
class Train:
    """一趟车的乘车区间信息。"""
    code: str
    from_code: str
    to_code: str
    dep: str
    arr: str
    lishi: str
    date: str
    start_code: str = ""
    end_code: str = ""
    wz: str = ""              # 无座
    rw: str = ""              # 软卧
    yw: str = ""              # 硬卧
    yz: str = ""              # 硬座
    ze: str = ""              # 二等座
    zy: str = ""              # 一等座
    swz: str = ""             # 商务座
    # 查票价接口 queryTicketPrice 需要的四个参数（下标均于 2026-10 实测确认）
    train_no: str = ""
    from_station_no: str = ""
    to_station_no: str = ""
    seat_types: str = ""
    prices: dict[str, str] | None = None   # 席别名 → 票价字符串；None 表示尚未查询

    def dep_dt(self) -> datetime | None:
        return _clock_to_dt(self.date, self.dep)

    def arr_dt(self) -> datetime | None:
        """到达时刻。

        优先用「发车时刻 + 历时」计算：`lishi` 能正确处理跨天，甚至跨两天的
        超长车次（如广州→乌鲁木齐）。历时缺失时才退回「到达钟点小于发车
        钟点 = 次日」的旧推断。
        """
        d = self.dep_dt()
        if d is not None:
            dur = _duration_min(self.lishi)
            if dur is not None:
                return d + timedelta(minutes=dur)
        day = _parse_date(self.date)
        dep_m = _clock_to_min(self.dep)
        arr_m = _clock_to_min(self.arr)
        if day is None or dep_m is None or arr_m is None:
            return None
        if arr_m < dep_m:
            day = day + timedelta(days=1)
        return datetime(day.year, day.month, day.day) + timedelta(minutes=arr_m)

    def seats_text(self, show_standing: bool = True) -> str:
        """席别余票的紧凑显示。

        原始值里「无」表示无票（页面上会渲染成「候补」），「有」表示 ≥20 张。
        顺序按动车组优先（商务/一等/二等），普速席别排在后面。
        `--no-standing` 时隐藏无座。
        """
        parts = []
        for label, val in (("商务", self.swz), ("一等", self.zy), ("二等", self.ze),
                           ("软卧", self.rw), ("硬卧", self.yw), ("硬座", self.yz),
                           ("无座", self.wz)):
            if label == "无座" and not show_standing:
                continue
            if val and val not in ("", "--", "*"):
                parts.append(f"{label}{val}")
        return " ".join(parts) if parts else "—"

    def has_seat(self) -> bool:
        """是否有可买的座/卧票。无座不算「有票」。"""
        for val in (self.swz, self.zy, self.ze, self.rw, self.yw, self.yz):
            if val and val not in ("", "--", "*", "无"):
                return True
        return False

    def has_class(self, classes: set[str]) -> bool:
        """指定席别里至少有一种有余票。空集合视为不限制。"""
        if not classes:
            return True
        for name in classes:
            field = SEAT_CLASS_FIELDS.get(name)
            if not field:
                continue
            val = getattr(self, field, "")
            if val and val not in ("", "--", "*", "无"):
                return True
        return False

    def price_for_classes(self, classes: set[str] | None = None) -> float:
        """指定席别的最低价；没有价格或无匹配席别时返回 1e9。"""
        if not self.prices:
            return 1e9
        if not classes:
            return self.cheapest()
        vals = [v for k, v in self.prices.items() if k in classes]
        return min((_price_value(v) for v in vals), default=1e9)

    def prices_text(self, limit: int = 3) -> str:
        """票价的紧凑显示：按价格从低到高，最多列几个席别。"""
        if not self.prices:
            return ""
        items = sorted(self.prices.items(), key=lambda kv: _price_value(kv[1]))
        return " ".join(f"{name}{val}" for name, val in items[:limit])

    def cheapest(self) -> float:
        if not self.prices:
            return 1e9
        return min(_price_value(v) for v in self.prices.values())


@dataclass
class Transfer:
    """两段车次之间的换乘点。"""
    hub_city: str
    arrive_code: str
    depart_code: str
    same_station: bool
    gap_min: int
    required_min: int
    interstation_min: int = 0
    interstation_estimated: bool = False
    near_miss: bool = False


@dataclass
class Plan:
    """一条换乘方案：两段或三段车次 + 它们之间的换乘点。"""
    legs: list[Train]
    transfers: list[Transfer]
    near_miss: bool = False

    @property
    def leg1(self) -> Train:
        return self.legs[0]

    @property
    def leg2(self) -> Train:
        return self.legs[-1]

    def total_min(self) -> int:
        a = self.legs[0].dep_dt()
        b = self.legs[-1].arr_dt()
        if a is None or b is None:
            return 0
        return int((b - a).total_seconds() // 60)

    def min_gap(self) -> int:
        return min((t.gap_min for t in self.transfers), default=0)

    def cheapest_price(self, seat_classes: set[str] | None = None) -> float:
        total = 0.0
        for leg in self.legs:
            v = leg.price_for_classes(seat_classes)
            if v >= 1e9:
                return 1e9
            total += v
        return total


@dataclass
class Outcome:
    """对齐 shared/contracts.md 的 Command Outcome Schema。"""
    status: str = "success"           # success / partial_success / blocked / failure
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    next_action: str = ""
    failure_category: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


# --- 小工具 ---------------------------------------------------------------
def _parse_date(text: str) -> _date | None:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def _clock_to_min(hhmm: str) -> int | None:
    if not hhmm:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})", hhmm.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 47:  # 12306 跨天时会用 24+ 的写法
        return None
    return h * 60 + mi


def _clock_to_dt(date_str: str, hhmm: str) -> datetime | None:
    d = _parse_date(date_str)
    minutes = _clock_to_min(hhmm)
    if d is None or minutes is None:
        return None
    return datetime(d.year, d.month, d.day) + timedelta(minutes=minutes)


def _fmt_minutes(total: int) -> str:
    if total <= 0:
        return "0分"
    h, m = divmod(total, 60)
    if h and m:
        return f"{h}小时{m}分"
    if h:
        return f"{h}小时"
    return f"{m}分"


def _fmt_price(prices: dict) -> str:
    """票价的紧凑渲染；按价格从低到高，最多三个席别。"""
    if not prices:
        return ""
    items = sorted(prices.items(), key=lambda kv: _price_value(kv[1]))
    return "   票价 " + " ".join(f"{name}{val}" for name, val in items[:3])


def _now_minutes() -> int:
    n = datetime.now()
    return n.hour * 60 + n.minute


def _duration_min(text: str) -> int | None:
    """解析 12306 的历时字段（"12:34"；超长车次会出现 "47:10"）。"""
    if not text:
        return None
    m = re.match(r"^(\d{1,3}):(\d{2})$", str(text).strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _parse_hhmm(text: str | None) -> int | None:
    """解析用户给的时间窗 HH:MM，返回一天内的分钟数。"""
    if not text:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(text).strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h * 60 + mi


def _split_csv(text: str | None) -> list[str]:
    return [x.strip() for x in (text or "").split(",") if x.strip()]


def _normalize_seat_classes(text: str | None) -> tuple[set[str], list[str]]:
    """把 --seat-class 的文本展开成规范席别名集合，返回 (集合, 无法识别的项)。"""
    classes: set[str] = set()
    bad: list[str] = []
    for item in _split_csv(text):
        if item in SEAT_CLASS_FIELDS:
            classes.add(item)
        elif item in SEAT_CLASS_ALIASES:
            classes.update(SEAT_CLASS_ALIASES[item])
        else:
            bad.append(item)
    return classes, bad


def _expand_dates(spec: str) -> tuple[list[str], str]:
    """展开 --dates：支持 YYYY-MM-DD..YYYY-MM-DD 与逗号分隔的多个日期。"""
    raw = (spec or "").strip()
    if not raw:
        return [], "日期为空"
    out: list[str] = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        if ".." in part:
            a, b = [x.strip() for x in part.split("..", 1)]
            da, db = _parse_date(a), _parse_date(b)
            if not da or not db:
                return [], f"日期范围「{part}」格式不对，应为 YYYY-MM-DD..YYYY-MM-DD"
            if db < da:
                return [], f"日期范围「{part}」的结束早于开始"
            cur = da
            while cur <= db:
                out.append(cur.isoformat())
                cur += timedelta(days=1)
                if len(out) > 366:
                    return [], "日期范围过大"
        else:
            d = _parse_date(part)
            if not d:
                return [], f"日期「{part}」格式不对，应为 YYYY-MM-DD"
            out.append(d.isoformat())
    seen: set[str] = set()
    uniq: list[str] = []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq, ""


def _plan_sort_key(p: Plan, sort_key: str,
                   seat_classes: set[str] | None = None) -> tuple:
    total = p.total_min()
    if sort_key == "arrive":
        b = p.legs[-1].arr_dt()
        return (b.timestamp() if b else 1e18, total)
    if sort_key == "depart":
        a = p.legs[0].dep_dt()
        return (a.timestamp() if a else 1e18, total)
    if sort_key == "gap":
        return (p.min_gap(), total)
    if sort_key == "price":
        return (p.cheapest_price(seat_classes), total)
    return (total, p.min_gap())


def _sort_trains(trains: list[Train], sort_key: str) -> list[Train]:
    """直达车次排序。total/gap 对单趟车没有意义，退化为按发车时刻。"""
    if sort_key == "arrive":
        return sorted(trains, key=lambda t: (t.arr_dt() or datetime.max, t.dep or ""))
    if sort_key == "price":
        return sorted(trains, key=lambda t: (t.cheapest(), t.dep or ""))
    return sorted(trains, key=lambda t: (t.dep_dt() or datetime.max, t.code))


# --- 站点表 ---------------------------------------------------------------
class StationTable:
    """12306 站点表。静态资源，不走风控。"""

    def __init__(self, stations: list[Station]) -> None:
        self.stations = stations
        self.by_name: dict[str, Station] = {}
        self.by_telecode: dict[str, Station] = {}
        self.by_city: dict[str, list[Station]] = {}
        self.by_alias: dict[str, list[Station]] = {}
        for st in stations:
            self.by_telecode[st.telecode] = st
            self.by_name.setdefault(st.name, st)
            self.by_city.setdefault(st.city, []).append(st)
            for alias in (st.pinyin, st.abbr, st.name.rstrip("站")):
                if alias:
                    self.by_alias.setdefault(alias.lower(), []).append(st)

    def name_of(self, telecode: str) -> str:
        st = self.by_telecode.get(telecode)
        return st.name if st else (telecode or "?")

    def city_of(self, telecode: str) -> str:
        st = self.by_telecode.get(telecode)
        return st.city if st else ""

    def resolve(self, text: str) -> Resolution:
        """把用户输入解析成候选车站。

        城市名优先于站名：12306 的「城市」是地级市口径，一个城市往往有十几个
        车站（苏州名下 14 个），主人明确要求「输入城市名时先列出车站让我选」，
        所以即便「苏州」同时是站名，也要先按城市处理。
        """
        key = (text or "").strip()
        if not key:
            return Resolution(kind="none")

        # 「XX站」是极自然的写法，优先按「明确指定这个车站」处理。
        # 这一条必须在城市名判定之前，否则当站名与城市名重名时
        # （如「秦皇岛」既是城市又是车站），主人就永远选不到那个车站本身了。
        if len(key) > 1 and key.endswith("站"):
            bare = key[:-1]
            if bare in self.by_name:
                st = self.by_name[bare]
                return Resolution(kind="station", city=st.city,
                                  stations=[st], matched=key)

        if key in self.by_city:
            sts = self.by_city[key]
            if len(sts) > 1:
                return Resolution(kind="city", city=key, stations=list(sts), matched=key)
            return Resolution(kind="station", city=key, stations=list(sts), matched=key)

        if key in self.by_name:
            st = self.by_name[key]
            return Resolution(kind="station", city=st.city, stations=[st], matched=key)

        if key.upper() in self.by_telecode:
            st = self.by_telecode[key.upper()]
            return Resolution(kind="station", city=st.city, stations=[st], matched=key)

        hits = self.by_alias.get(key.lower(), [])
        if not hits:
            hits = list(self.by_alias.get(key.lower() + "站", []))
        if not hits:
            hits = [s for s in self.stations if s.name.startswith(key)]

        if hits:
            uniq = sorted({s.telecode: s for s in hits}.values(), key=lambda s: len(s.name))
            if len(uniq) == 1:
                return Resolution(kind="station", city=uniq[0].city,
                                  stations=uniq, matched=key)
            cities = {s.city for s in uniq}
            kind = "city" if len(cities) == 1 else "multiple"
            return Resolution(kind=kind, city=(uniq[0].city if kind == "city" else ""),
                              stations=uniq, matched=key)

        return Resolution(kind="none")

    def city_stations(self, city: str) -> list[Station]:
        return list(self.by_city.get(city, []))


def _cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    d = root / "rail-transfer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_station_js(text: str) -> list[Station]:
    stations: list[Station] = []
    for chunk in text.split("@"):
        if not chunk or "|" not in chunk:
            continue
        f = chunk.split("|")
        if len(f) < 8:
            continue
        abbr, name, telecode, pinyin = f[0], f[1], f[2], f[3]
        city_code, city = f[6], f[7]
        if not telecode or not name:
            continue
        stations.append(Station(name=name, telecode=telecode, pinyin=pinyin,
                                abbr=abbr, city=city, city_code=city_code))
    return stations


def load_station_table(refresh: bool = False, verbose: bool = False) -> tuple[StationTable | None, str]:
    """加载站点表，返回 (表, 错误说明)。"""
    cache = _cache_dir() / "station_name.js"
    text = ""

    if cache.is_file() and not refresh:
        age = time.time() - cache.stat().st_mtime
        if age < STATION_CACHE_TTL_DAYS * 86400:
            try:
                text = cache.read_text(encoding="utf-8")
                if verbose:
                    print(f"[站点表] 命中缓存 {cache}", file=sys.stderr)
            except OSError:
                text = ""

    if not text:
        try:
            req = urllib.request.Request(
                STATION_TABLE_URL,
                headers={"User-Agent": _ua(),
                         "Referer": "https://kyfw.12306.cn/otn/leftTicket/init"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            cache.write_text(text, encoding="utf-8")
            if verbose:
                print(f"[站点表] 已刷新 {cache}", file=sys.stderr)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            if cache.is_file():   # 联网失败时退回旧缓存
                try:
                    text = cache.read_text(encoding="utf-8")
                except OSError:
                    return None, f"站点表下载失败且缓存不可读：{exc}"
            else:
                return None, f"站点表下载失败：{exc}"

    stations = _parse_station_js(text)
    if len(stations) < 100:
        return None, f"站点表解析异常，只得到 {len(stations)} 个站（缓存可能损坏，可用 --refresh-stations）"
    return StationTable(stations), ""


def _ua() -> str:
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# --- 无头浏览器（Edge + CDP）---------------------------------------------
def find_browser(explicit: str | None = None) -> str | None:
    """按 CLI → 配置 → 环境变量 → 常见路径 的顺序找浏览器。"""
    if explicit:
        p = Path(explicit)
        return str(p) if p.is_file() else None

    configured = get_tool_path(BROWSER_TOOL_KEY)
    if configured and Path(configured).is_file():
        return configured

    env = os.environ.get("RAIL_TRANSFER_BROWSER")
    if env and Path(env).is_file():
        return env

    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/microsoft-edge", "/usr/bin/google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    for name in ("msedge", "google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


class _NoProxyEnv:
    """临时清掉代理环境变量：CDP 走 127.0.0.1，代理会把它带偏。"""

    KEYS = ("http_proxy", "https_proxy", "all_proxy",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

    def __enter__(self):
        self.saved = {k: os.environ.pop(k, None) for k in self.KEYS}
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        os.environ["no_proxy"] = "127.0.0.1,localhost"
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is not None:
                os.environ[k] = v
        return False


class BrowserSession:
    """用 Edge 的 DevTools 协议驱动一个无头标签页。"""

    def __init__(self, browser_path: str, timeout: int = 30, verbose: bool = False) -> None:
        self.browser_path = browser_path
        self.timeout = timeout
        self.verbose = verbose
        self.proc: subprocess.Popen | None = None
        self.ws = None
        self.profile_dir: Path | None = None
        self._msg_id = 0
        self._no_proxy = _NoProxyEnv()

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> None:
        try:
            import websocket  # noqa: F401  仅用于探测依赖
        except ImportError as exc:
            raise RuntimeError(
                "缺少 websocket-client，请先执行："
                "python -m pip install websocket-client"
            ) from exc

        self._no_proxy.__enter__()
        port = _free_port()
        self.profile_dir = Path(tempfile.mkdtemp(prefix="rail-transfer-edge-"))

        args = [
            self.browser_path,
            "--headless=new",
            f"--remote-debugging-port={port}",
            "--no-proxy-server",          # Clash 会让浏览器自动化全灭，必须显式绕过
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--window-size=1440,900",
            "about:blank",
        ]
        creation = 0
        if os.name == "nt":
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=creation)

        deadline = time.time() + self.timeout
        version = None
        while time.time() < deadline:
            try:
                version = _http_json(f"http://127.0.0.1:{port}/json/version", timeout=3)
                break
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"浏览器进程提前退出（退出码 {self.proc.returncode}）")
                time.sleep(0.4)
        if not version:
            raise RuntimeError(f"等待 CDP 端口 {port} 超时")

        target = _http_json(f"http://127.0.0.1:{port}/json/new?about:blank",
                            method="PUT", timeout=10)
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("未能创建调试标签页")

        import websocket
        self.ws = websocket.create_connection(
            ws_url, timeout=self.timeout,
            http_no_proxy=["127.0.0.1", "localhost"],
            suppress_origin=True,
        )
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.navigate", {"url": INIT_PAGE})
        self._wait_ready()

    def close(self) -> None:
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        try:
            if self.profile_dir and self.profile_dir.exists():
                shutil.rmtree(self.profile_dir, ignore_errors=True)
        except Exception:
            pass
        self._no_proxy.__exit__(None, None, None)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- CDP 原语 ---------------------------------------------------------
    def _send(self, method: str, params: dict | None = None) -> int:
        self._msg_id += 1
        payload = {"id": self._msg_id, "method": method, "params": params or {}}
        self.ws.send(json.dumps(payload))
        return self._msg_id

    def _collect(self, want: set[int], deadline: float) -> dict[int, dict]:
        got: dict[int, dict] = {}
        while want - set(got):
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self.ws.settimeout(max(0.5, min(remaining, 5.0)))
            try:
                raw = self.ws.recv()
            except Exception:
                continue
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            mid = msg.get("id")
            if mid in want:
                got[mid] = msg
        return got

    def call(self, method: str, params: dict | None = None) -> dict:
        mid = self._send(method, params)
        res = self._collect({mid}, time.time() + self.timeout)
        if mid not in res:
            raise RuntimeError(f"CDP 调用超时：{method}")
        return res[mid]

    def evaluate(self, expr: str, await_promise: bool = True) -> Any:
        mid = self._send("Runtime.evaluate", {
            "expression": expr,
            "awaitPromise": await_promise,
            "returnByValue": True,
        })
        res = self._collect({mid}, time.time() + self.timeout)
        if mid not in res:
            raise RuntimeError("页面求值超时")
        return _extract_value(res[mid])

    def evaluate_many(self, exprs: list[str], await_promise: bool = True) -> list[Any]:
        """并发求值：一次发多条，再统一收结果。"""
        ids = [self._send("Runtime.evaluate", {
            "expression": e, "awaitPromise": await_promise, "returnByValue": True,
        }) for e in exprs]
        res = self._collect(set(ids), time.time() + self.timeout * 3)
        out: list[Any] = []
        for i in ids:
            if i in res:
                try:
                    out.append(_extract_value(res[i]))
                except RuntimeError as exc:
                    out.append({"__eval_error": str(exc)})
            else:
                out.append({"__eval_error": "求值超时"})
        return out

    def _wait_ready(self, limit: float = 25.0) -> None:
        deadline = time.time() + limit
        while time.time() < deadline:
            try:
                state = self.evaluate(
                    'JSON.stringify({r:document.readyState,t:document.title,'
                    'h:location.href})', await_promise=False)
                info = json.loads(state) if isinstance(state, str) else {}
                if (info.get("r") == "complete"
                        and "12306" in str(info.get("h", ""))
                        and "leftTicket" in str(info.get("h", ""))):
                    time.sleep(3.0)   # 留给页面里的风控脚本就位
                    return
            except Exception:
                pass
            time.sleep(0.7)
        raise RuntimeError("12306 查询页加载超时（可能被风控拦截或网络不通）")

    # -- 业务查询 ---------------------------------------------------------
    def query(self, from_code: str, to_code: str, date_str: str) -> dict:
        url = (f"{API_BASE}/otn/leftTicket/queryG"
               f"?leftTicketDTO.train_date={date_str}"
               f"&leftTicketDTO.from_station={from_code}"
               f"&leftTicketDTO.to_station={to_code}"
               f"&purpose_codes=ADULT")   # ← 必须是 purpose_codes=ADULT
        expr = _QUERY_JS % json.dumps(url)
        raw = self.evaluate(expr)
        if not isinstance(raw, str):
            return {"_err": f"返回类型异常：{type(raw).__name__}"}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_err": "返回内容不是合法 JSON"}

    def query_price(self, train: Train) -> dict:
        """查询某趟车在某区间的票价。

        参数全部取自余票行，所以必须先查过余票才能查票价。
        """
        if not (train.train_no and train.from_station_no
                and train.to_station_no and train.seat_types):
            return {"_err": "余票行缺少 train_no / from_station_no / to_station_no / seat_types"}
        url = (f"{API_BASE}/otn/leftTicket/queryTicketPrice"
               f"?train_no={train.train_no}"
               f"&from_station_no={train.from_station_no}"
               f"&to_station_no={train.to_station_no}"
               f"&seat_types={train.seat_types}"
               f"&train_date={train.date}")
        return _decode(self.evaluate(_PRICE_JS % json.dumps(url)))


# 页面内查询脚本。只回传需要的列，避免几百 KB 的原始行穿网。
_QUERY_JS = """
(async () => {
  try {
    const r = await fetch(%s, {
      credentials: "include",
      headers: {"X-Requested-With": "XMLHttpRequest"}
    });
    if (r.status !== 200) return JSON.stringify({_http: r.status});
    const j = await r.json();
    const d = j.data || {};
    const rows = (d.result || []).map(s => s.split("|"));
    const idx = [3, 4, 5, 6, 7, 8, 9, 10, 26, 23, 28, 29, 30, 31, 32, 2, 16, 17, 35];
    return JSON.stringify({
      _ok: 1,
      map: d.map || {},
      rows: rows.map(f => idx.map(i => f[i] === undefined ? "" : f[i]))
    });
  } catch (e) {
    return JSON.stringify({_err: String((e && e.message) || e)});
  }
})()
"""


# 页面内票价查询脚本。参数取自余票行：train_no=[2]、from_station_no=[16]、
# to_station_no=[17]、seat_types=[35]（均为 2026-10 实测确认）。
_PRICE_JS = """
(async () => {
  try {
    const r = await fetch(%s, {
      credentials: "include",
      headers: {"X-Requested-With": "XMLHttpRequest"}
    });
    if (r.status !== 200) return JSON.stringify({_http: r.status});
    const j = await r.json();
    return JSON.stringify({_ok: 1, data: (j && j.data) || {}});
  } catch (e) {
    return JSON.stringify({_err: String((e && e.message) || e)});
  }
})()
"""


def _extract_value(msg: dict) -> Any:
    err = msg.get("error")
    if err:
        raise RuntimeError(f"CDP 错误：{err.get('message')}")
    result = (msg.get("result") or {}).get("result") or {}
    if "exceptionDetails" in (msg.get("result") or {}):
        detail = msg["result"]["exceptionDetails"]
        desc = (detail.get("exception") or {}).get("description") or detail.get("text")
        raise RuntimeError(f"页面异常：{desc}")
    return result.get("value")


def _price_value(text: str) -> float:
    """把 "¥32.5" 解析成 32.5，用于排序与比较；解析不了就当很大。"""
    try:
        return float(str(text).lstrip("¥￥").strip())
    except (TypeError, ValueError):
        return 1e9


def _parse_price(data: dict) -> dict[str, str]:
    """把 queryTicketPrice 的 data 解析成 {席别中文名: 票价字符串}。

    返回体实测有两套写法，两种都要认：
      {"A1":"¥32.5","1":"325", ...}      A 前缀是带 ¥ 的显示价，裸键是 ×10 的整数
      {"M":"¥124.0","O":"¥77.0", ...}    直接就是带 ¥ 的显示价
    优先采用带 ¥ 的显示值，避免自己做除法引入误差。
    """
    prices: dict[str, str] = {}
    if not isinstance(data, dict):
        return prices

    def put(code: str, value: str) -> None:
        code = code.strip()
        if not code:
            return
        name = SEAT_NAMES.get(code, f"席别{code}")
        if name not in prices:      # 先写入的优先（A 前缀带 ¥ 的最准）
            prices[name] = value.strip()

    def usable(key: str, val: Any) -> bool:
        return (key not in ("OT", "train_no") and isinstance(val, str) and bool(val))

    for key, val in data.items():                       # 第一轮：A 前缀
        if usable(key, val) and key.startswith("A") and len(key) > 1:
            put(key[1:], val)
    for key, val in data.items():                       # 第二轮：裸键兜底
        if not usable(key, val) or key.startswith("A"):
            continue
        if val.startswith("¥") or val.startswith("￥"):
            put(key, val)
        else:
            try:
                put(key, f"¥{int(val) / 10:.1f}")
            except (TypeError, ValueError):
                continue
    return prices


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_json(url: str, method: str = "GET", timeout: int = 5) -> dict:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --- 查询编排 -------------------------------------------------------------
def _query_js(from_code: str, to_code: str, date_str: str) -> str:
    url = (f"{API_BASE}/otn/leftTicket/queryG"
           f"?leftTicketDTO.train_date={date_str}"
           f"&leftTicketDTO.from_station={from_code}"
           f"&leftTicketDTO.to_station={to_code}"
           f"&purpose_codes=ADULT")   # ← 必须是 purpose_codes=ADULT
    return _QUERY_JS % json.dumps(url)


def _decode(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_err": "返回不是合法 JSON"}
    return {"_err": f"返回类型异常：{type(raw).__name__}"}


class QueryCache:
    """查询结果的磁盘缓存（按「出发城市|到达城市|日期」）。

    主人多半会反复微调参数（改 --min-transfer、换 --via、加 --all-stations）
    跑同一个出行日期，每次都重打 12306 既慢又容易触发限流，所以缓存一小段时间。

    余票是动态数据，默认 TTL 只有 10 分钟；票价是公布票价，QueryEngine 会用
    `--price-cache-ttl`（默认一天）单独写入。每条记录自带 ttl，互不影响。
    """

    def __init__(self, path: Path, ttl: int, enabled: bool = True) -> None:
        self.path = path
        self.ttl = ttl
        self.enabled = enabled
        self.hits = 0
        self.dirty = False
        self.data: dict[str, dict] = {}
        if not enabled:
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("_schema") == CACHE_SCHEMA:
                now = time.time()
                kept: dict[str, dict] = {}
                for k, v in raw.items():
                    if k.startswith("_") or not isinstance(v, dict):
                        continue
                    entry_ttl = int(v.get("ttl", ttl))
                    if now - v.get("t", 0) < entry_ttl:
                        kept[k] = v
                self.data = kept
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def get(self, key: tuple[str, ...]) -> list | dict | None:
        if not self.enabled:
            return None
        rec = self.data.get("|".join(key))
        if not rec:
            return None
        ttl = int(rec.get("ttl", self.ttl))
        if time.time() - rec.get("t", 0) >= ttl:
            return None
        rows = rec.get("rows")
        if not isinstance(rows, (list, dict)):
            return None
        self.hits += 1
        return rows

    def put(self, key: tuple[str, ...], rows: list | dict,
            ttl: int | None = None) -> None:
        if not self.enabled:
            return
        self.data["|".join(key)] = {
            "t": time.time(),
            "ttl": int(ttl if ttl is not None else self.ttl),
            "rows": rows,
        }
        self.dirty = True

    def save(self) -> None:
        if not self.enabled or not self.dirty:
            return
        try:
            payload = {"_schema": CACHE_SCHEMA}
            payload.update(self.data)
            self.path.write_text(json.dumps(payload, ensure_ascii=False),
                                 encoding="utf-8")
            self.dirty = False
        except OSError:
            pass


class RequestBudget:
    """请求预算。limit <= 0 表示不限制；每次真实请求都要先 take。"""

    def __init__(self, limit: int = 0) -> None:
        self.limit = max(0, int(limit))
        self.used = 0

    @property
    def remaining(self) -> int:
        if self.limit <= 0:
            return 1 << 30
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.limit > 0 and self.used >= self.limit

    def take(self, n: int = 1) -> int:
        if self.limit <= 0:
            self.used += n
            return n
        allowed = max(0, min(n, self.limit - self.used))
        self.used += allowed
        return allowed


class QueryEngine:
    """在同一个浏览器会话里批量查询，带并发上限、重试与请求预算。

    ★ 实测结论（2026-10）：这个接口是按【城市对】返回的，**不是**按站点对。
      查「句容西→苏州」返回的行里，乘车站既有 句容西(JWH) 也有 句容(JRU)，
      下车站覆盖 张家港/常熟/太仓/苏州南 等苏州名下各站；而且 JWH→SZH、
      JWH→ZAU、JRU→SZH 三个不同站点对返回的集合**完全一致**。
      查 北京南→上海虹桥 同理，结果里同时含 北京站 与 北京南、上海站 与 上海松江。

      所以这里按【城市对】做缓存：一次查询就拿到两城之间的全部车次，
      每行自带真实的乘车站/下车站（下标 6/7），由调用方按需过滤。
      好处是查询量从「出发站 × 到达站」降到「出发城市 × 到达城市」。
    """

    def __init__(self, session: BrowserSession, table: StationTable,
                 concurrency: int = DEFAULT_CONCURRENCY, verbose: bool = False,
                 disk: QueryCache | None = None,
                 budget: RequestBudget | None = None,
                 price_ttl: int = DEFAULT_PRICE_CACHE_TTL,
                 use_availability_cache: bool = True) -> None:
        self.session = session
        self.table = table
        self.concurrency = max(1, concurrency)
        self.verbose = verbose
        self.disk = disk
        self.budget = budget if budget is not None else RequestBudget(0)
        self.price_ttl = price_ttl
        self.use_availability_cache = use_availability_cache
        self.cache: dict[tuple[str, str, str], list[Train]] = {}
        self.price_cache: dict[tuple, dict[str, str]] = {}
        self.errors: list[str] = []
        self.calls = 0
        self.budget_exhausted = False
        self._delay = 0.2        # 自适应节流间隔，失败就放慢、顺利就提速

    def _city(self, code: str) -> str:
        return self.table.city_of(code) or code

    def _key(self, from_code: str, to_code: str, date_str: str) -> tuple[str, str, str]:
        return (self._city(from_code), self._city(to_code), date_str)

    def query_pair(self, from_code: str, to_code: str, date_str: str) -> list[Train]:
        """取两座城市之间的全部车次（不按站点过滤）。"""
        return self.query_pairs([(from_code, to_code, date_str)])[
            (from_code, to_code, date_str)]

    def query_pairs(self, jobs: list[tuple[str, str, str]]
                    ) -> dict[tuple[str, str, str], list[Train]]:
        """并发跑一批查询；jobs 为 (from, to, date)，内部按城市对去重。

        失败的城市对会自动补查：实测并发下偶发 `Failed to fetch`（12306 侧
        瞬时抖动，与参数、cookie 无关），重试一次基本都能补齐。重试时降低并发。
        `RequestBudget` 用完后立即停止，未完成的查询按空结果返回，绝不污染缓存。
        """
        wanted: dict[tuple[str, str, str], tuple[str, str, str]] = {}
        for f, t, d in jobs:
            key = self._key(f, t, d)
            if key in self.cache or key in wanted:
                continue
            rows = (self.disk.get(key)
                    if (self.disk and self.use_availability_cache) else None)
            if rows is not None:
                self.cache[key] = _parse_rows({"rows": rows}, f, t, d)
                continue
            wanted[key] = (f, t, d)

        pending = list(wanted.items())
        for attempt in range(QUERY_RETRIES + 1):
            failed: list[tuple[tuple[str, str, str], tuple[str, str, str], str]] = []
            limit = self.concurrency if attempt == 0 else max(1, self.concurrency // 2)
            for i in range(0, len(pending), limit):
                if self.budget.remaining <= 0:
                    self.budget_exhausted = True
                    break
                batch = pending[i:i + limit]
                allowed = self.budget.take(len(batch))
                if allowed < len(batch):
                    batch = batch[:allowed]
                    self.budget_exhausted = True
                exprs = [_query_js(f, t, d) for _, (f, t, d) in batch]
                results = self.session.evaluate_many(exprs)
                self.calls += len(batch)
                batch_failed = 0
                for (key, (f, t, d)), raw in zip(batch, results):
                    payload = _decode(raw)
                    if payload.get("_ok"):
                        self.cache[key] = _parse_rows(payload, f, t, d)
                        if self.disk:
                            self.disk.put(key, payload.get("rows") or [])
                    else:
                        reason = payload.get("_err") or f"HTTP {payload.get('_http')}"
                        failed.append((key, (f, t, d), reason))
                        batch_failed += 1
                # 自适应节流：一出现失败就明显放慢，连续顺利再逐步提速
                if batch_failed:
                    self._delay = min(self._delay * 2 + 0.5, 5.0)
                else:
                    self._delay = max(self._delay * 0.7, 0.15)
                time.sleep(self._delay)

            if self.budget_exhausted:
                break
            if not failed:
                break
            if attempt >= QUERY_RETRIES:
                for key, (f, t, d), reason in failed:
                    self.cache[key] = []
                    self.errors.append(f"{f}->{t} {d}: {reason}")
                break
            backoff = 2.0 * (attempt + 1)
            if self.verbose:
                print(f"[重试] {len(failed)} 个城市对查询失败，{backoff:.0f} 秒后补查……",
                      file=sys.stderr)
            pending = [(key, jt) for key, jt, _ in failed]
            time.sleep(backoff)

        return {job: self.cache.get(self._key(*job), []) for job in jobs}

    def query_prices(self, trains: list[Train], limit: int = 0) -> int:
        """批量补票价，返回实际发出的查询次数。

        每次票价查询都是一次真实请求，12306 会按累计请求量限流，所以：
          · 同一「车次 + 区间 + 日期」只查一次（内存 + 磁盘双层缓存）
          · 磁盘缓存用 --price-cache-ttl（默认一天），不和 10 分钟的余票缓存混用
          · limit > 0 时只处理前 limit 趟，用来把请求量钉死在上限内
          · 失败或预算耗尽不会写入缓存，避免把「没查到」固化一天
        """
        sent = 0
        targets = trains[:limit] if limit > 0 else trains
        for train in targets:
            key = (train.train_no, train.from_station_no,
                   train.to_station_no, train.date)
            if key in self.price_cache:
                train.prices = self.price_cache[key]
                continue

            disk_key = ("price",) + key
            disk_hit = self.disk.get(disk_key) if self.disk else None
            if isinstance(disk_hit, dict):
                train.prices = disk_hit
                self.price_cache[key] = disk_hit
                continue

            prices: dict[str, str] = {}
            reason = ""
            budget_short = False
            for _attempt in range(QUERY_RETRIES + 1):
                if self.budget.remaining <= 0:
                    self.budget_exhausted = True
                    budget_short = True
                    reason = "请求预算耗尽"
                    break
                self.budget.take(1)
                self.calls += 1
                sent += 1
                try:
                    payload = self.session.query_price(train)
                except RuntimeError as exc:
                    reason = str(exc)
                    time.sleep(0.8)
                    continue
                if payload.get("_ok"):
                    prices = _parse_price(payload.get("data") or {})
                    reason = ""
                    break
                reason = payload.get("_err") or f"HTTP {payload.get('_http')}"
                time.sleep(0.8)

            train.prices = prices
            if reason:
                self.errors.append(f"票价 {train.code}: {reason}")
            else:
                self.price_cache[key] = prices
                if self.disk:
                    self.disk.put(disk_key, prices, ttl=self.price_ttl)
            if budget_short:
                break
            time.sleep(self._delay)
        return sent


def _parse_rows(payload: dict, from_code: str, to_code: str, date_str: str) -> list[Train]:
    """解析返回行。

    注意：行里的乘车站/下车站由 [6]/[7] 决定，**不一定等于查询参数** ——
    接口按城市对返回，所以一次查询会带回同一城市内多个车站的组合。
    """
    rows = payload.get("rows") or []
    trains: list[Train] = []
    for f in rows:
        # 页面侧已按 IDX_* 顺序裁剪：0..12 对应下面各字段
        if len(f) < 11:
            continue
        code = f[0]
        if not code or not f[3] or not f[4]:
            continue
        trains.append(Train(
            code=code,
            start_code=f[1], end_code=f[2],
            from_code=f[3], to_code=f[4],
            dep=f[5], arr=f[6], lishi=f[7],
            wz=f[8], rw=f[9], yw=f[10], yz=f[11],
            ze=f[12] if len(f) > 12 else "",
            zy=f[13] if len(f) > 13 else "",
            swz=f[14] if len(f) > 14 else "",
            train_no=f[15] if len(f) > 15 else "",
            from_station_no=f[16] if len(f) > 16 else "",
            to_station_no=f[17] if len(f) > 17 else "",
            seat_types=f[18] if len(f) > 18 else "",
            date=date_str,
        ))
    return trains


def _filter_trains(trains: list[Train], from_codes: set[str],
                   to_codes: set[str]) -> list[Train]:
    """过滤到用户真正选定的车站，并按 (车次, 乘车站, 下车站, 发车) 去重。"""
    seen: set[tuple] = set()
    out: list[Train] = []
    for t in trains:
        if t.from_code not in from_codes or t.to_code not in to_codes:
            continue
        key = (t.code, t.from_code, t.to_code, t.dep)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    out.sort(key=lambda t: t.dep or "")
    return out


# --- 同城异站通勤时间 ------------------------------------------------------
class InterchangeTable:
    def __init__(self, path: Path) -> None:
        self.default_min = 60
        self.pairs: dict[str, dict[str, int]] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.default_min = int(raw.get("_default", 60))
                for city, mapping in raw.items():
                    if city.startswith("_") or not isinstance(mapping, dict):
                        continue
                    self.pairs[city] = {k: int(v) for k, v in mapping.items()
                                        if isinstance(v, (int, float))}
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    def lookup(self, city: str, a: str, b: str) -> tuple[int, bool]:
        """返回 (分钟, 是否为估算值)。"""
        if a == b:
            return 0, False
        table = self.pairs.get(city)
        if table:
            if f"{a}|{b}" in table:
                return table[f"{a}|{b}"], False
            if f"{b}|{a}" in table:
                return table[f"{b}|{a}"], False
        return self.default_min, True


class TransferBufferTable:
    """站点级同站换乘最小缓冲（分钟）。

    `--min-transfer` 是全局下限；本表可以按车站再加严，例如大枢纽留 20 分钟。
    查不到的车站**不额外加严**（回落 0），最终仍按 `--min-transfer` 判断；
    这样主人主动调低 `--min-transfer` 时不会被表里的默认值顶回去。
    """

    def __init__(self, path: Path) -> None:
        self.default = 0
        self.stations: dict[str, int] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.default = int(raw.get("_default", 0))
                for name, value in (raw.get("stations") or {}).items():
                    if isinstance(value, (int, float)):
                        self.stations[str(name)] = int(value)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            pass

    def lookup(self, station_name: str) -> int:
        return self.stations.get(station_name, self.default)

    def floor_for(self, names: list[str]) -> int:
        return max([self.default] + [self.lookup(n) for n in names])


# --- 换乘搜索 -------------------------------------------------------------
def search_direct(engine: QueryEngine, from_codes: list[str], to_codes: list[str],
                  date_str: str) -> list[Train]:
    """直达车次。

    因为接口按城市对返回，这里先做城市对查询（query_pairs 内部自动去重），
    再把结果过滤到主人真正选定的车站，所以不会出现「查 14 个目的站、
    同一趟车重复出现 14 次」的问题。
    """
    jobs = [(f, t, date_str) for f in from_codes for t in to_codes]
    res = engine.query_pairs(jobs)
    pool: list[Train] = []
    for trains in res.values():
        pool.extend(trains)
    return _filter_trains(pool, set(from_codes), set(to_codes))


def _dedupe_train_pool(trains: list[Train]) -> list[Train]:
    seen: set[tuple] = set()
    out: list[Train] = []
    for t in trains:
        key = (t.code, t.from_code, t.to_code, t.dep, t.date)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    out.sort(key=lambda t: (t.dep_dt() or datetime.max, t.code))
    return out


def build_plans(engine: QueryEngine,
                from_codes: list[str],
                to_codes: list[str],
                hub_groups: list[tuple[str, list[str]]],
                date_str: str,
                table: StationTable,
                inter: InterchangeTable,
                buffers: TransferBufferTable,
                min_transfer: int,
                cross_buffer: int,
                allow_cross_station: bool,
                allow_overnight: bool,
                near_miss: int = 0) -> list[Plan]:
    """组合两段车次，产出满足换乘缓冲要求的方案。

    hub_groups 为 (城市名, [该城车站电报码]) 列表。由于接口按城市对返回，
    每个枢纽城市**只需要两次查询**（出发城市→枢纽城市、枢纽城市→到达城市），
    不管该城有多少个车站；同城异站换乘由行内自带的真实乘车站/下车站自然覆盖。

    `near_miss` > 0 时，把「差一点点」的方案也带回来（由调用方单独展示），
    判定标准是 required - near_miss，绝不放进主方案列表。
    """
    next_date = (_parse_date(date_str) + timedelta(days=1)).isoformat() if allow_overnight else None  # type: ignore[operator]
    leg2_dates = [date_str] + ([next_date] if next_date else [])

    # 第一程：出发城市 → 每个枢纽城市（每城一次查询）
    leg1_jobs = [(from_codes[0], hubs[0], date_str) for _, hubs in hub_groups]
    leg1_res = engine.query_pairs(leg1_jobs)

    # 按第一程的实际命中情况裁剪：根本没有车到达的枢纽城市直接跳过
    live: list[tuple[str, list[str], list[Train]]] = []
    leg2_jobs: list[tuple[str, str, str]] = []
    for (city, hubs), job in zip(hub_groups, leg1_jobs):
        arrivals = _filter_trains(leg1_res.get(job, []), set(from_codes), set(hubs))
        if not arrivals:
            continue
        live.append((city, hubs, arrivals))
        for d in leg2_dates:
            leg2_jobs.append((hubs[0], to_codes[0], d))
    leg2_res = engine.query_pairs(leg2_jobs)

    plans: list[Plan] = []
    for city, hubs, arrivals in live:
        departures: list[Train] = []
        for d in leg2_dates:
            departures.extend(_filter_trains(
                leg2_res.get((hubs[0], to_codes[0], d), []), set(hubs), set(to_codes)))
        if not departures:
            continue

        for t1 in arrivals:
            a1 = t1.arr_dt()
            if a1 is None:
                continue
            h1 = t1.to_code                      # 实际下车的那一站
            for t2 in departures:
                if t1.code == t2.code:           # 同一趟车不是换乘，防御性排除
                    continue
                d2 = t2.dep_dt()
                if d2 is None:
                    continue
                h2 = t2.from_code                # 实际换乘上车的那一站
                same = (h1 == h2)
                if not same and not allow_cross_station:
                    continue

                n1, n2 = table.name_of(h1), table.name_of(h2)
                floor = buffers.floor_for([n1, n2])
                if same:
                    inter_min, estimated = 0, False
                    required = max(min_transfer, floor)
                else:
                    inter_min, estimated = inter.lookup(city, n1, n2)
                    required = inter_min + max(min_transfer, floor) + cross_buffer

                gap = int((d2 - a1).total_seconds() // 60)
                floor_gap = max(0, required - near_miss) if near_miss > 0 else required
                if gap < floor_gap:
                    continue
                is_near = gap < required
                plans.append(Plan(
                    legs=[t1, t2],
                    transfers=[Transfer(
                        hub_city=city,
                        arrive_code=h1,
                        depart_code=h2,
                        same_station=same,
                        gap_min=gap,
                        required_min=required,
                        interstation_min=inter_min,
                        interstation_estimated=estimated,
                        near_miss=is_near,
                    )],
                    near_miss=is_near,
                ))
    return plans


def build_chain_plans(engine: QueryEngine,
                      from_codes: list[str],
                      to_codes: list[str],
                      chain_groups: list[tuple[str, list[str]]],
                      date_str: str,
                      table: StationTable,
                      inter: InterchangeTable,
                      buffers: TransferBufferTable,
                      min_transfer: int,
                      cross_buffer: int,
                      allow_cross_station: bool,
                      allow_overnight: bool,
                      near_miss: int = 0,
                      max_combinations: int = 5000) -> list[Plan]:
    """按显式 `--via A,B` 链组合三段车次。

    只支持「用户明确指定的链」，不做自动三段：自动三段会让请求量和组合数
    一起爆炸，而这个工具最怕的就是触发 12306 限流。
    每段查询一次城市对；后两段允许查到次日（--allow-overnight）。
    """
    next_date = (_parse_date(date_str) + timedelta(days=1)).isoformat() if allow_overnight else None  # type: ignore[operator]
    later_dates = [date_str] + ([next_date] if next_date else [])

    # segments[i] = (起点车站码, 终点车站码, 这段之后要换乘的城市)
    segments: list[tuple[list[str], list[str], str]] = []
    prev = from_codes
    for city, codes in chain_groups:
        segments.append((prev, codes, city))
        prev = codes
    segments.append((prev, to_codes, chain_groups[-1][0]))

    pools: list[list[Train]] = []
    for idx, (src_codes, dst_codes, _city) in enumerate(segments):
        dates = [date_str] if idx == 0 else later_dates
        jobs = [(src_codes[0], dst_codes[0], d) for d in dates]
        res = engine.query_pairs(jobs)
        pool: list[Train] = []
        for d in dates:
            pool.extend(_filter_trains(
                res.get((src_codes[0], dst_codes[0], d), []),
                set(src_codes), set(dst_codes)))
        pools.append(_dedupe_train_pool(pool))

    plans: list[Plan] = []

    def dfs(idx: int, legs: list[Train], transfers: list[Transfer]) -> None:
        if len(plans) >= max_combinations:
            return
        if idx == len(segments):
            plans.append(Plan(legs=list(legs), transfers=list(transfers),
                              near_miss=any(t.near_miss for t in transfers)))
            return

        for train in pools[idx]:
            if idx == 0:
                if train.arr_dt() is None:
                    continue
                legs.append(train)
                dfs(idx + 1, legs, transfers)
                legs.pop()
                continue

            prev_leg = legs[-1]
            if prev_leg.code == train.code:
                continue
            a1 = prev_leg.arr_dt()
            d2 = train.dep_dt()
            if a1 is None or d2 is None:
                continue
            h1, h2 = prev_leg.to_code, train.from_code
            same = (h1 == h2)
            if not same and not allow_cross_station:
                continue

            city = segments[idx - 1][2]
            n1, n2 = table.name_of(h1), table.name_of(h2)
            floor = buffers.floor_for([n1, n2])
            if same:
                inter_min, estimated = 0, False
                required = max(min_transfer, floor)
            else:
                inter_min, estimated = inter.lookup(city, n1, n2)
                required = inter_min + max(min_transfer, floor) + cross_buffer

            gap = int((d2 - a1).total_seconds() // 60)
            floor_gap = max(0, required - near_miss) if near_miss > 0 else required
            if gap < floor_gap:
                continue

            transfers.append(Transfer(
                hub_city=city,
                arrive_code=h1,
                depart_code=h2,
                same_station=same,
                gap_min=gap,
                required_min=required,
                interstation_min=inter_min,
                interstation_estimated=estimated,
                near_miss=gap < required,
            ))
            legs.append(train)
            dfs(idx + 1, legs, transfers)
            legs.pop()
            transfers.pop()

    dfs(0, [], [])
    return plans


def dedupe_and_rank(plans: list[Plan], limit: int, sort_key: str = "total",
                    seat_classes: set[str] | None = None) -> list[Plan]:
    seen: set[tuple] = set()
    unique: list[Plan] = []
    for p in plans:
        key = tuple((leg.code, leg.dep, leg.date) for leg in p.legs)
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    unique.sort(key=lambda x: _plan_sort_key(x, sort_key, seat_classes))
    return unique[:limit]


def load_hubs(path: Path) -> list[str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, dict):
        raw = raw.get("hubs", [])
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if isinstance(x, str)]


def _pick_hubs_geographically(table: StationTable, hub_names: list[str],
                              from_codes: list[str], to_codes: list[str],
                              limit: int) -> list[str]:
    """按「地理编码距离」挑选中转枢纽。

    12306 站点表里的城市编码是沿铁路地理顺序编排的：
      黑龙江 00xx → 吉林 01xx → 辽宁 02xx → 京津冀 03xx → 山西 04xx
      → 山东 06xx → 江苏/上海 07xx → 安徽 08xx → 浙江 09xx → 福建 10xx
      → 江西 11xx → 河南 12xx → 湖北 13xx → 湖南 14xx → 广东 15xx
      → 广西 16xx → 四川 17xx → 重庆 17xx → 贵州 18xx → 云南 19xx
      → 陕西 20xx → 甘肃 21xx → 青海 22xx → 宁夏 23xx → 新疆 24xx
    所以编码差可以作为「地理远近」的廉价代理。挑选规则：

      · 编码落在出发/到达城市【之间】的枢纽视为「在路上」，等距抽样；
      · 其余按到两端的编码距离取最近的若干个。

    这样短途会优先命中南京南 / 上海虹桥 这类真正的换乘点，
    长途则沿线路均匀取样，而不是把全国枢纽无脑查一遍
    （那会触发 12306 限流，实测 200+ 次查询会有约 20% 直接失败）。
    """
    def name_code(name: str) -> int | None:
        st = table.by_name.get(name)
        return _city_code_int(st)

    def tele_code(tele: str) -> int | None:
        return _city_code_int(table.by_telecode.get(tele))

    f, t = tele_code(from_codes[0]), tele_code(to_codes[0])
    if f is None or t is None:
        return hub_names[:limit]

    # 起讫城市自身的车站不当枢纽：那属于「起讫城市多站选择」，已由
    # from_codes / to_codes 展开处理，再当枢纽只会白跑查询。
    exclude_cities = {table.city_of(c) for c in list(from_codes) + list(to_codes)}
    hubs = []
    for n in hub_names:
        st = table.by_name.get(n)
        if st is None:
            continue
        if (st.city or n) in exclude_cities:
            continue
        c = _city_code_int(st)
        if c is not None:
            hubs.append((n, c))
    if not hubs:
        return []

    lo, hi = sorted((f, t))
    span = hi - lo
    inside = sorted([h for h in hubs if lo <= h[1] <= hi], key=lambda x: x[1])
    outside = sorted([h for h in hubs if not (lo <= h[1] <= hi)],
                     key=lambda x: min(abs(x[1] - lo), abs(x[1] - hi)))

    # 短途（同区域或邻近区域）：直接取编码上离两端最近的枢纽，
    # 结果就是南京南 / 上海虹桥这类真正的换乘点。
    if span <= 60:
        picked = [n for n, _ in inside] + [n for n, _ in outside]
        return picked[:limit]

    # 长途：沿线路「铺开」挑枢纽。但不能只靠均匀分段 —— 分段里若简单取
    # 编码最小的那个，会把紧邻起讫点的重要枢纽挤掉（实测：镇江→秦皇岛
    # 会漏掉南京，而南京恰恰是这条线最关键的中转点）。所以分两步：
    #   1) 先锁定「离两端最近」的若干枢纽，这两簇最重要；
    #   2) 剩余名额再按编码区间均匀分段，保证沿线路铺开。
    chosen: list[str] = []
    end_quota = max(1, limit // 4)
    for name, _ in sorted(hubs, key=lambda x: abs(x[1] - lo))[:end_quota]:
        if name not in chosen:
            chosen.append(name)
    for name, _ in sorted(hubs, key=lambda x: abs(x[1] - hi))[:end_quota]:
        if name not in chosen:
            chosen.append(name)

    remaining = max(0, limit - len(chosen))
    inside_left = [h for h in inside if h[0] not in chosen]
    if remaining and inside_left:
        buckets = max(1, min(remaining, len(inside_left)))
        width = span / buckets
        for b in range(buckets):
            seg_lo = lo + b * width
            seg_hi = lo + (b + 1) * width
            seg = [h for h in inside_left if seg_lo <= h[1] < seg_hi]
            if not seg and b == buckets - 1:
                seg = [h for h in inside_left if h[1] >= seg_lo]
            if seg and seg[0][0] not in chosen:
                chosen.append(seg[0][0])

    # 剩余名额补给两翼最近的枢纽（允许合理绕行）
    for name, _ in outside:
        if len(chosen) >= limit:
            break
        if name not in chosen:
            chosen.append(name)
    for name, _ in inside:
        if len(chosen) >= limit:
            break
        if name not in chosen:
            chosen.append(name)
    return chosen[:limit]


def _city_code_int(st: Station | None) -> int | None:
    if st is None:
        return None
    try:
        return int(st.city_code)
    except (TypeError, ValueError):
        return None


def _resolve_avoid(table: StationTable, avoid: list[str],
                   warnings: list[str]) -> tuple[set[str], set[str]]:
    """把 --avoid 展开成 (城市集合, 车站电报码集合)。"""
    cities: set[str] = set()
    codes: set[str] = set()
    for item in avoid:
        res = table.resolve(item)
        if res.kind == "none" or not res.stations:
            warnings.append(f"排除项「{item}」无法识别，已忽略")
            continue
        for st in res.stations:
            cities.add(st.city)
            codes.add(st.telecode)
    return cities, codes


def build_hub_groups(table: StationTable,
                     from_codes: list[str], to_codes: list[str],
                     via: list[str] | None,
                     hubs_path: Path,
                     auto_limit: int,
                     verbose: bool = False,
                     avoid: list[str] | None = None
                     ) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """返回 (枢纽组, 警告信息)。多个 --via 视为「备选中转站」，不是链。"""
    warnings: list[str] = []
    groups: list[tuple[str, list[str]]] = []
    used: set[str] = set()
    avoid_cities, avoid_codes = _resolve_avoid(table, avoid or [], warnings)
    exclude_cities = {table.city_of(c) for c in list(from_codes) + list(to_codes)}

    def add_group(city: str, codes: list[str]) -> None:
        codes = [c for c in codes if c not in used and c not in avoid_codes]
        if not codes:
            return
        used.update(codes)
        groups.append((city, codes))

    if via:
        for item in via:
            res = table.resolve(item)
            if res.kind == "none" or not res.stations:
                warnings.append(f"中转站「{item}」无法识别，已跳过")
                continue
            # 城市名（如「上海」）会展开成该城全部车站，从而支持同城异站换乘；
            # 具体站名（如「南京南」）则只用这一个站。
            city = res.city or res.stations[0].city or res.stations[0].name
            if city in exclude_cities:
                warnings.append(f"中转站「{item}」与出发/到达城市相同，已跳过")
                continue
            if city in avoid_cities:
                warnings.append(f"中转站「{item}」在 --avoid 列表中，已跳过")
                continue
            add_group(city, [s.telecode for s in res.stations])
    else:
        hub_names = load_hubs(hubs_path)
        if not hub_names:
            warnings.append("未找到枢纽表 references/hubs.json，自动模式无候选，请改用 --via")
        picked = _pick_hubs_geographically(table, hub_names, from_codes, to_codes,
                                           auto_limit)
        if verbose and picked:
            print(f"[枢纽] 按地理就近选出 {len(picked)} 个：{'、'.join(picked)}",
                  file=sys.stderr)
        for name in picked:
            st = table.by_name.get(name)
            if not st:
                continue
            # 同城车站一并纳入，这样「到南京南、从南京走」这类同城异站换乘也能算出来
            city = st.city or st.name
            if city in avoid_cities or st.telecode in avoid_codes:
                continue
            siblings = [s.telecode for s in table.city_stations(city)
                        if s.telecode not in avoid_codes] or [st.telecode]
            add_group(city, siblings)

    # 说明：不把出发/到达城市自身的车站当作枢纽 —— 起讫城市的多站选择
    # 已经由 from_codes / to_codes 展开处理，再当枢纽只会产生噪声方案。

    return groups, warnings


def build_chain_groups(table: StationTable,
                       from_codes: list[str], to_codes: list[str],
                       via: list[str],
                       avoid: list[str] | None = None
                       ) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """把 `--via A,B` 解析成有顺序的中转链（用于 --legs 3）。"""
    warnings: list[str] = []
    groups: list[tuple[str, list[str]]] = []
    avoid_cities, avoid_codes = _resolve_avoid(table, avoid or [], warnings)
    exclude_cities = {table.city_of(c) for c in list(from_codes) + list(to_codes)}

    for item in via:
        res = table.resolve(item)
        if res.kind == "none" or not res.stations:
            warnings.append(f"中转站「{item}」无法识别，已跳过")
            continue
        city = res.city or res.stations[0].city or res.stations[0].name
        if city in exclude_cities:
            warnings.append(f"中转站「{item}」与出发/到达城市相同，已跳过")
            continue
        if city in avoid_cities:
            warnings.append(f"中转站「{item}」在 --avoid 列表中，已跳过")
            continue
        codes = [s.telecode for s in res.stations if s.telecode not in avoid_codes]
        if not codes:
            warnings.append(f"中转站「{item}」的车站都被 --avoid 排除了，已跳过")
            continue
        if groups and groups[-1][0] == city:
            warnings.append(f"中转站「{item}」与上一个中转城市相同，已合并")
            groups[-1] = (city, list(dict.fromkeys(groups[-1][1] + codes)))
            continue
        groups.append((city, codes))
    return groups, warnings


def _station_label(table: StationTable, code: str) -> str:
    return table.name_of(code)


def render_text(res: dict, table: StationTable, verbose: bool = False) -> str:
    lines: list[str] = []
    src, dst, date_str = res["from"], res["to"], res["date"]
    lines.append(f"🚄 {src} → {dst}    {date_str}")
    lines.append(f"   最小换乘缓冲 {res['min_transfer']} 分钟"
                 f" · 跨站额外 +{res['cross_station_buffer']} 分钟"
                 f" · 查询 {res['queries']} 次"
                 f" · 排序 {res.get('sort', 'total')}")

    filters = res.get("filters") or {}
    bits: list[str] = []
    if filters.get("depart_after"):
        bits.append(f"发车 ≥{filters['depart_after']}")
    if filters.get("depart_before"):
        bits.append(f"发车 ≤{filters['depart_before']}")
    if filters.get("arrive_after"):
        bits.append(f"到达 ≥{filters['arrive_after']}")
    if filters.get("arrive_before"):
        bits.append(f"到达 ≤{filters['arrive_before']}")
    if filters.get("max_wait") is not None:
        bits.append(f"换乘等待 ≤{filters['max_wait']} 分")
    if filters.get("seat_classes"):
        bits.append("席别 " + "/".join(filters["seat_classes"]))
    if filters.get("near_miss"):
        bits.append(f"近似 ±{filters['near_miss']} 分")
    if filters.get("show_departed"):
        bits.append("含已发车")
    if bits:
        lines.append("   筛选：" + " · ".join(bits))
    if res.get("chain"):
        lines.append("   模式：三段换乘（显式 --via 链）")
    budget = res.get("budget") or {}
    if budget.get("exhausted"):
        lines.append(f"   ⚠ 请求预算 {budget.get('limit')} 次已耗尽，结果可能不完整")
    lines.append("")

    directs = res["direct"]
    lines.append("━━ 直达 ━━")
    if not directs:
        lines.append("   （无直达车）")
    else:
        for t in directs[:8]:
            lines.append(f"   {t['code']:<7} {t['from_name']} {t['dep']} → "
                         f"{t['to_name']} {t['arr']}  历时 {t['lishi']}  余票 {t['seats']}"
                         f"{_fmt_price(t.get('prices') or {})}")
        if len(directs) > 8:
            lines.append(f"   …… 另有 {len(directs) - 8} 趟直达车")
    lines.append("")

    plans = res["plans"]
    lines.append("━━ 中转换乘 ━━")
    if not plans:
        lines.append("   （没有满足换乘缓冲要求的方案）")
    else:
        for i, p in enumerate(plans, 1):
            total_txt = f"总耗时 {p['total']}"
            if p.get("min_total"):
                total_txt += f"   最低合计 {p['min_total']}"
            lines.append(f"{i:>2}. {total_txt}   换乘 {len(p.get('transfers') or [])} 次")
            legs = p.get("legs") or []
            transfers = p.get("transfers") or []
            for j, leg in enumerate(legs, 1):
                lines.append(f"    {j}) {leg['code']:<7} {leg['from_name']} "
                             f"{leg['dep']} → {leg['to_name']} {leg['arr']}"
                             f"   余票 {leg['seats']}"
                             f"{_fmt_price(leg.get('prices') or {})}")
                if j <= len(transfers):
                    tr = transfers[j - 1]
                    if tr["same_station"]:
                        hub_txt = f"{tr['hub_arrive']}（同站）"
                        extra = ""
                    else:
                        est = "（估算）" if tr.get("interstation_estimated") else ""
                        hub_txt = (f"{tr['hub_arrive']} → {tr['hub_depart']}"
                                   f"（异站 · {tr['hub_city']}）")
                        extra = f" · 站间通勤 {tr['interstation_min']} 分{est}"
                    ok = "✓" if tr["gap_min"] >= tr["required_min"] else "✗"
                    lines.append(f"       ↓ 缓冲 {tr['gap_min']} 分钟"
                                 f"（要求 ≥{tr['required_min']}）{ok}"
                                 f"   换乘 {hub_txt}{extra}")

    near = res.get("near_miss_plans") or []
    if near:
        lines.append("")
        lines.append("━━ 差一点点的方案（未达缓冲要求，仅供参考） ━━")
        for i, p in enumerate(near, 1):
            short = min((t["required_min"] - t["gap_min"]
                         for t in (p.get("transfers") or [])
                         if t["gap_min"] < t["required_min"]), default=0)
            lines.append(f"{i:>2}. 总耗时 {p['total']}   换乘 "
                         f"{len(p.get('transfers') or [])} 次   最紧处差 {short} 分钟")
            transfers = p.get("transfers") or []
            for j, leg in enumerate(p.get("legs") or [], 1):
                lines.append(f"    {j}) {leg['code']:<7} {leg['from_name']} "
                             f"{leg['dep']} → {leg['to_name']} {leg['arr']}"
                             f"   余票 {leg['seats']}")
                if j <= len(transfers):
                    tr = transfers[j - 1]
                    ok = "✓" if tr["gap_min"] >= tr["required_min"] else "✗"
                    lines.append(f"       ↓ 缓冲 {tr['gap_min']} 分钟"
                                 f"（要求 ≥{tr['required_min']}）{ok}")
    lines.append("")

    if res.get("warnings"):
        lines.append("⚠ 提示：")
        for w in res["warnings"]:
            lines.append(f"   - {w}")
        lines.append("")

    lines.append(f"状态：{res['status']} · {res['summary']}")
    if res.get("errors") and verbose:
        lines.append("查询异常明细：")
        for e in res["errors"][:10]:
            lines.append(f"   - {e}")
    return "\n".join(lines)


def render_dates_text(res: dict, table: StationTable, verbose: bool = False) -> str:
    lines: list[str] = []
    lines.append(f"🚄 {res['from']} → {res['to']}    多日扫描（{len(res['results'])} 天）")
    lines.append("")
    lines.append("   日期          直达  换乘  近似  最佳方案")
    for item in res["results"]:
        best = item["plans"][0] if item["plans"] else None
        near = len(item.get("near_miss_plans") or [])
        if best:
            trip = " → ".join(f"{leg['code']} {leg['dep']}" for leg in best["legs"])
            detail = f"{trip}（{best['total']}）"
            if best.get("min_total"):
                detail += f" {best['min_total']}"
        elif item["direct"]:
            t = item["direct"][0]
            detail = f"直达 {t['code']} {t['dep']}→{t['arr']}"
        else:
            detail = "无可用方案"
        lines.append(f"   {item['date']}  {len(item['direct']):>4}  "
                     f"{len(item['plans']):>4}  {near:>4}  {detail}")
    lines.append("")
    if res.get("warnings"):
        lines.append("⚠ 提示：")
        for w in res["warnings"]:
            lines.append(f"   - {w}")
        lines.append("")
    lines.append(f"状态：{res['status']} · {res['summary']}")
    return "\n".join(lines)


def outcome_exit_code(status: str) -> int:
    return {"success": 0, "partial_success": 0, "blocked": 2, "failure": 1}.get(status, 1)


# --- 子命令 ---------------------------------------------------------------
def cmd_detect(args) -> Outcome:
    out = Outcome()
    ev: list[str] = []

    browser = find_browser(args.browser_path)
    if browser:
        ev.append(f"浏览器：{browser}")
    else:
        ev.append("浏览器：未找到（可用 --browser-path 指定，或用 --set-browser-path 持久保存）")

    try:
        import websocket
        ev.append(f"websocket-client：已安装 {getattr(websocket, '__version__', '?')}")
        has_ws = True
    except ImportError:
        ev.append("websocket-client：缺失（python -m pip install websocket-client）")
        has_ws = False

    table, err = load_station_table(refresh=args.refresh_stations, verbose=True)
    if table:
        ev.append(f"站点表：{len(table.stations)} 个站，缓存 {_cache_dir() / 'station_name.js'}")
    else:
        ev.append(f"站点表：不可用（{err}）")

    inter_path = _script_ref("interchange-times.json")
    hubs_path = _script_ref("hubs.json")
    buffers_path = _script_ref("transfer-buffers.json")
    ev.append(f"通勤时间表：{'已加载' if inter_path.is_file() else '缺失'} {inter_path}")
    ev.append(f"枢纽表：{'已加载' if hubs_path.is_file() else '缺失'} {hubs_path}")
    ev.append(f"站点缓冲表：{'已加载' if buffers_path.is_file() else '缺失'} {buffers_path}")

    if not browser or not has_ws or not table:
        out.status = "failure"
        out.failure_category = FC_ENV if (not browser or not has_ws) else FC_ARTIFACT
        out.summary = "环境未就绪，无法查询。"
        out.next_action = "按 evidence 中提示补齐依赖后重跑 --detect"
    else:
        out.status = "success"
        out.summary = "环境就绪，可以开始查询。"
        out.next_action = "python scripts/rail_transfer.py --from <站> --to <站> --date <日期>"
    out.evidence = ev
    out.data = {"browser": browser, "websocket": has_ws,
                "stations": len(table.stations) if table else 0}
    return out


def _script_ref(name: str) -> Path:
    """定位 references/ 下的数据文件。

    源码运行时在 skill 目录；pip 安装后由 data-files 放到 share/rail-transfer。
    找不到时返回源码布局的首选路径，让上层报出可读的「缺失」提示。
    """
    candidates = [
        _SCRIPT_DIR.parent / "references" / name,
        _SCRIPT_DIR / "references" / name,
        Path(sys.prefix) / "share" / "rail-transfer" / "references" / name,
        Path.home() / ".local" / "share" / "rail-transfer" / "references" / name,
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def cmd_stations(args, table: StationTable) -> Outcome:
    out = Outcome()
    query = args.stations
    res = table.resolve(query)

    if res.kind == "none" or not res.stations:
        out.status = "blocked"
        out.failure_category = FC_AMBIGUOUS
        out.summary = f"无法把「{query}」解析成任何车站。"
        out.next_action = "换用站名全称（如「句容西」）或电报码重试"
        return out

    if res.kind == "station":
        st = res.stations[0]
        out.status = "success"
        out.summary = f"「{query}」→ 车站 {st.name}（{st.telecode}），所属城市 {st.city}。"
        out.evidence = [f"   {st.name}  电报码 {st.telecode}  拼音 {st.pinyin}"]
        out.data = {"city": st.city,
                    "stations": [{"name": st.name, "telecode": st.telecode}]}
        out.next_action = (f"python scripts/rail_transfer.py --from {st.name} "
                           f"--to <站> --date <日期>")
        return out

    head = (f"「{query}」→ 城市 {res.city}，共 {len(res.stations)} 个车站："
            if res.city else f"「{query}」匹配到 {len(res.stations)} 个车站：")
    lines = [head]
    for s in sorted(res.stations, key=lambda x: x.name):
        lines.append(f"   {s.name:<8} 电报码 {s.telecode}   拼音 {s.pinyin}")
    first = sorted(res.stations, key=lambda x: x.name)[0]
    # 站名与城市名重名时，光看列表容易卡死（再输一次还是列表），
    # 所以额外提示「XX站」和电报码两条出路。
    same_named = [s for s in res.stations if s.name == res.city]
    hint = ""
    if same_named:
        hint = (f"；若你要的就是与城市同名的「{same_named[0].name}」站，"
                f"写「{same_named[0].name}站」或电报码 {same_named[0].telecode} 即可")
    out.status = "blocked"
    out.failure_category = FC_AMBIGUOUS
    out.summary = (f"「{query}」对应 {len(res.stations)} 个车站，请选一个具体车站"
                   f"（或加 --all-stations 展开全查）。")
    out.evidence = lines
    out.data = {"city": res.city,
                "stations": [{"name": s.name, "telecode": s.telecode}
                             for s in res.stations]}
    out.next_action = f"用上面列出的站名重跑，例如 --from {first.name}{hint}"
    return out


def _resolve_endpoint(table: StationTable, text: str, label: str,
                      all_stations: bool) -> tuple[list[str], Outcome | None]:
    res = table.resolve(text)

    if res.kind == "none" or not res.stations:
        o = Outcome(status="blocked", failure_category=FC_AMBIGUOUS,
                    summary=f"无法解析{label}「{text}」。",
                    next_action=f"用 --stations {text} 查看候选车站，或直接写车站全称")
        return [], o

    if res.kind == "station":
        return [s.telecode for s in res.stations], None

    if not all_stations:
        lines = [f"{label}「{text}」对应 {len(res.stations)} 个车站，请指定具体车站："]
        for s in sorted(res.stations, key=lambda x: x.name):
            lines.append(f"   {s.name:<8} 电报码 {s.telecode}")
        first = sorted(res.stations, key=lambda x: x.name)[0]
        same_named = [s for s in res.stations if s.name == res.city]
        hint = ""
        if same_named:
            hint = (f"；若要的就是与城市同名的「{same_named[0].name}」站，"
                    f"写「{same_named[0].name}站」或电报码 {same_named[0].telecode}")
        o = Outcome(status="blocked", failure_category=FC_AMBIGUOUS,
                    summary=f"{label}「{text}」有 {len(res.stations)} 个候选车站，需要你先选定。",
                    evidence=lines,
                    next_action=(f"用上面某个站名重跑（如 {first.name}），"
                                 f"或加 --all-stations 一次性搜索全部 {len(res.stations)} 个站{hint}"))
        return [], o

    return [s.telecode for s in res.stations], None


def _ask_transfer_minutes(args) -> None:
    """--ask-transfer：在查询前询问最小换乘缓冲。

    提示写到 stderr，避免污染 --format json 的 stdout。
    `--min-transfer N --ask-transfer` 时，N 作为提示里的默认值。
    """
    if not getattr(args, "ask_transfer", False):
        return
    default = args.min_transfer
    prompt = (f"两趟车之间至少留多少分钟？"
              f"[默认 {default}；同城异站会再加站间通勤和 "
              f"{args.cross_station_buffer} 分钟] ")
    if sys.stdin is None:
        print(f"stdin 不可用，使用默认值 {default} 分钟。", file=sys.stderr)
        args.min_transfer = default
        return
    for _ in range(3):
        sys.stderr.write(prompt)
        sys.stderr.flush()
        raw = sys.stdin.readline()
        if raw == "":
            print(f"\n未收到输入，使用默认值 {default} 分钟。", file=sys.stderr)
            args.min_transfer = default
            return
        text = raw.strip()
        if not text:
            args.min_transfer = default
            print(f"使用默认值 {default} 分钟。", file=sys.stderr)
            return
        try:
            value = int(text)
        except ValueError:
            print("请输入一个整数分钟数。", file=sys.stderr)
            continue
        if value < 0:
            print("不能是负数。", file=sys.stderr)
            continue
        args.min_transfer = value
        print(f"使用 {value} 分钟。", file=sys.stderr)
        return
    print(f"多次输入无效，使用默认值 {default} 分钟。", file=sys.stderr)
    args.min_transfer = default


def _prepare_query(args, table: StationTable) -> tuple[dict | None, Outcome | None]:
    """解析站点、枢纽、席别与时间窗等与具体日期无关的部分。"""
    from_codes, err = _resolve_endpoint(table, args.from_station, "出发站", args.all_stations)
    if err:
        return None, err
    to_codes, err = _resolve_endpoint(table, args.to_station, "到达站", args.all_stations)
    if err:
        return None, err

    seat_classes, bad = _normalize_seat_classes(args.seat_class)
    if bad:
        return None, Outcome(
            status="failure", failure_category=FC_AMBIGUOUS,
            summary=f"无法识别的席别：{'、'.join(bad)}",
            next_action="可选席别：" + "、".join(SEAT_CLASS_FIELDS))

    for label, value in (("--depart-after", args.depart_after),
                         ("--depart-before", args.depart_before),
                         ("--arrive-after", args.arrive_after),
                         ("--arrive-before", args.arrive_before)):
        if value and _parse_hhmm(value) is None:
            return None, Outcome(
                status="failure", failure_category=FC_AMBIGUOUS,
                summary=f"{label} 时间格式不对：{value}",
                next_action="用 HH:MM，例如 08:00")

    if args.max_wait is not None and args.max_wait < 0:
        return None, Outcome(status="failure", failure_category=FC_AMBIGUOUS,
                             summary="--max-wait 不能是负数。")
    if args.near_miss < 0:
        return None, Outcome(status="failure", failure_category=FC_AMBIGUOUS,
                             summary="--near-miss 不能是负数。")
    for label, value in (("--max-queries", args.max_queries),
                         ("--min-transfer", args.min_transfer),
                         ("--cross-station-buffer", args.cross_station_buffer),
                         ("--auto-hubs", args.auto_hubs),
                         ("--max-results", args.max_results),
                         ("--price-limit", args.price_limit),
                         ("--max-days", args.max_days)):
        if value < 0:
            return None, Outcome(status="failure", failure_category=FC_AMBIGUOUS,
                                 summary=f"{label} 不能是负数。")
    if args.max_days < 1:
        return None, Outcome(status="failure", failure_category=FC_AMBIGUOUS,
                             summary="--max-days 至少为 1。")

    via = _split_csv(args.via) or None
    avoid = _split_csv(args.avoid) or None
    warnings: list[str] = []

    if args.legs == 3:
        if not via:
            return None, Outcome(
                status="blocked", failure_category=FC_AMBIGUOUS,
                summary="--legs 3 需要配合 --via 指定两个中转站。",
                next_action="例如 --legs 3 --via 南京南,徐州东")
        if len(via) != 2:
            return None, Outcome(
                status="blocked", failure_category=FC_AMBIGUOUS,
                summary=f"--legs 3 需要恰好两个 --via，当前给了 {len(via)} 个。",
                next_action="例如 --legs 3 --via 南京南,徐州东")
        hub_groups, w = build_chain_groups(table, from_codes, to_codes, via, avoid)
        warnings.extend(w)
        if len(hub_groups) != 2:
            return None, Outcome(
                status="blocked", failure_category=FC_AMBIGUOUS,
                summary="三段换乘的两个中转站没有全部解析成功。",
                evidence=warnings)
    else:
        hub_groups, w = build_hub_groups(
            table, from_codes, to_codes, via, _script_ref("hubs.json"),
            args.auto_hubs, verbose=args.verbose, avoid=avoid)
        warnings.extend(w)
        if not hub_groups and not via:
            warnings.append("自动模式没有找到可用枢纽，只返回直达结果")

    inter = InterchangeTable(_script_ref("interchange-times.json"))
    buffers = TransferBufferTable(_script_ref("transfer-buffers.json"))
    if getattr(args, "ignore_station_buffers", False):
        buffers.stations = {}
        buffers.default = 0

    if args.sort == "price" and not args.with_price:
        warnings.append("--sort price 需要配合 --with-price；没有票价时按总耗时排序")

    return {
        "from_codes": from_codes,
        "to_codes": to_codes,
        "from_names": [table.name_of(c) for c in from_codes],
        "to_names": [table.name_of(c) for c in to_codes],
        "hub_groups": hub_groups,
        "warnings": warnings,
        "inter": inter,
        "buffers": buffers,
        "seat_classes": seat_classes,
        "chain": args.legs == 3,
    }, None


def _day_bound(date_str: str, hhmm: str | None) -> datetime | None:
    """把「查询日期 + HH:MM」变成完整时刻；跨天到达按真实日期比较。"""
    day = _parse_date(date_str)
    minutes = _parse_hhmm(hhmm)
    if day is None or minutes is None:
        return None
    return datetime(day.year, day.month, day.day) + timedelta(minutes=minutes)


def _departure_ok(t: Train, args, today: str, now: datetime,
                  date_str: str) -> bool:
    dep_dt = t.dep_dt()
    if args.depart_after:
        lo = _day_bound(date_str, args.depart_after)
        if dep_dt is None or lo is None or dep_dt < lo:
            return False
    if args.depart_before:
        hi = _day_bound(date_str, args.depart_before)
        if dep_dt is None or hi is None or dep_dt > hi:
            return False
    if t.date == today and not args.show_departed:
        if dep_dt is not None and dep_dt < now:
            return False
    return True


def _arrival_ok(t: Train, args, date_str: str) -> bool:
    arr_dt = t.arr_dt()
    if args.arrive_after:
        lo = _day_bound(date_str, args.arrive_after)
        if arr_dt is None or lo is None or arr_dt < lo:
            return False
    if args.arrive_before:
        hi = _day_bound(date_str, args.arrive_before)
        if arr_dt is None or hi is None or arr_dt > hi:
            return False
    return True


def _train_matches_filters(t: Train, args, today: str, now: datetime,
                           date_str: str) -> bool:
    return (_departure_ok(t, args, today, now, date_str)
            and _arrival_ok(t, args, date_str))


def _plan_matches_filters(p: Plan, args, seat_classes: set[str],
                          today: str, now: datetime, date_str: str) -> bool:
    if not _departure_ok(p.legs[0], args, today, now, date_str):
        return False
    if not _arrival_ok(p.legs[-1], args, date_str):
        return False
    if args.max_wait is not None and any(tr.gap_min > args.max_wait for tr in p.transfers):
        return False
    if seat_classes and not all(leg.has_class(seat_classes) for leg in p.legs):
        return False
    if (args.require_seats or args.no_standing) and not all(leg.has_seat() for leg in p.legs):
        return False
    return True


def _execute_query(args, table: StationTable, setup: dict, date_str: str,
                   session: BrowserSession, cache: QueryCache,
                   budget: RequestBudget | None = None) -> tuple[dict, QueryEngine]:
    """在一个已有浏览器会话里执行一次完整查询，返回结果字典与引擎。"""
    budget = budget if budget is not None else RequestBudget(args.max_queries)
    engine = QueryEngine(session, table, concurrency=args.concurrency,
                         verbose=args.verbose, disk=cache, budget=budget,
                         price_ttl=args.price_cache_ttl,
                         use_availability_cache=not args.watch)
    warnings = list(dict.fromkeys(setup["warnings"]))
    today = _date.today().isoformat()
    now = datetime.now()
    hits_before = cache.hits if cache else 0
    seat_classes = setup["seat_classes"]

    direct_trains = search_direct(engine, setup["from_codes"], setup["to_codes"], date_str)
    direct_trains = [t for t in direct_trains
                     if _train_matches_filters(t, args, today, now, date_str)]
    if seat_classes:
        direct_trains = [t for t in direct_trains if t.has_class(seat_classes)]
    if args.require_seats or args.no_standing:
        direct_trains = [t for t in direct_trains if t.has_seat()]
    direct_trains = _sort_trains(direct_trains, args.sort)

    plans: list[Plan] = []
    if setup["hub_groups"]:
        if setup["chain"]:
            plans = build_chain_plans(
                engine, setup["from_codes"], setup["to_codes"], setup["hub_groups"],
                date_str, table, setup["inter"], setup["buffers"],
                args.min_transfer, args.cross_station_buffer,
                args.allow_cross_station, args.allow_overnight,
                near_miss=args.near_miss)
        else:
            plans = build_plans(
                engine, setup["from_codes"], setup["to_codes"], setup["hub_groups"],
                date_str, table, setup["inter"], setup["buffers"],
                args.min_transfer, args.cross_station_buffer,
                args.allow_cross_station, args.allow_overnight,
                near_miss=args.near_miss)

    plans = [p for p in plans
             if _plan_matches_filters(p, args, seat_classes, today, now, date_str)]
    near_plans = [p for p in plans if p.near_miss]
    plans = [p for p in plans if not p.near_miss]
    # --sort price 需要先拿到票价；先把候选池放宽到 price-limit，查完价再重排
    pre_limit = args.max_results
    if args.sort == "price" and args.with_price:
        pre_limit = max(args.max_results, args.price_limit)
    plans = dedupe_and_rank(plans, pre_limit, args.sort, seat_classes)
    near_plans = dedupe_and_rank(near_plans, pre_limit, args.sort, seat_classes)

    # 自检：主方案里任何一条都不允许低于所需缓冲
    violations = [p for p in plans
                  if any(tr.gap_min < tr.required_min for tr in p.transfers)]
    if violations:
        plans = [p for p in plans
                 if not any(tr.gap_min < tr.required_min for tr in p.transfers)]
        warnings.append(f"自检拦下 {len(violations)} 条不满足缓冲要求的方案（不应发生，请反馈）")

    price_calls = 0
    if args.with_price and args.price_limit > 0:
        wanted: list[Train] = []
        for plan in plans:
            wanted.extend(plan.legs)
        for plan in near_plans[:max(0, args.max_results // 2)]:
            wanted.extend(plan.legs)
        seen: set[tuple] = set()
        unique: list[Train] = []
        for t in wanted:
            key = (t.train_no, t.from_station_no, t.to_station_no, t.date)
            if key in seen:
                continue
            seen.add(key)
            unique.append(t)
        spare = max(0, args.price_limit - len(unique))
        if spare:
            unique.extend(direct_trains[:spare])
        price_calls = engine.query_prices(unique, limit=args.price_limit * 2)
        if args.sort == "price":
            direct_trains.sort(key=lambda t: (t.cheapest(), t.dep or ""))
            plans.sort(key=lambda p: _plan_sort_key(p, "price", seat_classes))
            near_plans.sort(key=lambda p: _plan_sort_key(p, "price", seat_classes))
            plans = plans[:args.max_results]
            near_plans = near_plans[:args.max_results]

    if args.with_price and args.price_limit <= 0:
        warnings.append("--price-limit 为 0：已跳过票价查询")
    if engine.budget_exhausted:
        warnings.append(f"请求预算 {args.max_queries} 次已耗尽，结果可能不完整；"
                        f"可调大 --max-queries 或减少 --auto-hubs")
    if engine.errors:
        total = max(1, engine.calls)
        ratio = len(engine.errors) / total
        if ratio > 0.15:
            warnings.append(
                f"{len(engine.errors)}/{total} 次子查询失败（{ratio:.0%}），"
                f"很像是被 12306 限流了：稍后重试、调小 --auto-hubs，"
                f"或直接用 --via 指定中转站会更稳")
        else:
            warnings.append(f"{len(engine.errors)} 次子查询失败，结果可能不完整")

    res = {
        "from": "/".join(setup["from_names"]),
        "to": "/".join(setup["to_names"]),
        "date": date_str,
        "min_transfer": args.min_transfer,
        "cross_station_buffer": args.cross_station_buffer,
        "sort": args.sort,
        "queries": engine.calls,
        "cache_hits": (cache.hits - hits_before) if cache else 0,
        "price_calls": price_calls,
        "warnings": warnings,
        "errors": engine.errors,
        "direct": [_train_json(t, table, not args.no_standing) for t in direct_trains[:40]],
        "plans": [_plan_json(p, table, seat_classes, not args.no_standing) for p in plans],
        "near_miss_plans": [_plan_json(p, table, seat_classes, not args.no_standing)
                            for p in near_plans],
        "filters": {
            "depart_after": args.depart_after,
            "depart_before": args.depart_before,
            "arrive_after": args.arrive_after,
            "arrive_before": args.arrive_before,
            "max_wait": args.max_wait,
            "seat_classes": sorted(seat_classes),
            "near_miss": args.near_miss,
            "show_departed": args.show_departed,
            "require_seats": args.require_seats,
            "no_standing": args.no_standing,
        },
        "chain": setup["chain"],
        "budget": {"limit": args.max_queries, "used": budget.used,
                   "exhausted": engine.budget_exhausted},
        "status": "",
        "summary": "",
    }

    if direct_trains or plans:
        res["status"] = "success"
        bits = []
        if direct_trains:
            bits.append(f"{len(direct_trains)} 趟直达")
        if plans:
            bits.append(f"{len(plans)} 条换乘方案")
        if near_plans:
            bits.append(f"{len(near_plans)} 条近似方案")
        res["summary"] = "、".join(bits) + f"（查询 {engine.calls} 次）"
        if engine.budget_exhausted:
            res["status"] = "partial_success"
            res["summary"] += "（请求预算耗尽，结果可能不完整）"
    elif near_plans:
        res["status"] = "partial_success"
        res["summary"] = ("没有满足缓冲要求的方案；下方列出的是差一点点、"
                          "仅供主人自行判断是否冒险的方案。")
    elif engine.errors and engine.calls and len(engine.errors) >= engine.calls:
        res["status"] = "failure"
        res["failure_category"] = FC_CONN
        res["summary"] = "全部查询失败，可能被 12306 风控或网络不通。"
    else:
        res["status"] = "partial_success"
        res["summary"] = (f"{date_str} 未找到直达或满足缓冲要求的换乘方案。"
                          f"可尝试放宽 --min-transfer、增加 --auto-hubs、"
                          f"调整时间窗，或用 --via 指定中转站。")
    return res, engine


def cmd_query(args, table: StationTable) -> Outcome:
    out = Outcome()

    if not args.date:
        args.date = (_date.today() + timedelta(days=1)).isoformat()

    dates = [args.date]
    if getattr(args, "dates", None):
        dates, derr = _expand_dates(args.dates)
        if derr:
            return Outcome(status="failure", failure_category=FC_AMBIGUOUS,
                           summary=derr,
                           next_action="例如 --dates 2026-10-20..2026-10-22")
        if not dates:
            return Outcome(status="failure", failure_category=FC_AMBIGUOUS,
                           summary="--dates 没有展开出任何日期。")
        if len(dates) > args.max_days:
            return Outcome(status="blocked", failure_category=FC_AMBIGUOUS,
                           summary=f"--dates 展开后有 {len(dates)} 天，"
                                   f"超过 --max-days {args.max_days}。",
                           next_action="缩小日期范围，或显式调大 --max-days（注意请求量）")
    elif not _parse_date(args.date):
        out.status = "failure"
        out.failure_category = FC_AMBIGUOUS
        out.summary = f"日期格式不对：{args.date}"
        out.next_action = "用 YYYY-MM-DD 格式，例如 --date 2026-10-20"
        return out

    setup, err = _prepare_query(args, table)
    if err:
        return err

    _ask_transfer_minutes(args)

    browser = find_browser(args.browser_path)
    if not browser:
        out.status = "failure"
        out.failure_category = FC_ENV
        out.summary = "找不到可用的浏览器（Edge/Chrome）。"
        out.next_action = "用 --browser-path 指定，或用 --set-browser-path 持久保存"
        return out

    cache = QueryCache(_cache_dir() / "query-cache.json", args.cache_ttl,
                       enabled=not args.no_cache)
    if cache.data and args.verbose:
        print(f"[缓存] 载入 {len(cache.data)} 条记录（余票 TTL {args.cache_ttl}s，"
              f"票价 TTL {args.price_cache_ttl}s）", file=sys.stderr)

    session = BrowserSession(browser, timeout=args.timeout, verbose=args.verbose)
    try:
        session.start()
    except RuntimeError as exc:
        session.close()
        out.status = "failure"
        out.failure_category = FC_CONN
        out.summary = f"启动浏览器失败：{exc}"
        out.next_action = "检查 Edge 是否可用；若 12306 风控，稍后重试或降低 --auto-hubs"
        return out

    budget = RequestBudget(args.max_queries)
    results: list[dict] = []
    try:
        for d in dates:
            res, _engine = _execute_query(args, table, setup, d, session, cache, budget)
            results.append(res)
    except Exception as exc:  # CDP / 网络 / 解析异常都收口成 failure，不抛 traceback
        return Outcome(status="failure", failure_category=FC_CONN,
                       summary=f"查询过程异常：{exc}",
                       next_action="稍后重试；若持续出现，请带上 --verbose 输出反馈")
    finally:
        session.close()
        cache.save()

    if len(results) == 1:
        res = results[0]
        out.status = res["status"]
        out.summary = res["summary"]
        out.data = res
        out.failure_category = res.get("failure_category")
        out.evidence = [
            f"查询次数 {res['queries']}（缓存命中 {res['cache_hits']} 条）",
            f"直达 {len(res['direct'])} 趟，换乘 {len(res['plans'])} 条，"
            f"近似 {len(res['near_miss_plans'])} 条",
        ]
        if res["price_calls"]:
            out.evidence.append(f"其中票价查询 {res['price_calls']} 次")
        if res["warnings"]:
            out.evidence.extend(res["warnings"][:5])
        out.next_action = ("python scripts/rail_transfer.py --from ... --to ... "
                           "--date ... --via <中转站>")
        return out

    # 多日扫描：汇总一个结果，JSON 里保留每天的完整数据
    warnings = list(dict.fromkeys(w for r in results for w in r["warnings"]))
    total_queries = sum(r["queries"] for r in results)
    total_prices = sum(r["price_calls"] for r in results)
    best_days = [r for r in results if r["direct"] or r["plans"]]
    if len(best_days) == len(results):
        status = "success"
    elif not best_days and all(r["status"] == "failure" for r in results):
        status = "failure"
    else:
        status = "partial_success"
    multi = {
        "from": "/".join(setup["from_names"]),
        "to": "/".join(setup["to_names"]),
        "dates": dates,
        "multi": True,
        "results": results,
        "warnings": warnings,
        "queries": total_queries,
        "price_calls": total_prices,
        "sort": args.sort,
        "min_transfer": args.min_transfer,
        "cross_station_buffer": args.cross_station_buffer,
        "status": status,
        "summary": f"扫描 {len(results)} 天，{len(best_days)} 天有可用方案"
                   f"（查询 {total_queries} 次）",
    }
    out.status = status
    out.summary = multi["summary"]
    out.data = multi
    if status == "failure":
        out.failure_category = FC_CONN
    out.evidence = [f"查询次数 {total_queries}"] + warnings[:5]
    out.next_action = ("python scripts/rail_transfer.py --from ... --to ... "
                       "--dates <范围> --via <中转站>")
    return out


def _collect_watch_matches(res: dict, codes: set[str]) -> list[dict]:
    codes = {c.upper() for c in codes}
    matches: list[dict] = []
    for t in res.get("direct") or []:
        if codes and t["code"].upper() not in codes:
            continue
        if not t.get("has_seat"):
            continue
        matches.append({
            "key": f"direct|{t['code']}|{t['from_name']}|{t['to_name']}|{t['dep']}|{t['date']}",
            "kind": "直达",
            "code": t["code"],
            "text": (f"{t['code']} {t['from_name']} {t['dep']}→"
                     f"{t['to_name']} {t['arr']} 余票 {t['seats']}"),
        })
    for p in res.get("plans") or []:
        legs = p.get("legs") or []
        if not legs or not all(leg.get("has_seat") for leg in legs):
            continue
        if codes and not any(leg["code"].upper() in codes for leg in legs):
            continue
        key = "plan|" + "|".join(
            f"{leg['code']}:{leg['dep']}:{leg['date']}" for leg in legs)
        text = "；".join(
            f"{leg['code']} {leg['from_name']} {leg['dep']}→{leg['to_name']} {leg['arr']}"
            for leg in legs)
        matches.append({"key": key, "kind": "换乘", "code": legs[0]["code"],
                        "text": text + f"（总耗时 {p['total']}）"})
    seen: set[str] = set()
    unique: list[dict] = []
    for m in matches:
        if m["key"] in seen:
            continue
        seen.add(m["key"])
        unique.append(m)
    return unique


def _notify(title: str, message: str, bell: bool = True) -> None:
    """尽力发一条桌面通知；失败也不影响监控。"""
    try:
        if os.name == "nt":
            script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "Add-Type -AssemblyName System.Drawing;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                "$n.Visible=$true;"
                "$n.ShowBalloonTip(8000,$env:RT_NOTIFY_TITLE,$env:RT_NOTIFY_MESSAGE,"
                "[System.Windows.Forms.ToolTipIcon]::Info);"
                "Start-Sleep -Milliseconds 800;$n.Dispose()"
            )
            env = dict(os.environ, RT_NOTIFY_TITLE=title, RT_NOTIFY_MESSAGE=message)
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", script],
                           env=env, timeout=10, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 f"display notification {json.dumps(message, ensure_ascii=False)} "
                 f"with title {json.dumps(title, ensure_ascii=False)}"],
                timeout=10, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["notify-send", title, message], timeout=10, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass
    if bell:
        print("\a", end="", flush=True)


def cmd_watch(args, table: StationTable) -> Outcome:
    if args.dates:
        return Outcome(status="failure", failure_category=FC_AMBIGUOUS,
                       summary="--watch 不能和 --dates 同时使用。")
    if args.interval < MIN_WATCH_INTERVAL:
        return Outcome(status="failure", failure_category=FC_AMBIGUOUS,
                       summary=f"--interval 不能小于 {MIN_WATCH_INTERVAL} 秒，"
                               f"否则很容易触发 12306 限流。")
    if args.watch_cycles < 0:
        return Outcome(status="failure", failure_category=FC_AMBIGUOUS,
                       summary="--watch-cycles 不能是负数（0 表示无限轮）。")

    if not args.date:
        args.date = (_date.today() + timedelta(days=1)).isoformat()
    if not _parse_date(args.date):
        return Outcome(status="failure", failure_category=FC_AMBIGUOUS,
                       summary=f"日期格式不对：{args.date}")

    setup, err = _prepare_query(args, table)
    if err:
        return err

    _ask_transfer_minutes(args)

    browser = find_browser(args.browser_path)
    if not browser:
        return Outcome(status="failure", failure_category=FC_ENV,
                       summary="找不到可用的浏览器（Edge/Chrome）。",
                       next_action="用 --browser-path 指定，或用 --set-browser-path 持久保存")

    cache = QueryCache(_cache_dir() / "query-cache.json", args.cache_ttl,
                       enabled=not args.no_cache)
    session = BrowserSession(browser, timeout=args.timeout, verbose=args.verbose)
    try:
        session.start()
    except RuntimeError as exc:
        session.close()
        return Outcome(status="failure", failure_category=FC_CONN,
                       summary=f"启动浏览器失败：{exc}")

    watch_codes = {c.upper() for c in _split_csv(args.watch_trains)}
    notified: set[str] = set()
    cycle = 0
    status = "success"
    try:
        while args.watch_cycles <= 0 or cycle < args.watch_cycles:
            cycle += 1
            stamp = datetime.now().strftime("%H:%M:%S")
            try:
                res, _engine = _execute_query(
                    args, table, setup, args.date, session, cache,
                    RequestBudget(args.max_queries))
            except Exception as exc:  # 单轮失败不终止整个监控
                status = "partial_success"
                print(f"[{stamp}] 第 {cycle} 轮查询失败：{exc}", file=sys.stderr)
            else:
                if res.get("status") != "success":
                    status = "partial_success"
                matches = _collect_watch_matches(res, watch_codes)
                new = [m for m in matches if m["key"] not in notified]
                for m in new:
                    notified.add(m["key"])

                if args.format == "json":
                    print(json.dumps({
                        "time": stamp, "cycle": cycle,
                        "status": res["status"],
                        "queries": res["queries"],
                        "budget": res["budget"],
                        "warnings": res["warnings"][:3],
                        "available": matches, "new": new,
                    }, ensure_ascii=False), flush=True)
                else:
                    print(f"[{stamp}] 第 {cycle} 轮：直达 {len(res['direct'])} 趟、"
                          f"换乘 {len(res['plans'])} 条、有票 {len(matches)} 项"
                          f"（新增 {len(new)}）", flush=True)
                    for m in new:
                        print(f"   🔔 {m['text']}", flush=True)

                if new and not args.no_notify:
                    _notify("12306 有票了",
                            "\n".join(m["text"] for m in new[:5]),
                            bell=args.format != "json")
            if args.watch_cycles > 0 and cycle >= args.watch_cycles:
                break
            time.sleep(max(MIN_WATCH_INTERVAL, args.interval))
    except KeyboardInterrupt:
        if args.format != "json":
            print("\n已停止监控。", flush=True)
    finally:
        session.close()
        cache.save()

    out = Outcome(status=status,
                  summary=f"余票监控结束：共 {cycle} 轮，累计发现 {len(notified)} 项有票。")
    out.data = {"watch": True, "cycles": cycle, "notified": len(notified)}
    return out


def _min_total_text(p: Plan, seat_classes: set[str] | None = None) -> str:
    """各段指定席别最低价之和；可能混席别，所以标注为「最低合计」。"""
    total = p.cheapest_price(seat_classes)
    if total >= 1e9:
        return ""
    return f"¥{total:.1f}"


def _train_json(t: Train, table: StationTable, show_standing: bool = True) -> dict:
    dep_dt, arr_dt = t.dep_dt(), t.arr_dt()
    return {
        "code": t.code,
        "from_name": table.name_of(t.from_code),
        "to_name": table.name_of(t.to_code),
        "start_name": table.name_of(t.start_code),
        "end_name": table.name_of(t.end_code),
        "dep": t.dep, "arr": t.arr, "lishi": t.lishi, "date": t.date,
        "dep_datetime": dep_dt.isoformat() if dep_dt else "",
        "arr_datetime": arr_dt.isoformat() if arr_dt else "",
        "seats": t.seats_text(show_standing),
        "has_seat": t.has_seat(),
        "prices": dict(t.prices) if t.prices else {},
    }


def _plan_json(p: Plan, table: StationTable,
               seat_classes: set[str] | None = None,
               show_standing: bool = True) -> dict:
    legs = [_train_json(leg, table, show_standing) for leg in p.legs]
    transfers = [{
        "hub_city": tr.hub_city,
        "hub_arrive": table.name_of(tr.arrive_code),
        "hub_depart": table.name_of(tr.depart_code),
        "same_station": tr.same_station,
        "gap_min": tr.gap_min,
        "required_min": tr.required_min,
        "interstation_min": tr.interstation_min,
        "interstation_estimated": tr.interstation_estimated,
        "near_miss": tr.near_miss,
    } for tr in p.transfers]
    first = transfers[0] if transfers else {}
    return {
        "legs": legs,
        "transfers": transfers,
        "total": _fmt_minutes(p.total_min()),
        "total_min": p.total_min(),
        "min_total": _min_total_text(p, seat_classes),
        "near_miss": p.near_miss,
        # 兼容旧版两段方案的字段
        "hub_city": first.get("hub_city", ""),
        "hub_arrive": first.get("hub_arrive", ""),
        "hub_depart": first.get("hub_depart", ""),
        "same_station": first.get("same_station", True),
        "gap_min": first.get("gap_min", 0),
        "required_min": first.get("required_min", 0),
        "interstation_min": first.get("interstation_min", 0),
        "interstation_estimated": first.get("interstation_estimated", False),
        "leg1": legs[0] if legs else {},
        "leg2": legs[-1] if legs else {},
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rail_transfer.py",
        description="火车票中转换乘查询（强制最小换乘缓冲 + 同城异站通勤时间）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--detect", action="store_true", help="探测运行环境（浏览器 / 依赖 / 数据文件）")
    p.add_argument("--from", dest="from_station", help="出发站：站名或城市名")
    p.add_argument("--to", dest="to_station", help="到达站：站名或城市名")
    p.add_argument("--date", help="乘车日期 YYYY-MM-DD（默认明天）")
    p.add_argument("--dates", help="多日扫描：YYYY-MM-DD..YYYY-MM-DD 或逗号分隔")
    p.add_argument("--max-days", type=int, default=DEFAULT_MAX_DAYS,
                   help=f"--dates 最多扫描多少天（默认 {DEFAULT_MAX_DAYS}）")
    p.add_argument("--via", help="指定中转站或城市，多个用逗号分隔；不给则自动挑选枢纽")
    p.add_argument("--legs", type=int, choices=[2, 3], default=2,
                   help="换乘段数：2=一次换乘（默认）；3=两次换乘，需 --via 两个站")
    p.add_argument("--avoid", help="排除作为中转的城市/车站，逗号分隔")
    p.add_argument("--min-transfer", type=int, default=DEFAULT_MIN_TRANSFER,
                   help=f"同站换乘最小缓冲分钟数（默认 {DEFAULT_MIN_TRANSFER}）")
    p.add_argument("--ask-transfer", action="store_true",
                   help="查询前从 stdin 询问最小换乘缓冲（可配合 --min-transfer 设默认值）")
    p.add_argument("--cross-station-buffer", type=int, default=DEFAULT_CROSS_BUFFER,
                   help=f"同城异站换乘的额外缓冲分钟数（默认 {DEFAULT_CROSS_BUFFER}）")
    p.add_argument("--ignore-station-buffers", action="store_true",
                   help="忽略 references/transfer-buffers.json 的站点加严值")
    p.add_argument("--allow-cross-station", dest="allow_cross_station",
                   action="store_true", default=True, help="允许同城异站换乘（默认允许）")
    p.add_argument("--no-cross-station", dest="allow_cross_station",
                   action="store_false", help="禁止同城异站换乘")
    p.add_argument("--allow-overnight", action="store_true", help="允许第二程跨到次日")
    p.add_argument("--require-seats", action="store_true", help="只保留有余票的方案")
    p.add_argument("--no-standing", action="store_true",
                   help="不把无座当作有票，并在输出中隐藏无座")
    p.add_argument("--seat-class", help="只保留含指定席别余票的车，如 二等座,硬卧")
    p.add_argument("--depart-after", metavar="HH:MM", help="只看发车不早于该时刻的方案")
    p.add_argument("--depart-before", metavar="HH:MM", help="只看发车不晚于该时刻的方案")
    p.add_argument("--arrive-after", metavar="HH:MM", help="只看到达不早于该时刻的方案")
    p.add_argument("--arrive-before", metavar="HH:MM", help="只看到达不晚于该时刻的方案")
    p.add_argument("--max-wait", type=int, metavar="MIN",
                   help="换乘等待上限（分钟），超过则剔除")
    p.add_argument("--show-departed", action="store_true",
                   help="查询当天时也显示已经发车的车次")
    p.add_argument("--near-miss", type=int, default=0, metavar="MIN",
                   help="额外列出差多少分钟以内的方案（默认 0 关闭）")
    p.add_argument("--sort", choices=["total", "arrive", "depart", "gap", "price"],
                   default="total", help="排序方式（默认 total；price 需 --with-price）")
    p.add_argument("--with-price", action="store_true",
                   help="附带票价（每趟车多一次请求，会明显增加被限流的风险）")
    p.add_argument("--price-limit", type=int, default=DEFAULT_PRICE_LIMIT,
                   help=f"--with-price 时最多为多少趟车查票价（默认 {DEFAULT_PRICE_LIMIT}；0 表示不查）")
    p.add_argument("--price-cache-ttl", type=int, default=DEFAULT_PRICE_CACHE_TTL,
                   help=f"票价磁盘缓存秒数（默认 {DEFAULT_PRICE_CACHE_TTL}）")
    p.add_argument("--auto-hubs", type=int, default=DEFAULT_AUTO_HUBS,
                   help=f"自动模式最多使用多少个枢纽城市（默认 {DEFAULT_AUTO_HUBS}）")
    p.add_argument("--max-results", type=int, default=10,
                   help="最多输出多少条换乘方案（默认 10）")
    p.add_argument("--all-stations", action="store_true",
                   help="城市名输入时展开为该城全部车站一起搜索")
    p.add_argument("--stations", help="只解析站点信息，不查询车票（可给城市名或站名）")
    p.add_argument("--refresh-stations", action="store_true", help="强制刷新站点表缓存")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"并发查询数（默认 {DEFAULT_CONCURRENCY}，调高更快但更容易被限流）")
    p.add_argument("--max-queries", type=int, default=0, metavar="N",
                   help="单次查询的请求硬上限（0 表示不限制）")
    p.add_argument("--no-cache", action="store_true",
                   help="忽略查询结果磁盘缓存，强制重新查询")
    p.add_argument("--cache-ttl", type=int, default=DEFAULT_CACHE_TTL,
                   help=f"余票缓存有效期秒数（默认 {DEFAULT_CACHE_TTL}）")
    p.add_argument("--watch", action="store_true",
                   help="持续监控余票，有票时提醒（需配合 --from / --to）")
    p.add_argument("--interval", type=int, default=DEFAULT_WATCH_INTERVAL,
                   help=f"监控间隔秒数（默认 {DEFAULT_WATCH_INTERVAL}，最小 {MIN_WATCH_INTERVAL}）")
    p.add_argument("--watch-cycles", type=int, default=0,
                   help="监控轮数（0 表示无限，Ctrl+C 停止）")
    p.add_argument("--watch-trains", help="只监控这些车次，逗号分隔")
    p.add_argument("--no-notify", action="store_true",
                   help="监控时只打印，不发桌面通知")
    p.add_argument("--timeout", type=int, default=30, help="单次 CDP 操作超时秒数（默认 30）")
    p.add_argument("--browser-path", help="浏览器可执行文件路径")
    p.add_argument("--set-browser-path", help="把浏览器路径写入全局工具配置后退出")
    p.add_argument("--format", choices=["text", "json"], default="text", help="输出格式")
    p.add_argument("-v", "--verbose", action="store_true", help="详细输出")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.set_browser_path:
        path = Path(args.set_browser_path)
        if not path.is_file():
            print(json.dumps({"status": "failure", "failure_category": FC_ENV,
                              "summary": f"文件不存在：{path}"},
                             ensure_ascii=False, indent=2))
            return 1
        written = set_tool_path(BROWSER_TOOL_KEY, str(path), global_=True)
        print(json.dumps({"status": "success",
                          "summary": f"已保存浏览器路径到 {written}",
                          "browser": str(path)}, ensure_ascii=False, indent=2))
        return 0

    if args.detect:
        out = cmd_detect(args)
        return _emit(out, args, None)

    table, err = load_station_table(refresh=args.refresh_stations, verbose=args.verbose)
    if not table:
        out = Outcome(status="failure", failure_category=FC_ARTIFACT,
                      summary=err,
                      next_action="检查网络后重试，或用 --refresh-stations 强制刷新")
        return _emit(out, args, None)

    if args.stations:
        out = cmd_stations(args, table)
        return _emit(out, args, table)

    if not args.from_station or not args.to_station:
        out = Outcome(status="failure", failure_category=FC_AMBIGUOUS,
                      summary="缺少 --from / --to 参数。",
                      next_action="例如：--from 句容西 --to 苏州 --date 2026-10-20")
        return _emit(out, args, table)

    if args.watch:
        out = cmd_watch(args, table)
        if args.format == "json":
            # 监控模式已经按轮输出了 JSONL，不再追加一个汇总对象
            return outcome_exit_code(out.status)
        return _emit(out, args, table)

    out = cmd_query(args, table)
    return _emit(out, args, table)


def _emit(out: Outcome, args, table: StationTable | None) -> int:
    if args.format == "json":
        payload = {
            "status": out.status,
            "summary": out.summary,
            "evidence": out.evidence,
            "next_action": out.next_action,
            "failure_category": out.failure_category,
            "data": out.data,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return outcome_exit_code(out.status)

    icon = {"success": "✅", "partial_success": "⚠️",
            "blocked": "🛑", "failure": "❌"}.get(out.status, "•")
    print(f"结果: {icon} {out.summary}")
    if out.evidence:
        print()
        for line in out.evidence:
            print(line)
    if out.failure_category:
        print(f"\n失败分类: {out.failure_category}")
    if out.next_action:
        print(f"下一步: {out.next_action}")

    if out.data and table is not None:
        if out.data.get("multi"):
            print()
            print(render_dates_text(out.data, table, verbose=args.verbose))
        elif out.data.get("plans") is not None:
            print()
            print(render_text(out.data, table, verbose=args.verbose))
    return outcome_exit_code(out.status)


if __name__ == "__main__":
    sys.exit(main())
