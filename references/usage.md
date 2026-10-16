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
    --via 上海 --with-price
```

## 4. 换乘可行性怎么判

这是本工具与官方中转最大的差别，规则是硬性的：

```
同站换乘：  第二程发车 − 第一程到达  ≥  min_transfer
异站换乘：  第二程发车 − 第一程到达  ≥  站间通勤时间 + min_transfer + cross_station_buffer
```

- `--min-transfer`：默认 **16 分钟**。
- `--cross-station-buffer`：默认 **5 分钟**，给找站、安检排队留的额外余量。
- 站间通勤时间来自 [interchange-times.json](interchange-times.json)，
  查不到的站对用 `_default`（60 分钟）**保守估计，并在输出里标注「估算」**。
- `--no-cross-station`：完全不考虑同城异站换乘。
- 每条方案都会打印「缓冲 X 分钟（要求 ≥Y）」，脚本内部还有一道自检，
  不满足要求的方案不会输出。

## 5. 票价查询（`--with-price`）

### 用法

```bash
python scripts/rail_transfer.py --from 太仓南 --to 句容 --date 2026-10-18 \
    --via 上海 --with-price
```

输出会在每段车次后面追加票价，并在方案标题上给出**最低合计**：

```
 1. 总耗时 2小时35分   最低合计 ¥145.0   换乘 上海虹桥（同站）
    1) D3201   太仓南 07:43 → 上海虹桥 08:09   余票 二等16
       票价 无座¥17.0 二等座¥17.0 一等座¥27.0
       ↓ 缓冲 25 分钟（要求 ≥16）✓
    2) G3272   上海虹桥 08:34 → 句容 10:18   余票 一等1 二等无
       票价 无座¥128.0 二等座¥128.0 一等座¥204.0
```

### ⚠ 请求量警告

**票价是「每趟车一次额外请求」**，不加限制很容易触发 12306 限流（见第 7 节）。
所以：

- `--with-price` **默认关闭**，需要才加。
- `--price-limit`（默认 8）限制最多为多少趟车查票价，实际请求量上限约 `2 × price-limit`。
- 同一「车次 + 区间 + 日期」只查一次，内存 + 磁盘双层缓存。
- 建议配合 `--via` 使用：基础查询本来就只要 2~4 次，加票价总共约 10 次上下。

### 「最低合计」怎么算

把两段各自最便宜的席别相加。**注意它可能混席别**（比如一段二等座 + 一段硬座），
所以只作比价参考，不代表能买到同一等级的组合。要精确组合请自己按席别加。

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

余票与票价都是动态数据，缓存 TTL 只有 10 分钟；要看最新的请加 `--no-cache`。

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
    "direct": [],
    "plans": [{
      "hub_city": "上海", "hub_arrive": "上海虹桥", "hub_depart": "上海虹桥",
      "same_station": true, "gap_min": 25, "required_min": 16,
      "interstation_min": 0, "interstation_estimated": false,
      "total": "2小时35分", "total_min": 155, "min_total": "¥145.0",
      "leg1": {
        "code": "D3201", "from_name": "太仓南", "to_name": "上海虹桥",
        "dep": "07:43", "arr": "08:09", "lishi": "00:26", "seats": "二等16 硬座有",
        "prices": {"无座": "¥17.0", "二等座": "¥17.0", "一等座": "¥27.0"}
      },
      "leg2": { }
    }]
  }
}
```

未加 `--with-price` 时，`prices` 为空对象 `{}`、`min_total` 为空字符串。

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

**Q：「最低合计」可以直接当总价看吗？**
A：只能当**比价参考**。它是两段各自最低席别相加，可能混席别。

**Q：余票显示为空？**
A：普速车的余票字段与动车组不同，部分车次只给「有/无」；跨局列车也可能不返回
具体张数。`--require-seats` 只保留确认有余票的方案。

**Q：能不能顺便帮我下单/抢票？**
A：不能。本 skill 只做查询与方案筛选，不涉及下单、支付、占座。
