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
QUERY_RETRIES = 2

# 返回行（58 列，`|` 分隔）中本脚本用得到的下标，均为 2026-10 实测确认
IDX_TRAIN_CODE = 3        # 车次
IDX_START_STATION = 4     # 始发站电报码
IDX_END_STATION = 5       # 终到站电报码
IDX_FROM = 6              # 乘车站电报码
IDX_TO = 7                # 下车站电报码
IDX_DEPART = 8            # 发车时刻
IDX_ARRIVE = 9            # 到达时刻
IDX_LISHI = 10            # 历时
IDX_WZ = 26               # 无座
IDX_YZ = 30               # 硬座 / 二等（普速为硬座）
IDX_ZE = 31               # 二等座
IDX_ZY = 32               # 一等座
IDX_SWZ = 33              # 商务座

# 席别代码 → 中文名。9/M/O/WZ/1/3/4/6 为 2026-10 实测确认，
# 其余为 12306 通用代码，遇到不认识的会原样显示成「席别X」而不是瞎猜。
SEAT_NAMES = {
    "9": "商务座", "P": "特等座", "M": "一等座", "O": "二等座", "S": "商务座",
    "1": "硬座", "2": "软座", "3": "硬卧", "4": "软卧", "5": "包厢硬卧",
    "6": "高级软卧", "W": "高级动卧", "WZ": "无座",
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
    wz: str = ""
    yz: str = ""
    ze: str = ""
    zy: str = ""
    swz: str = ""
    # 查票价接口 queryTicketPrice 需要的四个参数（下标均于 2026-10 实测确认）
    train_no: str = ""
    from_station_no: str = ""
    to_station_no: str = ""
    seat_types: str = ""
    prices: dict[str, str] | None = None   # 席别代码 → 票价字符串；None 表示尚未查询

    def dep_dt(self) -> datetime | None:
        return _clock_to_dt(self.date, self.dep)

    def arr_dt(self) -> datetime | None:
        """到达时刻。到达钟点小于发车钟点时视为次日到达。"""
        d = _parse_date(self.date)
        dep_m = _clock_to_min(self.dep)
        arr_m = _clock_to_min(self.arr)
        if d is None or dep_m is None or arr_m is None:
            return None
        if arr_m < dep_m:
            d = d + timedelta(days=1)
        return datetime(d.year, d.month, d.day) + timedelta(minutes=arr_m)

    def seats_text(self) -> str:
        parts = []
        for label, val in (("商务", self.swz), ("一等", self.zy),
                           ("二等", self.ze), ("硬座", self.yz), ("无座", self.wz)):
            if val and val not in ("", "--", "*"):
                parts.append(f"{label}{val}")
        return " ".join(parts) if parts else "—"

    def has_seat(self) -> bool:
        for val in (self.swz, self.zy, self.ze, self.yz):
            if val and val not in ("", "--", "*", "无"):
                return True
        return False

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
class Plan:
    """一条换乘方案。"""
    leg1: Train
    leg2: Train
    hub_city: str
    same_station: bool
    gap_min: int
    required_min: int
    interstation_min: int = 0
    interstation_estimated: bool = False

    def total_min(self) -> int:
        a = self.leg1.dep_dt()
        b = self.leg2.arr_dt()
        if a is None or b is None:
            return 0
        return int((b - a).total_seconds() // 60)


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
    const idx = [3, 4, 5, 6, 7, 8, 9, 10, 26, 30, 31, 32, 33, 2, 16, 17, 35];
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
    """查询结果的短期磁盘缓存（按「出发城市|到达城市|日期」）。

    主人多半会反复微调参数（改 --min-transfer、换 --via、加 --all-stations）
    跑同一个出行日期，每次都重打 12306 既慢又容易触发限流，所以缓存一小段时间。

    余票是会变的动态数据，因此 TTL 默认只有 10 分钟，随时可用 --no-cache 绕过。
    缓存只用来省重复请求，命中与否都不影响换乘判定逻辑。
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
            if isinstance(raw, dict):
                now = time.time()
                self.data = {k: v for k, v in raw.items()
                             if isinstance(v, dict) and now - v.get("t", 0) < ttl}
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def get(self, key: tuple[str, str, str]) -> list | None:
        if not self.enabled:
            return None
        rec = self.data.get("|".join(key))
        if not rec or time.time() - rec.get("t", 0) >= self.ttl:
            return None
        rows = rec.get("rows")
        if not isinstance(rows, (list, dict)):
            return None
        self.hits += 1
        return rows

    def put(self, key: tuple[str, str, str], rows: list) -> None:
        if not self.enabled:
            return
        self.data["|".join(key)] = {"t": time.time(), "rows": rows}
        self.dirty = True

    def save(self) -> None:
        if not self.enabled or not self.dirty:
            return
        try:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False),
                                 encoding="utf-8")
            self.dirty = False
        except OSError:
            pass


class QueryEngine:
    """在同一个浏览器会话里批量查询，带并发上限与重试。

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
                 disk: QueryCache | None = None) -> None:
        self.session = session
        self.table = table
        self.concurrency = max(1, concurrency)
        self.verbose = verbose
        self.disk = disk
        self.cache: dict[tuple[str, str, str], list[Train]] = {}
        self.price_cache: dict[tuple, dict[str, str]] = {}
        self.errors: list[str] = []
        self.calls = 0
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
        """
        wanted: dict[tuple[str, str, str], tuple[str, str, str]] = {}
        for f, t, d in jobs:
            key = self._key(f, t, d)
            if key in self.cache or key in wanted:
                continue
            rows = self.disk.get(key) if self.disk else None
            if rows is not None:
                self.cache[key] = _parse_rows({"rows": rows}, f, t, d)
                continue
            wanted[key] = (f, t, d)

        pending = list(wanted.items())
        for attempt in range(QUERY_RETRIES + 1):
            failed: list[tuple[tuple[str, str, str], tuple[str, str, str], str]] = []
            limit = self.concurrency if attempt == 0 else max(1, self.concurrency // 2)
            for i in range(0, len(pending), limit):
                batch = pending[i:i + limit]
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
          · limit > 0 时只处理前 limit 趟，用来把请求量钉死在上限内
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
            for attempt in range(QUERY_RETRIES + 1):
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

            if reason:
                self.errors.append(f"票价 {train.code}: {reason}")
            train.prices = prices
            self.price_cache[key] = prices
            if self.disk:
                self.disk.put(disk_key, prices)
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
            wz=f[8], yz=f[9], ze=f[10],
            zy=f[11] if len(f) > 11 else "",
            swz=f[12] if len(f) > 12 else "",
            train_no=f[13] if len(f) > 13 else "",
            from_station_no=f[14] if len(f) > 14 else "",
            to_station_no=f[15] if len(f) > 15 else "",
            seat_types=f[16] if len(f) > 16 else "",
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


def build_plans(engine: QueryEngine,
                from_codes: list[str],
                to_codes: list[str],
                hub_groups: list[tuple[str, list[str]]],
                date_str: str,
                table: StationTable,
                inter: InterchangeTable,
                min_transfer: int,
                cross_buffer: int,
                allow_cross_station: bool,
                allow_overnight: bool) -> list[Plan]:
    """组合两段车次，产出满足换乘缓冲要求的方案。

    hub_groups 为 (城市名, [该城车站电报码]) 列表。由于接口按城市对返回，
    每个枢纽城市**只需要两次查询**（出发城市→枢纽城市、枢纽城市→到达城市），
    不管该城有多少个车站；同城异站换乘由行内自带的真实乘车站/下车站自然覆盖。
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
                d2 = t2.dep_dt()
                if d2 is None:
                    continue
                h2 = t2.from_code                # 实际换乘上车的那一站
                same = (h1 == h2)
                if not same and not allow_cross_station:
                    continue

                n1, n2 = table.name_of(h1), table.name_of(h2)
                if same:
                    inter_min, estimated = 0, False
                    required = min_transfer
                else:
                    inter_min, estimated = inter.lookup(city, n1, n2)
                    required = inter_min + min_transfer + cross_buffer

                gap = int((d2 - a1).total_seconds() // 60)
                if gap < required:
                    continue
                plans.append(Plan(
                    leg1=t1, leg2=t2, hub_city=city,
                    same_station=same,
                    gap_min=gap, required_min=required,
                    interstation_min=inter_min,
                    interstation_estimated=estimated,
                ))
    return plans


def dedupe_and_rank(plans: list[Plan], limit: int) -> list[Plan]:
    seen: set[tuple] = set()
    unique: list[Plan] = []
    for p in sorted(plans, key=lambda x: (x.total_min(), x.gap_min)):
        key = (p.leg1.code, p.leg1.dep, p.leg2.code, p.leg2.dep, p.leg2.date)
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique[:limit]


# --- 自动枢纽 -------------------------------------------------------------
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

    # 长途：把 [lo, hi] 均匀分段、每段取一个枢纽，保证沿线路「铺开」。
    # 不能只按枢纽表顺序等距抽样 —— 枢纽表本身地域分布不均（江浙沪偏多），
    # 那样会在长三角堆一堆小站，却漏掉郑州东、武汉、长沙南这类关键节点。
    chosen: list[str] = []
    buckets = max(1, min(limit, len(inside)))
    if buckets:
        width = span / buckets
        for b in range(buckets):
            seg_lo = lo + b * width
            seg_hi = lo + (b + 1) * width
            seg = [h for h in inside if seg_lo <= h[1] < seg_hi]
            if not seg and b == buckets - 1:
                seg = [h for h in inside if h[1] >= seg_lo]
            if seg:
                name = seg[0][0]
                if name not in chosen:
                    chosen.append(name)

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


def build_hub_groups(table: StationTable,
                     from_codes: list[str], to_codes: list[str],
                     via: list[str] | None,
                     hubs_path: Path,
                     auto_limit: int,
                     verbose: bool = False) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """返回 (枢纽组, 警告信息)。"""
    warnings: list[str] = []
    groups: list[tuple[str, list[str]]] = []
    used: set[str] = set()

    def add_group(city: str, codes: list[str]) -> None:
        codes = [c for c in codes if c not in used]
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
            siblings = [s.telecode for s in table.city_stations(city)] or [st.telecode]
            add_group(city, siblings)

    # 说明：不把出发/到达城市自身的车站当作枢纽 —— 起讫城市的多站选择
    # 已经由 from_codes / to_codes 展开处理，再当枢纽只会产生噪声方案。

    return groups, warnings


# --- 输出 -----------------------------------------------------------------
def _station_label(table: StationTable, code: str) -> str:
    return table.name_of(code)


def render_text(res: dict, table: StationTable, verbose: bool = False) -> str:
    lines: list[str] = []
    src, dst, date_str = res["from"], res["to"], res["date"]
    lines.append(f"🚄 {src} → {dst}    {date_str}")
    lines.append(f"   最小换乘缓冲 {res['min_transfer']} 分钟"
                 f" · 跨站额外 +{res['cross_station_buffer']} 分钟"
                 f" · 查询 {res['queries']} 次")
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
            if p["same_station"]:
                hub_txt = f"{p['hub_arrive']}（同站）"
                extra = ""
            else:
                est = "（估算）" if p["interstation_estimated"] else ""
                hub_txt = (f"{p['hub_arrive']} → {p['hub_depart']}"
                           f"（异站 · {p['hub_city']}）")
                extra = f" · 站间通勤 {p['interstation_min']} 分{est}"
            total_txt = f"总耗时 {p['total']}"
            if p.get("min_total"):
                total_txt += f"   最低合计 {p['min_total']}"
            lines.append(f"{i:>2}. {total_txt}   换乘 {hub_txt}{extra}")
            lines.append(f"    1) {p['leg1']['code']:<7} {p['leg1']['from_name']} "
                         f"{p['leg1']['dep']} → {p['leg1']['to_name']} {p['leg1']['arr']}"
                         f"   余票 {p['leg1']['seats']}{_fmt_price(p['leg1'].get('prices') or {})}")
            ok = "✓" if p["gap_min"] >= p["required_min"] else "✗"
            lines.append(f"       ↓ 缓冲 {p['gap_min']} 分钟（要求 ≥{p['required_min']}）{ok}")
            lines.append(f"    2) {p['leg2']['code']:<7} {p['leg2']['from_name']} "
                         f"{p['leg2']['dep']} → {p['leg2']['to_name']} {p['leg2']['arr']}"
                         f"   余票 {p['leg2']['seats']}{_fmt_price(p['leg2'].get('prices') or {})}")
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
    ev.append(f"通勤时间表：{'已加载' if inter_path.is_file() else '缺失'} {inter_path}")
    ev.append(f"枢纽表：{'已加载' if hubs_path.is_file() else '缺失'} {hubs_path}")

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
    return _SCRIPT_DIR.parent / "references" / name


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
    out.status = "blocked"
    out.failure_category = FC_AMBIGUOUS
    out.summary = (f"「{query}」对应 {len(res.stations)} 个车站，请选一个具体车站"
                   f"（或加 --all-stations 展开全查）。")
    out.evidence = lines
    out.data = {"city": res.city,
                "stations": [{"name": s.name, "telecode": s.telecode}
                             for s in res.stations]}
    out.next_action = f"用上面列出的站名重跑，例如 --from {first.name}"
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
        o = Outcome(status="blocked", failure_category=FC_AMBIGUOUS,
                    summary=f"{label}「{text}」有 {len(res.stations)} 个候选车站，需要你先选定。",
                    evidence=lines,
                    next_action=(f"用上面某个站名重跑（如 {first.name}），"
                                 f"或加 --all-stations 一次性搜索全部 {len(res.stations)} 个站"))
        return [], o

    return [s.telecode for s in res.stations], None


def cmd_query(args, table: StationTable) -> Outcome:
    out = Outcome()

    if not args.date:
        args.date = (_date.today() + timedelta(days=1)).isoformat()
    if not _parse_date(args.date):
        out.status = "failure"
        out.failure_category = FC_AMBIGUOUS
        out.summary = f"日期格式不对：{args.date}"
        out.next_action = "用 YYYY-MM-DD 格式，例如 --date 2026-10-20"
        return out

    from_codes, err = _resolve_endpoint(table, args.from_station, "出发站", args.all_stations)
    if err:
        return err
    to_codes, err = _resolve_endpoint(table, args.to_station, "到达站", args.all_stations)
    if err:
        return err

    from_names = [table.name_of(c) for c in from_codes]
    to_names = [table.name_of(c) for c in to_codes]

    via = [v for v in (args.via or "").split(",") if v.strip()] if args.via else None

    browser = find_browser(args.browser_path)
    if not browser:
        out.status = "failure"
        out.failure_category = FC_ENV
        out.summary = "找不到可用的浏览器（Edge/Chrome）。"
        out.next_action = "用 --browser-path 指定，或用 --set-browser-path 持久保存"
        return out

    hub_groups, warnings = build_hub_groups(
        table, from_codes, to_codes, via, _script_ref("hubs.json"),
        args.auto_hubs, verbose=args.verbose)

    if not hub_groups and not via:
        warnings.append("自动模式没有找到可用枢纽，只返回直达结果")

    inter = InterchangeTable(_script_ref("interchange-times.json"))

    cache = QueryCache(_cache_dir() / "query-cache.json", args.cache_ttl,
                       enabled=not args.no_cache)
    if cache.data and args.verbose:
        print(f"[缓存] 命中 {len(cache.data)} 个城市对（TTL {args.cache_ttl}s）",
              file=sys.stderr)

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

    price_calls = 0
    try:
        engine = QueryEngine(session, table, concurrency=args.concurrency,
                             verbose=args.verbose, disk=cache)

        # 直达
        direct_trains = search_direct(engine, from_codes, to_codes, args.date)

        if args.require_seats:
            direct_trains = [t for t in direct_trains if t.has_seat()]

        plans: list[Plan] = []
        if hub_groups:
            plans = build_plans(
                engine, from_codes, to_codes, hub_groups, args.date, table, inter,
                min_transfer=args.min_transfer,
                cross_buffer=args.cross_station_buffer,
                allow_cross_station=args.allow_cross_station,
                allow_overnight=args.allow_overnight)
            if args.require_seats:
                plans = [p for p in plans if p.leg1.has_seat() and p.leg2.has_seat()]
            plans = dedupe_and_rank(plans, args.max_results)

        # 票价（可选）。只补要展示的那几趟车，并把总量钉死在 --price-limit，
        # 因为票价是「每趟车一次请求」，不加限制很容易触发 12306 限流。
        if args.with_price:
            wanted: list[Train] = []
            for plan in plans[:args.price_limit]:
                wanted.append(plan.leg1)
                wanted.append(plan.leg2)
            spare = max(0, args.price_limit - len(wanted))
            if spare:
                wanted.extend(direct_trains[:spare])
            price_calls = engine.query_prices(wanted, limit=args.price_limit * 2)
        else:
            price_calls = 0
    finally:
        session.close()
        cache.save()

    # 自检：任何一条方案都不允许低于所需缓冲
    violations = [p for p in plans if p.gap_min < p.required_min]
    if violations:
        plans = [p for p in plans if p.gap_min >= p.required_min]
        warnings.append(f"自检拦下 {len(violations)} 条不满足缓冲要求的方案（不应发生，请反馈）")

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
        "from": "/".join(from_names),
        "to": "/".join(to_names),
        "date": args.date,
        "min_transfer": args.min_transfer,
        "cross_station_buffer": args.cross_station_buffer,
        "queries": engine.calls,
        "price_calls": price_calls,
        "warnings": warnings,
        "errors": engine.errors,
        "direct": [_train_json(t, table) for t in direct_trains[:40]],
        "plans": [_plan_json(p, table) for p in plans],
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
        res["summary"] = "、".join(bits) + f"（查询 {engine.calls} 次）"
    elif engine.errors and not engine.cache:
        res["status"] = "failure"
        res["summary"] = "全部查询失败，可能被 12306 风控或网络不通。"
        out.failure_category = FC_CONN
    else:
        res["status"] = "partial_success"
        res["summary"] = (f"{args.date} 未找到直达或满足缓冲要求的换乘方案。"
                          f"可尝试放宽 --min-transfer、增加 --auto-hubs，或用 --via 指定中转站。")
        out.failure_category = None

    out.status = res["status"]
    out.summary = res["summary"]
    out.data = res
    out.evidence = [f"查询次数 {engine.calls}（缓存命中 {cache.hits} 个城市对）",
                    f"直达 {len(direct_trains)} 趟，换乘 {len(plans)} 条"]
    if price_calls:
        out.evidence.append(f"其中票价查询 {price_calls} 次")
    if warnings:
        out.evidence.extend(warnings[:5])
    out.next_action = ("python scripts/rail_transfer.py --from ... --to ... "
                       "--date ... --via <中转站>")
    return out


def _min_total_text(p: Plan) -> str:
    """两段各自最便宜席别之和，仅作比价参考（可能混席别，所以标注为「最低合计」）。"""
    if not (p.leg1.prices and p.leg2.prices):
        return ""
    lo1, lo2 = p.leg1.cheapest(), p.leg2.cheapest()
    if lo1 >= 1e9 or lo2 >= 1e9:
        return ""
    return f"¥{lo1 + lo2:.1f}"


def _train_json(t: Train, table: StationTable) -> dict:
    return {
        "code": t.code,
        "from_name": table.name_of(t.from_code),
        "to_name": table.name_of(t.to_code),
        "start_name": table.name_of(t.start_code),
        "end_name": table.name_of(t.end_code),
        "dep": t.dep, "arr": t.arr, "lishi": t.lishi, "date": t.date,
        "seats": t.seats_text(),
        "prices": dict(t.prices) if t.prices else {},
    }


def _plan_json(p: Plan, table: StationTable) -> dict:
    return {
        "hub_city": p.hub_city,
        "hub_arrive": table.name_of(p.leg1.to_code),
        "hub_depart": table.name_of(p.leg2.from_code),
        "same_station": p.same_station,
        "gap_min": p.gap_min,
        "required_min": p.required_min,
        "interstation_min": p.interstation_min,
        "interstation_estimated": p.interstation_estimated,
        "total": _fmt_minutes(p.total_min()),
        "total_min": p.total_min(),
        "min_total": _min_total_text(p),
        "leg1": _train_json(p.leg1, table),
        "leg2": _train_json(p.leg2, table),
    }


# --- 入口 -----------------------------------------------------------------
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
    p.add_argument("--via", help="指定中转站或城市，多个用逗号分隔；不给则自动挑选枢纽")
    p.add_argument("--min-transfer", type=int, default=DEFAULT_MIN_TRANSFER,
                   help=f"同站换乘最小缓冲分钟数（默认 {DEFAULT_MIN_TRANSFER}）")
    p.add_argument("--cross-station-buffer", type=int, default=DEFAULT_CROSS_BUFFER,
                   help=f"同城异站换乘的额外缓冲分钟数（默认 {DEFAULT_CROSS_BUFFER}）")
    p.add_argument("--allow-cross-station", dest="allow_cross_station",
                   action="store_true", default=True, help="允许同城异站换乘（默认允许）")
    p.add_argument("--no-cross-station", dest="allow_cross_station",
                   action="store_false", help="禁止同城异站换乘")
    p.add_argument("--allow-overnight", action="store_true", help="允许第二程跨到次日")
    p.add_argument("--require-seats", action="store_true", help="只保留有余票的方案")
    p.add_argument("--with-price", action="store_true",
                   help="附带票价（每趟车多一次请求，会明显增加被限流的风险）")
    p.add_argument("--price-limit", type=int, default=DEFAULT_PRICE_LIMIT,
                   help=f"--with-price 时最多为多少趟车查票价（默认 {DEFAULT_PRICE_LIMIT}）")
    p.add_argument("--auto-hubs", type=int, default=DEFAULT_AUTO_HUBS,
                   help=f"自动模式最多使用多少个枢纽城市（默认 {DEFAULT_AUTO_HUBS}）")
    p.add_argument("--max-results", type=int, default=10, help="最多输出多少条换乘方案（默认 10）")
    p.add_argument("--all-stations", action="store_true",
                   help="城市名输入时展开为该城全部车站一起搜索")
    p.add_argument("--stations", help="只解析站点信息，不查询车票（可给城市名或站名）")
    p.add_argument("--refresh-stations", action="store_true", help="强制刷新站点表缓存")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"并发查询数（默认 {DEFAULT_CONCURRENCY}，调高更快但更容易被限流）")
    p.add_argument("--no-cache", action="store_true",
                   help="忽略查询结果磁盘缓存，强制重新查询")
    p.add_argument("--cache-ttl", type=int, default=DEFAULT_CACHE_TTL,
                   help=f"查询结果缓存有效期秒数（默认 {DEFAULT_CACHE_TTL}）")
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

    icon = {"success": "✅", "partial_success": "⚠️", "blocked": "🛑", "failure": "❌"}.get(out.status, "•")
    print(f"结果: {icon} {out.summary}")
    if out.evidence:
        print()
        for line in out.evidence:
            print(line)
    if out.failure_category:
        print(f"\n失败分类: {out.failure_category}")
    if out.next_action:
        print(f"下一步: {out.next_action}")

    # 查询成功时把完整方案渲染出来
    if out.data and out.data.get("plans") is not None and table is not None:
        print()
        print(render_text(out.data, table, verbose=args.verbose))
    return outcome_exit_code(out.status)


if __name__ == "__main__":
    sys.exit(main())
