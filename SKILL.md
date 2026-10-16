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

## 必要输入

- `--from`：出发站（站名、城市名、拼音或电报码均可）
- `--to`：到达站（同上）
- `--date`：乘车日期 `YYYY-MM-DD`，省略则默认明天
- 可选 `--via`：中转站或中转城市，多个用逗号分隔；不给则自动挑选

若 `--from` / `--to` 给的是**城市名且该城有多个车站**，脚本会列出全部车站并停下，
由主人指定具体车站后再查 —— 这是刻意设计，避免脚本替主人决定从哪个站出发。

## 自动探测

先运行 [scripts/rail_transfer.py](scripts/rail_transfer.py) 的 `--detect`，
确认三项就绪：浏览器（Edge/Chrome）、`websocket-client`、站点表缓存。
再用 `--stations <城市名>` 单独查看某城有哪些车站，此模式**不启动浏览器、不访问余票接口**。

## 执行步骤

1. 先阅读 [references/usage.md](references/usage.md)，确认本次是直达、指定中转站、
   还是自动挑选枢纽，以及需要的缓冲参数。
2. 运行 `--detect` 确认环境；缺失依赖时按提示补齐后再继续。
3. 若不确定站名，先跑 `--stations <城市名或站名>` 拿到候选车站清单。
4. 正式查询：给出 `--from`、`--to`、`--date`；有明确中转站时用 `--via` 指定，
   让查询又快又稳。
5. 若某条方案的缓冲时间不理想，调整 `--min-transfer`（默认 16 分钟）后重跑；
   反复微调时**不必加 `--no-cache`**，脚本自带的短期缓存会省掉重复请求。
6. 读取脚本输出的人读报告或 `--format json` 结构化结果，重点关注
   「缓冲 X 分钟（要求 ≥Y）」这一行，它是方案是否可用的判据。
7. 出现 `ambiguous-context` 时，把候选车站列给主人，等主人选定后重跑。

## 失败分流

- 缺少 `websocket-client`，或找不到 Edge/Chrome 时，返回 `environment-missing`。
- 浏览器起不来、CDP 连不上，或 12306 大量返回 `Failed to fetch` 时，返回
  `connection-failure`；这通常是**被 12306 限流**（见下），而不是参数写错。
- 城市名对应多个车站、或站名有歧义时，返回 `ambiguous-context`。
- 站点表能拿到但该站当天完全没有车次时，返回 `target-response-abnormal`。
- 站点表缓存损坏且无法重新下载时，返回 `artifact-missing`，可用
  `--refresh-stations` 强制刷新。

## 平台说明

- 依赖按优先级复用 `shared/tool_config.py` 的全局工具配置：浏览器路径可用
  `--set-browser-path` 持久保存一次，之后无需每次传 `--browser-path`。
- Windows 上必须用 `--no-proxy-server` 启动浏览器：本机开着 Clash 时，
  代理会让浏览器自动化整体失效。脚本已内置该参数。
- 站点表缓存在 `%LOCALAPPDATA%\rail-transfer\station_name.js`（默认 7 天）。
- 脚本以 UTF-8 输出，Windows 控制台已做编码兜底。

## 输出约定

- 默认输出人读报告：直达车次列表 + 换乘方案列表，每条方案标注换乘站、同站/异站、
  站间通勤时间、缓冲余量与是否满足要求。
- `--format json` 输出结构化结果，字段对齐 `shared/contracts.md` 的
  Command Outcome Schema：`status` / `summary` / `evidence` / `next_action` /
  `failure_category` / `data`。
- 退出码：`success` 与 `partial_success` 为 0，`blocked` 为 2，`failure` 为 1。

## 交接关系

- 本 skill 只做**查询与方案筛选**，不涉及下单、支付或占座。
- 若主人拿到方案后要去官方渠道购票，直接把方案里的车次、乘车站、
  日期与时间抄过去即可；本 skill 不再向下游 skill 传递状态。
