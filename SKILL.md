---
name: rail-transfer
description: 当需要查询火车票中转换乘方案，调用自带脚本按最小换乘缓冲与同城异站通勤时间筛选可行换乘时使用。
---

# 火车票中转换乘查询

## 适用场景

- 出发站到到达站**没有直达车**，需要自己组合两段车次中转。
- 官方「中转换乘」不好用：缓冲时间不可调、忽略同城异站通勤，给出的方案会误车。
- 需要严格保证「上一趟车到达」与「下一趟车发车」之间留有指定缓冲时间。
- 换乘涉及**同一城市的不同车站**（如到上海虹桥、从上海走），必须把两站之间的通勤算进去。
- 一个城市车站太多，需要先列出候选站再决定从哪个站走。
- 有明确时间窗、席别或排序偏好，需要筛掉「时间不合适」的方案。
- 想看几天内哪天最好走，或需要「出发 → A → B → 到达」的三段换乘。
- 想盯着余票捡漏，有票时收到提醒。

## 必要输入

- `--from`：出发站（站名、城市名、拼音或电报码均可）
- `--to`：到达站（同上）
- `--date`：乘车日期 `YYYY-MM-DD`，省略则默认明天
- 可选 `--via`：中转站或中转城市，多个用逗号分隔；不给则自动挑选
- 可选 `--with-price`：附带票价（**每趟车多一次请求**，默认关闭，见下）
- 可选时间窗：`--depart-after/-before`、`--arrive-after/-before`（`HH:MM`）
- 可选 `--max-wait`：换乘等待上限（分钟）
- 可选 `--seat-class 二等座,硬卧` / `--no-standing`：席别约束
- 可选 `--sort total|arrive|depart|gap|price`：排序（`price` 需配合 `--with-price`）
- 可选 `--near-miss N`：额外列出「差 N 分钟以内」的方案
- 可选 `--avoid`：排除中转城市/车站
- 可选 `--max-queries N`：请求硬上限（0 不限）
- 可选 `--dates 2026-10-20..2026-10-22`：多日扫描（受 `--max-days` 限制）
- 可选 `--legs 3 --via A,B`：显式三段换乘
- 可选 `--watch`：余票监控（`--interval` 最小 60 秒；`--watch-cycles`、`--watch-trains`、`--no-notify`）
- `--min-transfer N`：同站换乘最小缓冲分钟数；**默认 16，但必须先问主人**（见执行步骤）
- `--ask-transfer`：命令行交互模式下，查询前自动询问换乘时间

若 `--from` / `--to` 给的是**城市名且该城有多个车站**，脚本会列出全部车站并停下，
由主人指定具体车站后再查 —— 这是刻意设计，避免脚本替主人决定从哪个站出发。

## 自动探测

先运行 [scripts/rail_transfer.py](scripts/rail_transfer.py) 的 `--detect`，
确认环境就绪：浏览器（Edge/Chrome）、`websocket-client`、站点表缓存与三个数据文件。
再用 `--stations <城市名>` 单独查看某城有哪些车站，此模式**不启动浏览器、不访问余票接口**。

## 执行步骤

1. 先阅读 [references/usage.md](references/usage.md)，确认本次是直达、指定中转站、
   还是自动挑选枢纽，以及需要的时间窗、席别等参数。
2. 运行 `--detect` 确认环境；缺失依赖时按提示补齐后再继续。
3. 若不确定站名，先跑 `--stations <城市名或站名>` 拿到候选车站清单。
4. **询问换乘时间（必须步骤）**：拿到出发站、到达站和日期后，先问主人：
   「两趟车之间至少留多少分钟？默认 16 分钟；同城异站还会再加站间通勤和
   cross_station_buffer（默认 5 分钟）。」
   - 主人给了数字：用 `--min-transfer N`。
   - 主人说默认 / 随便 / 不知道：用 16，并明确告诉主人「按 16 分钟算」。
   - 主人在最初消息里已经给了数字：跳过提问，直接用。
   - **不要**在没问过的情况下默默按 16 分钟开查。
   - 命令行人工使用时，可加 `--ask-transfer` 让脚本在查询前弹出同一个问题。
   - agent 调用脚本时不要依赖 `--ask-transfer`（可能没有可交互的 stdin），
     而是先把主人的答案拿到，再用 `--min-transfer N` 传进去。
5. 正式查询：给出 `--from`、`--to`、`--date` 和协商好的 `--min-transfer`；
   有明确中转站时用 `--via` 指定，让查询又快又稳。
6. 若某条方案的缓冲时间不理想，调整 `--min-transfer` 后重跑；
   反复微调时**不必加 `--no-cache`**，脚本自带的短期缓存会省掉重复请求。
   注意：大枢纽还会套用 `references/transfer-buffers.json` 的站点加严值，
   最终要求是 `max(--min-transfer, 站点缓冲)`；要完全按 `--min-transfer` 走，
   加 `--ignore-station-buffers`。
7. 读取脚本输出的人读报告或 `--format json` 结构化结果，重点关注
   「缓冲 X 分钟（要求 ≥Y）」这一行，它是方案是否可用的判据。
8. 出现 `ambiguous-context` 时，把候选车站列给主人，等主人选定后重跑。
9. 时间窗/席别/排序等过滤不会增加请求；`--near-miss` 只影响展示，不会污染主方案。
10. 多日扫描按天线性增加请求；`--max-queries` 是总预算。`--dates` 与 `--watch` 互斥。
11. 三段换乘必须显式给两个 `--via`；不要尝试自动三段。
12. 监控模式每轮绕过余票缓存、但保留 24 小时票价缓存；
    有票判定要求换乘方案的**每一段**都有座/卧票。
13. 需要票价时加 `--with-price`。**票价是「每趟车一次额外请求」**，
    会明显增加被限流的风险，因此默认关闭、并受 `--price-limit`（默认 8）约束；
    建议与 `--via` 一起用，把总请求量控制在十次上下。未指定 `--seat-class` 时，
    「最低合计」可能混席别，只作比价参考。

## 失败分流

- 缺少 `websocket-client`，或找不到 Edge/Chrome 时，返回 `environment-missing`。
- 浏览器起不来、CDP 连不上，或 12306 大量返回 `Failed to fetch` 时，返回
  `connection-failure`；这通常是**被 12306 限流**（见下），而不是参数写错。
- 城市名对应多个车站、或站名有歧义时，返回 `ambiguous-context`。
- `--legs 3` 缺少两个 `--via`、时间格式错误、席别无法识别时，返回
  `ambiguous-context` / `blocked`，并给出可用取值。
- `--max-queries` 用尽时结果标记 `partial_success`，报告里会写明「请求预算耗尽」；
  这不是接口故障，调大预算或缩减枢纽即可。
- 站点表能拿到但该站当天完全没有车次时，返回 `target-response-abnormal`。
- 站点表缓存损坏且无法重新下载时，返回 `artifact-missing`，可用
  `--refresh-stations` 强制刷新。

## 平台说明

- 依赖按优先级复用 `shared/tool_config.py` 的全局工具配置：浏览器路径可用
  `--set-browser-path` 持久保存一次，之后无需每次传 `--browser-path`。
- Windows 上必须用 `--no-proxy-server` 启动浏览器：本机开着 Clash 时，
  代理会让浏览器自动化整体失效。脚本已内置该参数。
- 站点表缓存在 `%LOCALAPPDATA%\rail-transfer\station_name.js`（默认 7 天）。
- `--watch` 会持续请求 12306：最小间隔 60 秒，默认 180 秒；建议 3 分钟以上，
  并配合 `--max-queries`、`--watch-trains` 或 `--via` 压低请求量。
- 脚本以 UTF-8 输出，Windows 控制台已做编码兜底。

## 输出约定

- 默认输出人读报告：直达车次列表 + 换乘方案列表（两段或三段），每条方案逐段标注
  换乘站、同站/异站、站间通勤时间、缓冲余量与是否满足要求；满足要求的用 `✓`。
  `--near-miss` 的方案单独放在「差一点点的方案」区块，用 `✗` 标注。
  多日扫描输出每天一行的汇总；加 `--with-price` 时每段追加票价与最低合计。
- `--format json` 输出结构化结果，字段对齐 `shared/contracts.md` 的
  Command Outcome Schema：`status` / `summary` / `evidence` / `next_action` /
  `failure_category` / `data`。
- `data` 里：每条方案有 `legs`（逐段车次）与 `transfers`（逐换乘点），
  同时保留旧版 `leg1` / `leg2` / `hub_arrive` 等兼容字段；
  `near_miss_plans` 与 `plans` 分开；`budget` 记录请求预算用量；
  多日扫描用 `multi: true` + `results: [每日结果]`。
- 监控模式的 JSON 输出是**每轮一行 JSONL**（`time` / `cycle` / `available` / `new`），
  不额外输出最终汇总对象。
- 退出码：`success` 与 `partial_success` 为 0，`blocked` 为 2，`failure` 为 1。

## 交接关系

- 本 skill 只做**查询与方案筛选**，不涉及下单、支付或占座。
- 若主人拿到方案后要去官方渠道购票，直接把方案里的车次、乘车站、
  日期与时间抄过去即可；本 skill 不再向下游 skill 传递状态。
