# china-rail-transfer

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)

> 12306 火车票**中转换乘**查询工具 —— 自己算换乘，严格保证换乘缓冲，并把**同城异站**的通勤时间算进去。

**中文** | 一个 agent skill，也是能独立运行的命令行工具：给出发站、到达站和日期，
它先把两段车次拼起来，再按你指定的**最小换乘缓冲**与**同城异站通勤时间**逐条筛选，
只输出真的赶得上的方案。

<sub>An unofficial 12306 (China Railway) **transfer/connection planner**. Unlike the official
"中转换乘" feature, it enforces a user-configurable minimum transfer buffer and accounts for the
commute time between *different stations in the same city* — so it never suggests a connection
you cannot physically make. Ships as an agent skill with a standalone CLI. Docs are in Chinese.</sub>

---

## 目录

- [为什么需要它](#为什么需要它)
- [特性](#特性)
- [安装](#安装)
- [快速开始](#快速开始)
- [使用指南](#使用指南)
- [主要参数](#主要参数)
- [时间窗、排序与席别](#时间窗排序与席别)
- [余票监控（--watch）](#余票监控--watch)
- [多日扫描与三段换乘](#多日扫描与三段换乘)
- [请求预算、缓存与站点缓冲](#请求预算缓存与站点缓冲)
- [换乘可行性怎么判](#换乘可行性怎么判)
- [工作原理](#工作原理)
- [⚠️ 限流：使用前请务必了解](#️-限流使用前请务必了解)
- [项目结构](#项目结构)
- [常见问题](#常见问题)
- [已知限制](#已知限制)
- [免责声明](#免责声明)
- [测试与开发](#测试与开发)
- [贡献](#贡献)

---

## 为什么需要它

12306 官方的「中转换乘」有两个硬伤，导致它给出的方案经常**看着可行、实际误车**：

1. **换乘缓冲不可调** —— 官方按自己的规则给方案。你没法要求「上一趟车到达后至少留 16 分钟」。
2. **忽略同城异站通勤** —— 一个城市往往有十几个车站（苏州名下就有 14 个）。
   如果方案是「到上海虹桥、从上海站走」，中间那段地铁/打车时间官方是不算的，直接给你接上。

本工具把这两件事变成**硬性约束**，不满足的方案根本不会出现在结果里。

> 顺带一提：12306 官方的中转接口（`lcquery`）目前恒返回「系统忙」，本身就不工作。
> 所以换乘方案由本工具自行组合，而不是转发官方结果。

### 一个真实例子

`太仓南 → 句容`（2026-10-18）没有直达车。两条可行路线对比：

| | **经上海虹桥** | **经南京** |
|---|---|---|
| 最快总耗时 | **2 小时 35 分** | **5 小时** |
| 换乘类型 | 同站（都在上海虹桥） | **异站**（到南京站、从南京南走） |
| 额外成本 | 无 | 站间通勤 40 分，缓冲要求抬到 ≥61 分 |
| 可用时段 | 07:43 → 10:18 | 15:37 → 20:37 |
| 合计票价 | **¥145.0** | ¥178.5 |

经南京之所以亏，是因为**句容本身就在南沿江城际上，而南京在句容之外** —— 经南京等于先坐过头再折返，
**既慢两个半小时、又贵三十多块**。这类判断正是官方中转给不出来的。

---

## 特性

- **强制最小换乘缓冲**（主动询问主人，默认 16 分钟），并按车站再加严（大枢纽默认 18~20 分钟）
- **同城异站通勤时间**：内置 41 个主要城市的站间通勤表；表里没有的站对用保守默认值，
  并**明确标注「估算」**，不假装精确
- **自动挑选中转枢纽**：按 12306 站点表里的**城市编码地理顺序**就近挑选 ——
  短途命中南京南/上海虹桥这类真正的换乘点，长途沿线路均匀铺开
- **时间窗与排序**：`--depart-after/-before`、`--arrive-after/-before`、`--max-wait`，
  以及按总耗时 / 到达 / 发车 / 换乘余量 / 票价排序
- **席别筛选**：`--seat-class 二等座,硬卧`、`--no-standing`；指定席别时价格明细和合计都只统计该席别
- **近似方案**：`--near-miss 5` 单独列出「只差几分钟」的方案，由主人自己决定要不要冒险
- **排除中转城市**：`--avoid 北京`，不增加请求
- **请求预算**：`--max-queries 40` 硬上限，超出立即停止并标记 `partial_success`
- **三段换乘**：`--legs 3 --via A,B` 显式链式换乘（不做自动三段，避免请求爆炸）
- **多日扫描**：`--dates 2026-10-20..2026-10-22`，一次比较几天内的方案
- **余票监控**：`--watch` 定时刷新，有票时桌面通知（带最小间隔与每轮请求预算）
- **城市名 → 列出全部车站让你选**，不替你决定从哪个站走
- **余票一并返回**，支持 `--require-seats` 只留有票方案
- **票价查询（可选）**：`--with-price` 查各席别票价，逐程显示选定席别价格，
  并给出双程/全程合计；票价单独缓存一天，避免反复查价
- **短途磁盘缓存 + 自适应节流 + 失败补查 + 请求预算**，尽量避免触发 12306 限流
- 输出人读报告或 `--format json` 结构化结果；每条方案都打印缓冲余量

---

## 安装

### 安装前检查

先确认 Python 和 pip 可用：

```bash
python --version        # 需要 3.10+
python -m pip --version
```

Windows 上如果 `python` 不在 PATH，可改用 `py -3 --version`，并把下文命令里的 `python` 换成 `py -3`。
Linux / macOS 上如果只有 `python3`，把 `python` 换成 `python3`。

还要确认系统里有 **Edge 或 Chrome**。脚本必须驱动真实浏览器查询 12306，不能只用 curl；
`--detect` 会告诉你找到的是哪一个。

### 方式一：作为 agent skill（Claude Code 等）

```bash
git clone https://github.com/qijie233/china-rail-transfer.git \
  ~/.claude/skills/rail-transfer

python -m pip install websocket-client
```

skill 目录布局即本仓库布局，克隆进去即可被识别。
若你的 agent 使用别的 skills 目录，把仓库放到对应位置、目录名保持 `rail-transfer` 即可。

> 脚本会复用 `~/.claude/shared/tool_config.py`（如果存在）来持久保存浏览器路径；
> 没有这个文件也能正常工作，会退回到自动探测。

### 方式二：当独立命令行工具用

只要 Python 和 `websocket-client`，不需要任何 agent 框架：

```bash
git clone https://github.com/qijie233/china-rail-transfer.git
cd china-rail-transfer
python -m pip install websocket-client
python scripts/rail_transfer.py --detect
```

### 方式三：pip / pipx / uvx

装好后命令名是 `rail-transfer`，数据文件会安装到 `<prefix>/share/rail-transfer/references/`：

```bash
# 从源码目录安装，随后可直接调用
python -m pip install .
rail-transfer --detect

# 或者用 pipx / uvx 隔离安装
pipx install git+https://github.com/qijie233/china-rail-transfer.git
uvx --from git+https://github.com/qijie233/china-rail-transfer.git rail-transfer --detect
```

开发时用可编辑安装：

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
```

### 环境要求

| | |
| --- | --- |
| Python | **3.10+** |
| `websocket-client` | 唯一依赖，用于驱动无头浏览器 |
| 浏览器 | **Edge 或 Chrome**（必须是真实浏览器，原因见[工作原理](#工作原理)） |
| 系统 | Windows / macOS / Linux 均可，脚本自动探测常见浏览器路径 |

Windows 上如果浏览器不在常见位置，可以持久保存一次：

```bash
python scripts/rail_transfer.py --set-browser-path "D:/Edge/msedge.exe"
```

### 安装后验证

无论用哪种方式安装，都先跑一次自检：

```bash
# 源码 / agent skill
python scripts/rail_transfer.py --detect

# pip / pipx / uvx 安装
rail-transfer --detect
```

正常输出类似：

```
结果: ✅ 环境就绪，可以开始查询。

浏览器：C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe
websocket-client：已安装 1.9.2
站点表：3404 个站，缓存 C:\Users\...\rail-transfer\station_name.js
通勤时间表：已加载 ...\references\interchange-times.json
枢纽表：已加载 ...\references\hubs.json
站点缓冲表：已加载 ...\references\transfer-buffers.json
```

看到 `✅ 环境就绪` 即可开始查询。任何一项缺失，`下一步` 和 `失败分类` 会指出怎么补。

### 安装常见问题

| 现象 | 处理 |
| --- | --- |
| `No module named 'websocket'` | `python -m pip install websocket-client` |
| 找不到 Edge/Chrome | 用 `--browser-path "浏览器路径"` 临时指定，或 `--set-browser-path "浏览器路径"` 持久保存 |
| 浏览器启动后查询全失败 | 检查是否开着 Clash/VPN；脚本已加 `--no-proxy-server`，仍失败可暂时关闭系统代理 |
| agent 不识别 skill | 目录名必须是 `rail-transfer`，`SKILL.md` 在仓库根目录 |
| `pip install` 后找不到命令 | 确认 `<Python 安装目录>/Scripts` 在 PATH；也可用 `python -m rail_transfer_cli --help` 验证模块入口 |
| pip 安装后数据文件缺失 | 重新安装；数据文件会放到 `<prefix>/share/rail-transfer/references/` |

### 升级与卸载

```bash
# 源码安装：拉取后重新装开发依赖
git pull
python -m pip install -e ".[dev]"

# pip 安装
python -m pip install --upgrade .

# pipx 安装
pipx upgrade china-rail-transfer

# 卸载
python -m pip uninstall china-rail-transfer
pipx uninstall china-rail-transfer
```

---

## 快速开始

```bash
# 1. 环境自检
python scripts/rail_transfer.py --detect

# 2. 不确定站名？先看某城有哪些车站（不启动浏览器、不访问余票接口）
python scripts/rail_transfer.py --stations 苏州

# 3. 指定中转站查询（推荐：只发 2~4 次请求，最快最稳）
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 --via 南京

# 4. 让工具自动挑中转枢纽
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18

# 5. 结构化输出
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 --format json

# 6. 带票价（注意：每趟车多一次请求，见「限流」一节）
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --via 上海 --seat-class 二等座 --with-price

# 7. 时间窗 + 排序 + 席别
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --arrive-before 18:00 --seat-class 二等座 --sort arrive

# 8. 只看「差一点点」的方案
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --via 南京 --near-miss 5

# 9. 多日扫描
python scripts/rail_transfer.py --from 太仓南 --to 句容 \
    --dates 2026-10-18..2026-10-20 --via 上海

# 10. 余票监控（有票时桌面通知，Ctrl+C 停止）
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --via 上海 --watch --interval 180

# 11. 查询前询问换乘时间（人工使用；agent 也会先问主人）
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --via 上海 --ask-transfer
```

输出示例（`--with-price`）：

```
🚄 太仓南 → 句容    2026-10-18
   最小换乘缓冲 16 分钟 · 跨站额外 +5 分钟 · 查询 3 次 · 排序 total

━━ 直达 ━━
   （无直达车）

━━ 中转换乘 ━━
 1. 总耗时 2小时35分   合计 ¥145.0   换乘 1 次
    1) D3201   太仓南 07:43 → 上海虹桥 08:09   余票 一等有 二等有 无座有   二等座 ¥17.0
       ↓ 缓冲 25 分钟（要求 ≥20）✓   换乘 上海虹桥（同站）
    2) G3272   上海虹桥 08:34 → 句容 10:18   余票 商务3 一等13 二等有 无座有   二等座 ¥128.0
    ── 双程合计 ¥145.0（二等座 ¥17.0 + 二等座 ¥128.0）
 2. 总耗时 4小时28分   合计 ¥141.0   换乘 1 次
    1) C3805   太仓南 08:01 → 上海 08:30   余票 一等有 二等有 无座有   二等座 ¥14.0
       ↓ 缓冲 122 分钟（要求 ≥75）✓   换乘 上海 → 上海虹桥（异站 · 上海） · 站间通勤 50 分
    2) G7172   上海虹桥 10:32 → 句容 12:29   余票 商务1 一等无 二等有 无座有   二等座 ¥127.0
    ── 双程合计 ¥141.0（二等座 ¥14.0 + 二等座 ¥127.0）
```

跨站方案会明确标出「异站」、站间通勤时间和抬高后的缓冲要求。
注意示例里的「要求 ≥20」来自上海虹桥的站点缓冲；异站方案则是
站间通勤 50 + 站点缓冲 20 + 跨站额外 5 = 75 分钟。
指定 `--seat-class` 时，每程显示该席别最低价，合计只统计该席别；
未指定时每程显示全席别最低价并标出席别，合计可能混席别。

---

## 使用指南

这一节按「拿到需求 → 查询 → 读结果」的顺序说明。更细的参数见[主要参数](#主要参数)和各专题章节。

### 推荐流程

**agent 使用**：

1. 主人给出出发站、到达站和日期。
2. 若城市名对应多个车站，先把候选站列给主人选（`--stations`）。
3. **先问换乘时间**：「两趟车之间至少留多少分钟？默认 16 分钟；同城异站会再加站间通勤和 5 分钟。」
4. 用 `--min-transfer N` 查询；有明确中转站时加 `--via`，请求更少更稳。
5. 读报告里的「缓冲 X 分钟（要求 ≥Y）✓」；不理想再调缓冲、时间窗或中转站。

**人工命令行**：

```bash
# 最省事：查询前自动问换乘时间
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 --ask-transfer

# 已经想好：直接给 25 分钟
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 --min-transfer 25 --via 上海
```

### 场景速查

| 我想… | 加什么参数 |
| --- | --- |
| 只看直达 | 不给 `--via`，直接查 |
| 指定中转站 | `--via 南京南` |
| 指定中转城市（含同城异站） | `--via 上海` |
| 让工具自动挑枢纽 | 省略 `--via` |
| 城市有多个车站，先看清单 | `--stations 苏州` |
| 城市所有车站一起搜 | `--all-stations` |
| 手动设换乘时间 | `--min-transfer 25` |
| 查询前问我换乘时间 | `--ask-transfer` |
| 完全按手输值，不套站点加严 | `--min-transfer 10 --ignore-station-buffers` |
| 18 点前到 | `--arrive-before 18:00` |
| 早上 8 点后发车 | `--depart-after 08:00` |
| 换乘最多等 90 分钟 | `--max-wait 90` |
| 只要二等座 | `--seat-class 二等座 --require-seats` |
| 不要无座 | `--no-standing` |
| 按到达时间排序 | `--sort arrive` |
| 看只差几分钟的方案 | `--near-miss 5` |
| 不走北京中转 | `--avoid 北京` |
| 看三天内哪天最好 | `--dates 2026-10-18..2026-10-20` |
| 三段换乘 | `--legs 3 --via 南京南,徐州东` |
| 查票价 | `--with-price` |
| 监控余票 | `--watch --interval 180` |
| 只盯某个车次 | `--watch --watch-trains G1234` |
| 限制请求量 | `--max-queries 40` |
| 给程序读 | `--format json` |

### 输出怎么读

```
🚄 太仓南 → 句容    2026-10-18
   最小换乘缓冲 16 分钟 · 跨站额外 +5 分钟 · 查询 3 次 · 排序 total
```

- 「最小换乘缓冲」是主人给的值；「跨站额外」是异站换乘的额外余量。
- 「查询 3 次」是实际发往 12306 的请求数；缓存命中的不计入。

每条换乘方案：

```
 1. 总耗时 2小时35分   合计 ¥145.0   换乘 1 次
    1) D3201   太仓南 07:43 → 上海虹桥 08:09   余票 一等有 二等有 无座有   二等座 ¥17.0
       ↓ 缓冲 25 分钟（要求 ≥20）✓   换乘 上海虹桥（同站）
    2) G3272   上海虹桥 08:34 → 句容 10:18   余票 商务3 一等13 二等有 无座有   二等座 ¥128.0
    ── 双程合计 ¥145.0（二等座 ¥17.0 + 二等座 ¥128.0）
```

- 「总耗时」从第一程发车算到最后一程到达，包含换乘等待。
- 每程末尾的价格是选定席别的最低价；`--seat-class` 指定时只显示该席别。
- 「双程合计 / 全程合计」是各程价格之和，括号里列出每程的席别和价格明细。
- 没有指定 `--seat-class` 时，每程显示全席别最低价，合计可能混席别，只作比价参考。
- 「缓冲 X 分钟（要求 ≥Y）」是能否赶上的判据：`✓` 表示满足，`✗` 只出现在「差一点点」区块。
- 「要求」= `max(--min-transfer, 站点缓冲)`；异站还要加站间通勤和跨站额外。
- 「（估算）」表示站间通勤时间来自默认值，不是人工核实过的站对。

### JSON 输出与退出码

`--format json` 输出 Command Outcome Schema：

```json
{
  "status": "success",
  "summary": "...",
  "evidence": ["..."],
  "next_action": "...",
  "failure_category": null,
  "data": {
    "from": "...", "to": "...", "date": "...",
    "min_transfer": 16,
    "direct": [],
    "plans": [],
    "near_miss_plans": [],
    "filters": {},
    "budget": {"limit": 0, "used": 3, "exhausted": false},
    "chain": false
  }
}
```

- 每条方案有 `legs`（逐段）和 `transfers`（逐换乘点）；同时保留 `leg1` / `leg2` 等旧字段。
- 加 `--with-price` 后，每段有 `selected_seat` / `selected_price`；
  方案有 `total_price` / `total_price_detail` / `total_price_note`，`min_total` 为兼容字段。
- `near_miss_plans` 与 `plans` 分开。
- 多日扫描：`data.multi=true`，`data.results` 是每天的结果。
- 监控模式：每轮一行 JSONL，包含 `time` / `cycle` / `status` / `available` / `new`。

退出码：

| 状态 | 退出码 |
| --- | --- |
| `success` / `partial_success` | 0 |
| `failure` | 1 |
| `blocked` | 2 |

### 使用频率与安全

- 这是查询工具，不提供下单、支付、占座、抢票。
- 12306 会按累计请求量限流；优先用 `--via`，把自动枢纽数控制小，或用 `--max-queries` 设预算。
- 余票缓存默认 10 分钟，票价缓存默认 24 小时；要最新数据用 `--no-cache` / `--price-cache-ttl 0`。
- 长时间监控请 ≥3 分钟一轮，并用 `--watch-trains` 缩小范围。

## 主要参数

| 参数 | 说明 |
| --- | --- |
| `--from` / `--to` | 出发站、到达站（站名 / 城市名 / 拼音 / 电报码） |
| `--date` | 乘车日期 `YYYY-MM-DD`，默认明天 |
| `--via` | 指定中转站或城市，多个用逗号分隔；不给则自动挑 |
| `--min-transfer` | 同站换乘最小缓冲分钟数，**默认 16**；不主动给时会先询问 |
| `--ask-transfer` | 查询前从 stdin 询问最小换乘缓冲（可配合 `--min-transfer` 设默认值） |
| `--cross-station-buffer` | 异站换乘的额外缓冲，默认 5 |
| `--no-cross-station` | 完全不考虑同城异站换乘 |
| `--all-stations` | 城市名展开为该城全部车站一起搜 |
| `--auto-hubs` | 自动模式枢纽数上限，默认 16 |
| `--require-seats` | 只保留有余票的方案 |
| `--with-price` | 附带票价（**每趟车多一次请求**，默认关闭）；逐程显示价格与双程/全程合计 |
| `--price-limit` | `--with-price` 时最多为多少趟车查票价，默认 8 |
| `--allow-overnight` | 允许第二程跨到次日 |
| `--max-results` | 最多输出多少条方案，默认 10 |
| `--format json` | 输出结构化结果 |
| `--stations` | 只解析车站，不查票（不启动浏览器） |
| `--concurrency` | 并发查询数，默认 2 |
| `--no-cache` / `--cache-ttl` | 余票缓存控制（默认 600 秒） |
| `--price-cache-ttl` | 票价缓存秒数，默认 86400（公布票价基本稳定） |
| `--dates` / `--max-days` | 多日扫描范围 / 最多天数（默认 7） |
| `--legs {2,3}` | 换乘段数；`3` 需配合两个 `--via` 形成链 |
| `--avoid` | 排除作为中转的城市/车站，逗号分隔 |
| `--seat-class` | 席别筛选，如 `二等座,硬卧`；也接受 `卧铺`、`坐票` |
| `--no-standing` | 不把无座当作有票，并在输出中隐藏无座 |
| `--depart-after` / `--depart-before` | 只看该发车时间窗内的方案（`HH:MM`） |
| `--arrive-after` / `--arrive-before` | 只看该到达时间窗内的方案（`HH:MM`） |
| `--max-wait` | 换乘等待上限（分钟） |
| `--near-miss` | 额外列出差多少分钟以内的方案（默认 0 关闭） |
| `--sort` | `total` / `arrive` / `depart` / `gap` / `price` |
| `--max-queries` | 单次查询的请求硬上限（0 表示不限） |
| `--watch` / `--interval` / `--watch-cycles` / `--watch-trains` / `--no-notify` | 余票监控 |
| `--ignore-station-buffers` | 忽略站点级缓冲表，只用 `--min-transfer` |

完整说明见 [`references/usage.md`](references/usage.md)。

---

## 时间窗、排序与席别

```bash
# 18 点前到，只要二等座，按到达时间排序
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --arrive-before 18:00 --seat-class 二等座 --sort arrive

# 换乘等待最多 90 分钟，且只要卧铺
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --max-wait 90 --seat-class 卧铺
```

- 发车/到达时间窗作用于**整个行程**：发车看第一程，到达看最后一程。
  跨天到达按真实日期比较：次日 01:00 不算「当天 22:00 前到达」。
- `--sort price` 需要同时加 `--with-price`，否则没有票价可用，会退化为按总耗时排序。
- 指定 `--seat-class` 后，两段（或三段）都必须有该席别余票；
  「合计」只统计指定席别，不再混席别。
- `--near-miss 5` 会把「比要求少 5 分钟以内」的方案单独放在
  「差一点点的方案」区块里，**不会混进主方案**。

---

## 余票监控（`--watch`）

```bash
# 每 3 分钟查一次，有票就弹桌面通知；Ctrl+C 停止
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --via 上海 --watch --interval 180

# 只盯两个车次，跑 10 轮后自动结束
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --via 上海 --watch --watch-trains D3201,G3272 --watch-cycles 10

# 不给 --via 也可以，让工具每轮自动挑枢纽
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 --watch
```

- 监控模式**每轮都绕过余票缓存**（票价缓存仍然保留），看到的是最新余票。
- `--interval` 最小 **60 秒**；默认 180 秒。再快非常容易触发 12306 限流。
- `--max-queries` 在监控模式下是**每轮**的请求预算。
- 有票判定：直达车看该车次是否有座/卧票；换乘方案要求**每一段**都有座/卧票。
- 桌面通知是尽力而为：Windows 用 PowerShell 气泡，macOS 用 `osascript`，
  Linux 用 `notify-send`；没有通知环境时仍会打印 `🔔` 行。
- `--watch-trains` 只关注列出的车次（大小写不敏感）。

---

## 多日扫描与三段换乘

```bash
# 三天内哪天方案最好
python scripts/rail_transfer.py --from 太仓南 --to 句容 \
    --dates 2026-10-18..2026-10-20 --via 上海
```

- 每一天都会完整执行一次查询；请求量按天数线性增长。
  `--max-days`（默认 7）是硬上限，`--max-queries` 可以再压总预算。
- JSON 输出里 `data.results` 是每天的完整结果，`data.dates` 是扫描的日期列表。
- 加 `--with-price` 时，票价查询也是**每天各一份**；建议配合 `--max-queries` 使用。

```bash
# 三段换乘：出发 → 南京南 → 徐州东 → 到达
python scripts/rail_transfer.py --from 某小站 --to 另一小站 --date 2026-10-18 \
    --legs 3 --via 南京南,徐州东
```

- 三段换乘只支持**显式 `--via` 链**，不做自动三段：组合数和请求量都会爆炸。
- 两个 `--via` 按顺序形成链；每一段都独立检查最小缓冲、站点缓冲和异站通勤。
- 每段默认只查当天；加 `--allow-overnight` 后后两段会额外查次日。

---

## 请求预算、缓存与站点缓冲

- `--max-queries N`：一次命令最多发 N 次真实请求（含重试与票价）。
  超出后立即停止未完成的查询，结果标记为 `partial_success`，
  并在报告里写明「请求预算耗尽」。对 agent 自动调用尤其有用。
- **余票缓存**：默认 600 秒，键为「出发城市|到达城市|日期」，用 `--cache-ttl` 调整。
- **票价缓存**：单独用 `--price-cache-ttl`（默认 86400 秒）保存。
  公布票价基本稳定，没必要每次重查；想看最新价可以设成 0 或加 `--no-cache`。
- **站点级缓冲**：[`references/transfer-buffers.json`](references/transfer-buffers.json)
  给大枢纽设置 18~20 分钟的加严值。最终要求是
  `max(--min-transfer, 站点缓冲)`；查不到的车站不额外加严，仍按 `--min-transfer`（默认 16 分钟）。
  这份表是人工保守估计，不是官方数据；`--ignore-station-buffers` 可完全忽略。

---

## 换乘可行性怎么判

这是本工具与官方中转最大的差别，规则是硬性的：

```
同站换乘：  leg2.发车 − leg1.到达  ≥  max(min_transfer, 站点缓冲)
异站换乘：  leg2.发车 − leg1.到达  ≥  站间通勤 + max(min_transfer, 站点缓冲) + cross_station_buffer
```

- `--min-transfer`：默认 **16 分钟**，但**不会默默使用**：agent 会在拿到出发/到达站后先问主人；
  命令行人工使用可加 `--ask-transfer`。主人说「默认」时才按 16 分钟算。
- **站点缓冲**：大枢纽默认 18~20 分钟（见 `references/transfer-buffers.json`），
  与 `--min-transfer` 取较大值；`--ignore-station-buffers` 可忽略
- `--cross-station-buffer`：默认 **5 分钟**，给找站、安检排队留的额外余量
- 站间通勤时间来自 [`references/interchange-times.json`](references/interchange-times.json)，
  查不到的站对用 `_default`（60 分钟）**保守估计，并在输出里标注「估算」**
- 脚本内部还有一道自检，任何低于要求的方案都不会输出

---

## 工作原理

### ⚠️ 关键约束：为什么必须用真实浏览器

余票接口 `leftTicket/queryG` 有两条硬性要求，缺一不可：

1. **参数必须是 `purpose_codes=ADULT`**。用老的 `purpose_corporation=0`
   会被**静默 302** 到 `error.html`，而且**不返回任何错误信息** —— 非常难排查。
2. **必须从真实浏览器页面上下文发起请求**。curl / 裸 HTTP 请求一律被 302；
   伪造 `RAIL_DEVICEID`、补 `X-Requested-With`、改用非无头模式，**统统无效**。

所以本工具的做法是：启动一个无头 Edge/Chrome，通过 DevTools 协议（CDP）打开
`https://kyfw.12306.cn/otn/leftTicket/init` 建立会话，再**在页面内**用 `fetch` 发查询。

> Windows 上如果开着 Clash 之类的系统代理，必须给浏览器加 `--no-proxy-server`，
> 否则浏览器自动化会整体失效（脚本已内置该参数）。

### 接口其实是按「城市对」返回的

这是个反直觉但很有用的性质：查询「句容西→苏州」时，返回的行里乘车站既有**句容西**也有**句容**，
下车站覆盖**张家港/常熟/太仓/苏州南**等。实测 `JWH→SZH`、`JWH→ZAU`、`JRU→SZH`
三个不同站点对返回的集合**完全一致**。

也就是说，**每一行自带的乘车站/下车站（下标 6/7）才是准的**，查询参数只是个「城市对入口」。

好处：查询量从「出发站 × 到达站」降到「出发城市 × 到达城市」。
本项目据此设计，所以指定中转站时通常只需 2~4 次请求。

坏处：如果直接按查询参数去解析结果，同一趟车会重复出现 N 次（N = 目的城市车站数）。
本项目按行内实际站点过滤并去重。

### 自动挑枢纽：用城市编码当"地理坐标"

12306 站点表里的城市编码是沿铁路地理顺序编排的：

```
黑龙江 00xx → 吉林 01xx → 辽宁 02xx → 京津冀 03xx → 山西 04xx → 山东 06xx
→ 江苏/上海 07xx → 安徽 08xx → 浙江 09xx → 福建 10xx → 江西 11xx → 河南 12xx
→ 湖北 13xx → 湖南 14xx → 广东 15xx → 广西 16xx → 四川/重庆 17xx → 贵州 18xx
→ 云南 19xx → 陕西 20xx → 甘肃 21xx → 青海 22xx → 宁夏 23xx → 新疆 24xx
```

于是编码差可以作为「地理远近」的廉价代理：

- **短途**（跨度 ≤ 60）：取编码上离两端最近的枢纽。句容西→苏州 会挑出
  南京、上海、常州、无锡、镇江 —— 正是真正管用的换乘点。
- **长途**：把编码区间均匀分段、每段取一个，保证沿线路铺开，
  不会被枢纽表本身的地域偏斜带跑（否则会在长三角堆一堆小站、却漏掉郑州东/武汉）。

### 官方中转接口是坏的

`lcquery/queryG` 即使参数完全正确也恒返回 `{"status":false,"errorMsg":"系统忙"}`。

### 票价接口可用

`leftTicket/queryTicketPrice` 的参数**全部取自余票行**，实测下标：

| 参数 | 取自余票行下标 |
| --- | --- |
| `train_no` | `[2]` |
| `from_station_no` | `[16]` |
| `to_station_no` | `[17]` |
| `seat_types` | `[35]` |

返回体有两种写法，两种都要认：

```json
{"A1":"¥32.5","1":"325","A3":"¥78.5","3":"785","WZ":"¥32.5","OT":[]}   // 普速：A 前缀是显示价，裸键是 ×10 的整数
{"M":"¥124.0","O":"¥77.0","WZ":"¥77.0"}                                // 动车：直接就是带 ¥ 的显示价
```

优先采用带 ¥ 的显示值，避免自己做除法引入误差。`OT` 是备注数组
（如 `["混编硬座: ¥32.5"]`），`train_no` 是回显，两者都要跳过。

### 站点表是静态资源

`station_name.js` 不受风控影响，可以直接下载解析（缓存 7 天）。
而另一个看起来有用的 `train_list.js`（15MB）数据冻结在 2022-08，是废弃文件，不要用。

---

## ⚠️ 限流：使用前请务必了解

12306 会对**累计请求量**做限制。实测现象：

- 单次 **3~30 次**查询：稳定正常。
- 单次 **100 次以上**查询：出现大量 `Failed to fetch`（约 20%），
  而且**封锁会持续到整机 IP 层面** —— 期间连全新浏览器会话的单次查询也会失败，
  需要等几分钟才恢复。
- 并发数不是主因：并发降到 1 也仍有约 20% 失败。

因此默认值刻意保守：`--auto-hubs 16`、`--concurrency 2`、失败自动补查两次、
自适应节流（一失败就放慢、顺利再提速）、以及 600 秒的短期结果缓存。

**建议**：能确定中转站时就用 `--via`（只发 2~4 次请求，几乎不可能触发限流）。
自动模式留给"不知道在哪中转"的时候。

`--with-price` 也是"每趟车一次请求"，请按需使用并配合 `--price-limit` 收窄。

---

## 项目结构

```
china-rail-transfer/
├── SKILL.md                          # agent skill 主文档（触发条件、执行步骤、失败分类）
├── README.md                         # 本文件
├── LICENSE                           # MIT
├── pyproject.toml                    # 打包、pytest、ruff 配置
├── rail_transfer_cli.py              # pip 安装后的 console-script 入口
├── agents/
│   └── openai.yaml                   # 部分 agent 平台的元数据
├── references/
│   ├── usage.md                      # 详细用法、接口坑位、FAQ
│   ├── hubs.json                     # 自动模式的枢纽候选（122 个，按站名）
│   ├── interchange-times.json        # 同城站间通勤时间（41 城）
│   └── transfer-buffers.json         # 站点级同站换乘缓冲（人工保守估计）
├── scripts/
│   ├── __init__.py                   # 让源码布局可被 pytest / pip 导入
│   └── rail_transfer.py              # 核心实现，单文件（标准库 + websocket-client）
├── tests/                            # pytest 单元测试（不需要浏览器）
└── .github/workflows/ci.yml          # Linux/Windows + Python 3.10/3.13
```

### 数据文件说明

- **`hubs.json`** —— 自动模式的中转枢纽候选，**按站名**书写，运行时从站点表解析电报码，
  避免硬编码未核实的电报码。顺序即优先级；`--auto-hubs N` 决定实际使用数量。
- **`interchange-times.json`** —— 同城异站通勤时间（分钟），
  键写作 `"站A|站B"`，脚本自动双向匹配。
  数值是**从下车出站到抵达下一站进站口**的保守估计：以地铁为主、含步行与安检排队，
  取偏慢的常见值，不含极端拥堵。表里没有的站对一律按 60 分钟估算并标注「估算」。
- **`transfer-buffers.json`** —— 站点级同站换乘缓冲（分钟）。
  大枢纽取 18~20 分钟，查不到的车站不额外加严，仍按 `--min-transfer`（默认 16 分钟）；
  最终要求与 `--min-transfer` 取较大值。

这三个文件都是**人工整理**的，非常欢迎按你的实际体验提 PR 修正（也欢迎补测试）。

---

## 常见问题

**Q：为什么 `--from 苏州` 不直接开查？**
A：因为苏州名下 14 个站，脚本不替你决定。先 `--stations 苏州` 看清单，再写具体站名；
确认要全城搜时加 `--all-stations`。

**Q：结果里一条换乘都没有？**
A：先看报告里的 `⚠ 提示`。若是限流，稍后重试或改用 `--via`；若确实没有可行组合，
可以放宽 `--min-transfer`、加 `--allow-overnight`，或用 `--via` 指定别的中转站。

**Q：报 `Failed to fetch` / 结果不完整？**
A：基本可以确定是被限流了。等几分钟，并改用 `--via` 指定中转站。

**Q：余票显示为空？**
A：普速列车的余票字段与动车组不同，部分车次只给「有/无」；跨局列车也可能不返回具体张数。
`--require-seats` 只保留确认有余票的方案。

**Q：怎么查票价？**
A：加 `--with-price`。注意它是**每趟车一次额外请求**，会明显增加被限流的风险，
所以默认关闭、并受 `--price-limit`（默认 8）约束。建议配合 `--via` 用，总请求量控制在十次上下。
输出会逐程显示选定席别价格，并在方案末尾给出「双程合计 / 全程合计」和每程明细。
未指定 `--seat-class` 时合计可能混席别，只作比价参考。

**Q：票价和 12306 上显示的不一样？**
A：`queryTicketPrice` 返回的是该区间该席别的公布票价；若线路执行市场化浮动定价，
以 12306 下单页为准。

**Q：能不能帮我自动抢票/下单？**
A：不能，也不会加。本项目只做查询与方案筛选，见[免责声明](#免责声明)。

**Q：换了日期还要重新查吗？**
A：要。余票缓存按「出发城市|到达城市|日期」为键，默认只保 600 秒，且余票是动态数据。
票价缓存是单独的 24 小时（公布票价基本稳定）。

**Q：`--watch` 会不会把 12306 惹毛？**
A：设计上已经尽量保守：最小间隔 60 秒、每轮 `--max-queries` 预算、
只查必要城市对。但长时间高频监控仍可能触发限流，建议 3 分钟以上一轮，
必要时用 `--watch-trains` 缩小范围、用 `--via` 固定中转站。

**Q：`--near-miss` 会不会把不安全方案混进结果？**
A：不会。近似方案单独放在「差一点点的方案」区块，主方案列表仍然只包含
严格满足缓冲要求的方案；JSON 里也是独立的 `near_miss_plans` 字段。

**Q：三段换乘为什么不能自动挑枢纽？**
A：两段自动模式已经会查十几个枢纽；三段再自动展开，请求量和组合数都会爆炸，
很容易触发限流。所以只支持用户明确给出的 `--via A,B` 链。

---

## 已知限制

- **强依赖 12306 的私有接口**，官方未开放且可能随时变更。本项目开发期间就撞上过一次参数变更
  （`purpose_corporation` → `purpose_codes`）。接口若再变，本项目需要相应更新。
- **票价是「每趟车一次请求」**，所以 `--with-price` 默认关闭并受 `--price-limit` 约束。
- **票价缓存默认 24 小时**：公布票价稳定，但市场化浮动线路的价格可能变化；
  要最新价请设 `--price-cache-ttl 0` 或加 `--no-cache`。
- **站点缓冲表是人工保守估计**，不是 12306 官方数据；大站取 18~20 分钟，宁可多等。
- **`--watch` 是持续请求**，即使有最小间隔和预算，长时间监控仍可能触发限流。
- **票价取的是公布票价**；若线路执行市场化浮动定价，请以 12306 下单页为准。
- **未指定 `--seat-class` 时，合计是各段最低席别相加（可能混席别）**，只作比价参考；
  指定席别后只统计该席别，并在每程与合计明细里标出席别名。
- **站间通勤时间是人工整理的保守估计**，不是实时路况。
- 余票是动态数据，缓存 TTL 只有 10 分钟；要看最新余票请加 `--no-cache`。
- 自动模式在长时间密集使用下仍可能触发限流（见上）。
- **三段换乘只支持显式 `--via` 链**，不做自动三段。
- 只在 Windows + Edge 上做过完整实测；macOS / Linux 的浏览器探测逻辑已写但未逐一验证。

---

## 免责声明

- 本项目为**个人出行参考工具**，非官方产品，与 12306 / 中国国家铁路集团有限公司**无任何关联**。
- 仅做**查询与方案筛选**，不涉及下单、支付、占座、抢票或任何自动化购票行为。
- 12306 未对外提供公开 API。使用本项目可能涉及其服务条款；请自行评估并**保持低频使用**，
  不要用于批量抓取或商业用途。
- 站间通勤时间、换乘方案均为**估算与建议**，不构成出行保证。
  实际乘车请以铁路部门公布的信息为准，并自行留足余量。
- 使用本项目所产生的一切后果由使用者自行承担。

---

## 测试与开发

```bash
python -m pip install -e ".[dev]"
python -m pytest -q          # 80+ 个单元测试，不需要浏览器、不访问 12306
python -m ruff check .
```

测试覆盖：站点解析、返回行下标映射、跨天/跨两天到达、缓存 TTL、
两段与三段方案组合、站点缓冲、异站通勤、时间窗、席别筛选、近似方案、
请求预算、JSON/文本渲染与监控匹配。CI 在 Linux 和 Windows、Python 3.10/3.13 上跑。

---

## 贡献

欢迎提 Issue 和 PR，特别是：

- **补充 / 修正 [`references/interchange-times.json`](references/interchange-times.json)**
  里的同城站间通勤时间（附上你的实测数据最好）
- 补充 [`references/hubs.json`](references/hubs.json) 的枢纽候选
- 补充 / 修正 [`references/transfer-buffers.json`](references/transfer-buffers.json) 的站点换乘缓冲
- 接口变更的适配
- macOS / Linux 上的实测反馈

提 PR 前请确认 `python scripts/rail_transfer.py --detect` 能通过。

## License

[MIT](LICENSE)
