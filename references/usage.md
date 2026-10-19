# rail-transfer Skill 用法

## 1. 环境自检

```bash
python scripts/rail_transfer.py --detect
```

会检查浏览器（Edge/Chrome）、`websocket-client`、站点表缓存、数据文件四项。
缺 `websocket-client` 时：

```bash
python -m pip install websocket-client
```

浏览器不在常见路径时，指定一次并持久保存：

```bash
python scripts/rail_transfer.py --set-browser-path "D:/Edge/msedge.exe"
```

## 2. 先查车站（不开浏览器、不碰余票接口）

一个城市往往有十几个车站（苏州名下就有 14 个），所以先看清再决定：

```bash
python scripts/rail_transfer.py --stations 句容      # 单站：直接给出电报码
python scripts/rail_transfer.py --stations 苏州      # 城市：列出全部 14 个站
python scripts/rail_transfer.py --stations 镇江
```

城市名有多个车站时，脚本以 `ambiguous-context`（退出码 2）停下并列出候选，
**不会替主人挑**。用站名全称重跑即可；确实想全城一起搜时加 `--all-stations`。

## 3. 常用查询

```bash
# 指定中转站（推荐：最快最稳）
python scripts/rail_transfer.py --from 句容西 --to 苏州 --date 2026-10-22 --via 南京南

# 指定中转城市（会展开该城全部车站，支持同城异站换乘）
python scripts/rail_transfer.py --from 句容西 --to 苏州 --date 2026-10-22 --via 上海

# 自动挑选枢纽
python scripts/rail_transfer.py --from 句容西 --to 苏州 --date 2026-10-22

# 到达城市有多个车站，全城一起搜
python scripts/rail_transfer.py --from 句容西 --to 苏州 --all-stations --date 2026-10-22

# 只要有余票的，输出 JSON
python scripts/rail_transfer.py --from 句容西 --to 苏州 --date 2026-10-22 \
    --via 南京南 --require-seats --format json

# 带上票价（见第 5 节，注意请求量）
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --via 上海 --seat-class 二等座        # 默认查价；--no-price 关闭
```

### 3.1 时间窗、排序与席别

```bash
# 18 点前到，只要二等座，按到达时刻排序
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --arrive-before 18:00 --seat-class 二等座 --sort arrive

# 换乘最多等 90 分钟，卧铺优先
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --max-wait 90 --seat-class 卧铺
```

- 发车时间窗看第一程，到达时间窗看最后一程；跨天到达按真实日期比较（次日 01:00 不会被当成当天 22:00 前）。
- `--sort` 取值：`total`（默认）/ `arrive` / `depart` / `gap` / `price`。
  `price` 需要 `--with-price`，否则没有票价可用，会退化为按总耗时排序。
- `--seat-class` 支持 `商务座、一等座、二等座、软卧、硬卧、硬座、无座`，
  以及别名 `卧铺`（软卧+硬卧）、`坐票`/`座位`（各类坐席）。
  指定后每一段都必须有该席别余票。
- `--no-standing` 不把无座当作有票，并从输出里隐藏无座。
- 查询当天时，默认隐藏已经发车的车次；要看全量加 `--show-departed`。

### 3.2 近似方案与排除中转

```bash
# 额外列出只差 5 分钟以内的方案（单独成块，不进主列表）
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --via 南京 --near-miss 5

# 别走北京
python scripts/rail_transfer.py --from 上海 --to 哈尔滨 --date 2026-10-18 \
    --avoid 北京
```

- `--near-miss` 的 JSON 字段是 `near_miss_plans`，与 `plans` 分开；
  每个换乘点还有一个 `near_miss` 布尔值。
- `--avoid` 对自动枢纽和 `--via` 都生效，不增加请求。

### 3.3 多日扫描

```bash
python scripts/rail_transfer.py --from 太仓南 --to 句容 \
    --dates 2026-10-18..2026-10-20 --via 上海
```

- 支持 `A..B` 范围或逗号分隔的日期；`--max-days`（默认 7）限制天数。
- 每天独立查询，请求量按天数线性增长；`--max-queries` 是总预算。
- 文本输出每天一行汇总；JSON 用 `multi: true` + `results` 保存每天完整结果。
- `--dates` 与 `--watch` 互斥。

### 3.4 三段换乘

```bash
python scripts/rail_transfer.py --from 某小站 --to 另一小站 --date 2026-10-18 \
    --legs 3 --via 南京南,徐州东
```

- 只有显式 `--via` 链才支持三段，且必须恰好两个中转站。
- 每段独立检查缓冲、站点缓冲与异站通勤；加 `--allow-overnight` 后后两段会查次日。
- 不做自动三段：请求量和组合数都会爆炸，容易触发限流。

### 3.5 余票监控

```bash
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --via 上海 --watch --interval 180

# 只盯指定车次，跑 10 轮后结束
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --via 上海 --watch --watch-trains D3201,G3272 --watch-cycles 10
```

- 每轮绕过余票缓存，但保留 24 小时票价缓存。
- 最小间隔 60 秒；默认 180 秒。`--max-queries` 是**每轮**预算。
- 有票判定：直达车看该车次；换乘方案要求每一段都有座/卧票。
- 有票时发桌面通知（Windows/macOS/Linux 尽力而为），同时打印 `🔔` 行。
- `--format json` 时每轮输出一行 JSONL；Ctrl+C 停止。

### 3.6 请求预算

```bash
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --via 上海 --max-queries 40
```

超过预算会立即停止未完成查询，结果标记 `partial_success`，
报告里写「请求预算耗尽」。用 `--via` + 小预算可以稳定控制请求量。

## 4. 换乘可行性怎么判

这是本工具与官方中转最大的差别，规则是硬性的：

```
同站换乘：  第二程发车 − 第一程到达  ≥  max(min_transfer, 站点缓冲)
异站换乘：  第二程发车 − 第一程到达  ≥  站间通勤 + max(min_transfer, 站点缓冲) + cross_station_buffer
```

- `--min-transfer`：默认 **16 分钟**。**查询前应先确定这个值**：
  agent 会在拿到出发/到达站后询问主人；命令行人工使用可加 `--ask-transfer`：

  ```bash
  python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
      --via 上海 --ask-transfer
  ```

  脚本会提示「两趟车之间至少留多少分钟？」，直接回车使用 16 分钟；
  也可以用 `--min-transfer 25 --ask-transfer` 把 25 作为提示里的默认值。
- **站点缓冲**来自 [transfer-buffers.json](transfer-buffers.json)：大枢纽 18~20 分钟，
  查不到的车站不额外加严，仍按 `--min-transfer`（默认 16 分钟）。最终要求和 `--min-transfer` 取较大值；
  `--ignore-station-buffers` 可完全忽略这份表。
- `--cross-station-buffer`：默认 **5 分钟**，给找站、安检排队留的额外余量。
  它和 `--min-transfer` 一样可以手动改；`--ask-transfer` 的提示里会说明这一点。
- 站间通勤时间来自 [interchange-times.json](interchange-times.json)，
  查不到的站对用 `_default`（60 分钟）**保守估计，并在输出里标注「估算」**。
- `--no-cross-station`：完全不考虑同城异站换乘。
- 每条方案都会打印「缓冲 X 分钟（要求 ≥Y）」，脚本内部还有一道自检，
  不满足要求的方案不会输出。

## 5. 票价查询（默认开启）

### 用法

```bash
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --via 上海 --seat-class 二等座        # 默认查价；--no-price 关闭
```

输出会逐程显示选定席别的价格，并在方案末尾给出**双程合计**：

```
 1. 总耗时 2小时35分（两段运行 2小时10分 + 换乘等待 25分）   合计 ¥145.0   换乘 1 次
    1) D3201   太仓南 07:43 → 上海虹桥 08:09   运行 00:26  余票 一等有 二等有 无座有   二等座 ¥17.0
       ↓ 缓冲 25 分钟（要求 ≥20）✓   换乘 上海虹桥（同站）
    2) G3272   上海虹桥 08:34 → 句容 10:18   运行 01:44  余票 商务3 一等13 二等有 无座有   二等座 ¥128.0
    ── 双程合计 ¥145.0（二等座 ¥17.0 + 二等座 ¥128.0）
```

这里的「要求 ≥20」来自上海虹桥的站点缓冲（`transfer-buffers.json`），
不是全局的 16 分钟。未指定 `--seat-class` 时，每程显示全席别最低价并标出席别名
（如 `最低 ¥17.0（无座）`），合计可能混席别。

### ⚠ 请求量警告

**票价是「每趟车一次额外请求」**，不加限制很容易触发 12306 限流（见第 7 节）。
所以：

- 票价**默认开启**；请求敏感时加 `--no-price`。
- `--price-limit`（默认 20）限制最多为多少趟车查票价，实际请求量上限约 `2 × price-limit`。
- 同一「车次 + 区间 + 日期」只查一次，内存 + 磁盘双层缓存；
  磁盘票价缓存默认 **24 小时**（`--price-cache-ttl`），因为公布票价基本稳定。
- 建议配合 `--via` 使用：基础查询本来就只要 2~4 次，加票价总共约 10 次上下。

### 「双程 / 全程合计」怎么算

- 指定 `--seat-class`：每程取该席别的最低价，合计 = 各程相加；
  输出末尾会列出 `二等座 ¥17.0 + 二等座 ¥128.0` 这样的明细。
- 未指定席别：每程取全席别最低价，合计可能混席别（比如一段二等座 + 一段硬座），
  只作比价参考。
- 两段显示「双程合计」，三段显示「全程合计」。
- JSON 里对应字段是 `total_price`、`total_price_detail`、`total_price_note`；
  每段还有 `selected_seat` / `selected_price`。`min_total` 保留为兼容字段。

### 席别代码

票价返回里是 12306 的席别代码，脚本会翻译成中文名：

| 代码 | 席别 | | 代码 | 席别 |
| --- | --- | --- | --- | --- |
| `9` | 商务座 | | `1` | 硬座 |
| `P` | 特等座 | | `2` | 软座 |
| `M` | 一等座 | | `3` | 硬卧 |
| `O` | 二等座 | | `4` | 软卧 |
| `WZ` | 无座 | | `6` | 高级软卧 |

遇到表里没有的代码会原样显示成「席别X」，**不会瞎猜**。

## 6. 自动挑枢纽是怎么挑的

12306 站点表里的**城市编码是沿铁路地理顺序编排的**：
黑龙江 `00xx` → 吉林 `01xx` → 辽宁 `02xx` → 京津冀 `03xx` → 山西 `04xx`
→ 山东 `06xx` → 江苏/上海 `07xx` → 安徽 `08xx` → 浙江 `09xx` → 福建 `10xx`
→ 江西 `11xx` → 河南 `12xx` → 湖北 `13xx` → 湖南 `14xx` → 广东 `15xx`
→ 广西 `16xx` → 四川/重庆 `17xx` → 贵州 `18xx` → 云南 `19xx` → 陕西 `20xx`
→ 甘肃 `21xx` → 青海 `22xx` → 宁夏 `23xx` → 新疆 `24xx`

脚本据此挑枢纽：

- **短途**（编码跨度 ≤ 60）：取编码上离两端最近的枢纽。句容西→苏州 会挑出
  南京、上海、常州、无锡、镇江 —— 正是真正管用的换乘点。
- **长途**：把编码区间均匀分段、每段取一个枢纽，保证沿线路「铺开」，
  不会被枢纽表本身的地域偏斜带跑（否则会在长三角堆一堆小站、却漏掉郑州东/武汉）。

`--auto-hubs N` 控制枢纽数量上限，**默认 16**。调大更全，但见下一节。

## 7. ⚠ 限流：这是最大的现实约束

12306 会对**累计请求量**做限制。实测现象：

- 单次 3～30 次查询：稳定正常。
- 单次 100 次以上查询：出现大量 `Failed to fetch`（约 20%），且**会持续一段时间**，
  期间连全新浏览器会话的单次查询也会失败。
- 并发数不是主因：并发 4 明显更糟，但并发降到 1 仍有约 20% 失败。

因此默认值刻意保守：`--auto-hubs 16`、`--concurrency 2`、自适应节流
（一失败就放慢、顺利再提速）、失败自动补查两次、600 秒短期缓存。

**被限流时的应对**：

1. 稍等几分钟再跑，通常会恢复。
2. 用 `--via` 指定中转站 —— 只需 2～4 次查询，几乎不可能触发限流。**推荐做法**。
3. 调小 `--auto-hubs`（如 `--auto-hubs 8`）。
4. 谨慎使用 `--with-price`，必要时调小 `--price-limit`。
5. 脚本缓存在反复微调参数时能省掉重复请求（含票价）。
6. 用 `--max-queries N` 给单次命令钉死请求上限；超出会停止并标记 `partial_success`。
7. 余票监控每轮都会绕过余票缓存，但 `--interval` 最小 60 秒，且建议 3 分钟以上。

余票缓存默认 10 分钟；票价缓存默认 24 小时（公布票价基本稳定）。
要看最新余票加 `--no-cache`；要最新票价再设 `--price-cache-ttl 0`。

## 8. 已实测确认的接口事实（改动代码前务必先读）

这些是踩过的坑，写在 [../scripts/rail_transfer.py](../scripts/rail_transfer.py) 的注释里：

1. **余票接口参数必须是 `purpose_codes=ADULT`**。用老的 `purpose_corporation=0`
   会被静默 302 到 `error.html`，**不报任何错误**，极难排查。这是最大的坑。
2. **必须从真实浏览器页面上下文发起请求**。curl / 裸请求一律 302；
   伪造 `RAIL_DEVICEID`、补 `X-Requested-With`、改用有头模式**统统无效**。
3. **余票接口是按「城市对」返回的，不是站点对**。查「句容西→苏州」回来的行里，
   乘车站既有句容西(JWH) 也有句容(JRU)，下车站覆盖张家港/常熟/太仓/苏州南 等；
   JWH→SZH、JWH→ZAU、JRU→SZH 三个站点对返回的集合**完全一致**。
   所以每行自带的乘车站/下车站（下标 6/7）才是准的，查询参数只是个「城市对入口」。
   好处是查询量从「出发站 × 到达站」降到「出发城市 × 到达城市」。
4. **官方中转接口 `lcquery/queryG` 目前恒返回「系统忙」**，参数正确也一样。
   所以换乘方案一律自行组合。
5. **票价接口 `queryTicketPrice` 可用**，参数全部取自余票行（实测下标）：

   | 参数 | 取自余票行下标 |
   | --- | --- |
   | `train_no` | `[2]` |
   | `from_station_no` | `[16]` |
   | `to_station_no` | `[17]` |
   | `seat_types` | `[35]` |
   | `train_date` | 查询日期 |

   返回体有两套写法，两种都要认：
   `{"A1":"¥32.5","1":"325"}`（A 前缀是带 ¥ 的显示价，裸键是 ×10 的整数）
   与 `{"M":"¥124.0"}`（直接就是带 ¥ 的显示价）。
   优先采用带 ¥ 的显示值，避免自己做除法引入误差。
   `OT` 是备注数组（如 `["混编硬座: ¥32.5"]`），`train_no` 是回显，两者都要跳过。
6. `station_name.js` 是**静态资源、不受风控影响**，可以直接下载解析；
   而 `train_list.js`（15MB）数据冻结在 2022-08，是废弃文件，不要用。
7. `data.map`（电报码→站名）**只含本次涉及的部分车站**，查不到时必须回退到
   `station_name.js`，否则部分车次的始发/终到站会显示成电报码。

## 9. 输出格式

```bash
python scripts/rail_transfer.py ... --format json
```

结构化字段对齐 `shared/contracts.md` 的 Command Outcome Schema：

```json
{
  "status": "success",
  "summary": "3 条换乘方案（查询 9 次）",
  "evidence": ["查询次数 9（缓存命中 0 个城市对）", "直达 0 趟，换乘 3 条", "其中票价查询 6 次"],
  "next_action": "python scripts/rail_transfer.py --from ... --via <中转站>",
  "failure_category": null,
  "data": {
    "from": "太仓南", "to": "...", "date": "2026-10-18",
    "min_transfer": 16, "cross_station_buffer": 5,
    "queries": 9, "price_calls": 6,
    "warnings": [], "errors": [],
    "sort": "total", "chain": false,
    "filters": {"seat_classes": [], "near_miss": 0, "max_wait": null},
    "budget": {"limit": 0, "used": 9, "exhausted": false},
    "direct": [],
    "plans": [{
      "legs": [
        {"code": "D3201", "from_name": "太仓南", "to_name": "上海虹桥",
         "dep": "07:43", "arr": "08:09", "lishi": "00:26", "lishi_min": 26,
         "seats": "二等16 硬座有", "has_seat": true,
         "selected_seat": "二等座", "selected_price": "¥17.0",
         "dep_datetime": "2026-10-18T07:43:00",
         "arr_datetime": "2026-10-18T08:09:00",
         "prices": {"无座": "¥17.0", "二等座": "¥17.0", "一等座": "¥27.0"}},
        {"code": "G3272", "from_name": "上海虹桥", "to_name": "句容", "...": "..."}
      ],
      "transfers": [{
        "hub_city": "上海", "hub_arrive": "上海虹桥", "hub_depart": "上海虹桥",
        "same_station": true, "gap_min": 25, "required_min": 16,
        "interstation_min": 0, "interstation_estimated": false,
        "near_miss": false
      }],
      "total": "2小时35分", "total_min": 155,
      "rail_total": "2小时10分", "rail_min": 130, "wait_min": 25,
      "total_price": "¥145.0",
      "total_price_detail": [
        {"code": "D3201", "seat": "二等座", "price": "¥17.0"},
        {"code": "G3272", "seat": "二等座", "price": "¥128.0"}
      ],
      "total_price_note": "指定席别",
      "min_total": "¥145.0",
      "near_miss": false,
      "hub_city": "上海", "hub_arrive": "上海虹桥", "hub_depart": "上海虹桥",
      "same_station": true, "gap_min": 25, "required_min": 16,
      "leg1": {"code": "D3201", "...": "..."},
      "leg2": {"code": "G3272", "...": "..."}
    }],
    "near_miss_plans": []
  }
}
```

要点：

- 每条方案用 `legs`（逐段）与 `transfers`（逐换乘点）描述，两段和三段一致；
  同时保留旧版 `leg1` / `leg2` / `hub_arrive` / `gap_min` 等字段，方便旧调用方。
- `total` = `rail_total`（各段运行之和）+ `wait_min`（换乘等待，含跨站通勤）；
  车次级的 `lishi_min` 是单段运行分钟数。
- `near_miss_plans` 与 `plans` 分开，不会混在一起。
- `budget` 记录请求预算上限、实际用量与是否耗尽。
- 多日扫描的 `data` 是 `multi: true` + `results: [每日结果]` + `dates`。
- 监控模式的 JSON 是每轮一行 JSONL（`time` / `cycle` / `available` / `new`），
  不额外输出汇总对象。

加 `--no-price` 时，`prices` 为空对象 `{}`、`total_price` / `min_total`
为空字符串、`total_price_detail` 为空数组；`rail_total` / `rail_min` / `wait_min`
与票价无关，始终输出。

退出码：`success` / `partial_success` → 0，`blocked` → 2，`failure` → 1。

## 10. 常见问题

**Q：为什么 `--from 苏州` 不直接开查？**
A：因为苏州名下 14 个站，脚本不替主人决定。用 `--stations 苏州` 看清单，
再写具体站名；或者确认要全城搜时加 `--all-stations`。

**Q：结果里一条换乘都没有？**
A：优先看报告里的 `⚠ 提示`。若是限流，稍后重试或改用 `--via`；若确实没有，
可以放宽 `--min-transfer`、加 `--allow-overnight`、或用 `--via` 指定别的中转站。

**Q：票价和 12306 上显示的不一样？**
A：`queryTicketPrice` 返回的是该区间该席别的公布票价。
若有浮动票价（部分线路执行市场化定价），以 12306 下单页为准。

**Q：「双程合计」可以直接当总价看吗？**
A：指定 `--seat-class` 时，它就是该席别各程价格之和，比较可靠；
未指定席别时，它是各程最低席别相加，可能混席别，只能当比价参考。

**Q：余票显示为空？**
A：普速车的余票字段与动车组不同，部分车次只给「有/无」；跨局列车也可能不返回
具体张数。`--require-seats` 只保留确认有余票的方案。

**Q：`--watch` 会不会被限流？**
A：设计上尽量保守（最小 60 秒、每轮预算、只查必要城市对），但长时间高频监控
仍可能触发限流。建议 3 分钟以上一轮；用 `--watch-trains` 缩小范围，
或用 `--via` 固定中转站。

**Q：`--near-miss` 会不会把不安全方案混进结果？**
A：不会。近似方案单独放在「差一点点的方案」区块，JSON 里也是独立的
`near_miss_plans` 字段；主方案仍然严格满足缓冲要求。

**Q：三段换乘能自动挑中转站吗？**
A：不能。只支持显式 `--via A,B` 链——自动三段会让请求量和组合数一起爆炸。

**Q：能不能顺便帮我下单/抢票？**
A：不能。本 skill 只做查询与方案筛选，不涉及下单、支付、占座。
