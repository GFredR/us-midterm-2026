# 2026 美国中期选举 · 官方计票追踪器

一个**数据全部来自美国各州州务卿 / 选举委员会官方结果系统**的中期选举实时计票网站，
用可视化呈现红蓝双方的票数与席位归属。

> 选举日：2026-11-03（美东时间）｜当前处于选举夜后认证阶段
> 认证前各州公布的都是 **非官方实时结果（Unofficial / Election Night Results）**，
> 票数会随清点回补持续变动。

---

## 一、为什么数据源是各州而不是 FEC

美国联邦选举委员会（FEC）在其官网明确说明：

> "Election results are not available immediately from the FEC. Instead, state election
> offices are responsible for certifying results."
> （实时结果不由 FEC 提供，认证与公布由各州选举办公室负责；FEC 只在认证后汇编发布。）

因此本站的实时数据直接取自 50 州 + 华盛顿特区各自的官方结果系统，FEC 仅作为
认证后的权威汇编来源被记录在数据源清单中。

---

## 二、目录结构

```
us-midterm-2026/
├── index.html                 # 站点页面
├── assets/
│   ├── styles.css             # 深色主题样式
│   ├── app.js                 # 前端渲染（席位计算 / 图表 / 表格 / 筛选排序）
│   └── echarts.min.js         # ECharts 5.5.1（已本地化，无 CDN 依赖）
├── data/
│   ├── election-2026.json     # 归一化后的计票快照（前端读取这个文件）
│   └── raw/                   # 各州抓取到的原始页面 / JSON，便于复核
└── scripts/
    ├── collect.py             # 采集器（Python 标准库，无第三方依赖）
    ├── browser.mjs            # Chrome CDP 抓取器（应对 JS 挑战 / SPA / iframe / 网络接口嗅探）
    ├── verify.mjs             # 页面渲染校验（控制台错误 / 关键 DOM / 截图）
    └── state_sources.json     # 51 个辖区的官方源配置 + 选举结构基准
```

---

## 三、刷新数据

```bash
cd us-midterm-2026

python3 scripts/collect.py                # 全量刷新（默认）
python3 scripts/collect.py --only WA,CO   # 只刷新指定州
python3 scripts/collect.py --no-browser   # 禁用 Chrome 通道，只用直连
python3 scripts/collect.py --list         # 查看各州官方源配置
```

采集器会自动：

1. 用 `curl` 直连官方 JSON 接口或静态表格页；
2. 遇到 Cloudflare JS 挑战 / SPA / iframe 时，自动拉起本机 Chrome
   （`--remote-debugging-port=9333`）渲染后再取 DOM，或嗅探页面实际调用的机读接口；
3. 把结果归一化成统一结构写入 `data/election-2026.json`；
4. 为每个州记录**官方机构、原始 URL、抓取时间与状态**，前端逐条可溯源。

**增量安全**：本轮抓取失败的州会沿用上一轮快照并标记 `stale`，不会被清空；
只有成功拿到新数据的州才会替换旧记录。51 个辖区始终全部在册，
即使尚未接入也会在「官方数据源清单」中列出并给出官方页面直达链接。

解析不出票数的州会被标记为 `pending`（页面可达但结果未公布）或 `blocked`
（官方站点对访问来源做了限制），**不会写入任何推测数字**。

### 浏览器通道的网络要求

部分州务卿站点对境外/数据中心 IP 做了封锁。本机的 Chrome 需要**走本机 HTTP 代理**
才能解析并访问这些站点：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9333 \
  --user-data-dir=/tmp/cdp-profile-midterm \
  --proxy-server=127.0.0.1:60502 \
  --no-first-run --ignore-certificate-errors about:blank &
```

### 数据结构

```jsonc
{
  "meta": {
    "generated_at": "...", "states_ok": 6, "states_total": 51,
    "baseline": { "house": {...}, "senate": {...}, "governor": {...} }
  },
  "states": [{
    "id": "CO", "zh": "科罗拉多州", "ev": 10,
    "house_seats": 8, "senate_seats_up": 1, "gov_up": true,
    "senate_holder": "D", "gov_holder": "D",     // 该州改选席位的原持有人党派
    "agency": "科罗拉多州州务卿", "url": "https://...",
    "status": "ok",            // ok | pending | blocked | error
    "updated_at": "2026-11-04T05:12:00+00:00"
  }],
  "races": [{
    "id": "CO-20", "state": "CO", "state_zh": "科罗拉多州",
    "chamber": "senate",       // senate | house | governor | other
    "office": "United States Senator",
    "candidates": [
      { "name": "...", "party": "R", "votes": 705158, "pct": 35.23, "winner": false }
    ],
    "total_votes": 2001671,
    "reporting": 1.56,          // 已报告比例（%，全站统一为百分数）
    "called": false,
    "source_url": "https://..."
  }]
}
```

---

## 四、选举结构基准（已逐项核验）

这些数字决定了站点显示的席位是否可信，因此全部按官方口径核对过：

| 层级 | 总席位 | 本轮改选 | 未改选席位（仍属原持有人） | 过半门槛 |
|---|---|---|---|---|
| 美国众议院 | 435 | **435**（全部改选） | R0 / D0 | 218 |
| 美国参议院 | 100 | **35** 席，分布于 35 个州 | R31 / D34 | 50 |
| 州长 | 50 | **36** 州 | R9 / D5 | 26 |

- **参议院 35 席**：Class II 33 席（AK AL AR CO DE GA IA ID IL KS KY LA MA ME MI MN MS MT NC NE NH NJ NM OK OR RI SC SD TN TX VA WV WY）
  \+ 俄亥俄、佛罗里达各 1 场 Class III 特选。
  俄亥俄的 Brown 持有的是 Class I（2028 才改选）、佛州的 Class II 同理，
  因此这两州本轮各只有 1 席。**宾夕法尼亚 2026 无参院选举**。
  改选席位的原持有人分布为 R22 / D13。
- **州长 36 州**：AL AK AZ AR CA CO CT FL GA HI ID IL IA KS ME MD MA MI MN NE NV NH NM NY OH OK OR PA RI SC SD TN TX VT WI WY。
  印第安纳与弗吉尼亚本轮不改选。原持有人分布为 R18 / D18。
- **选举人票 / 众院席数**：依据美国国家档案馆 2024 年选举人团官方选举人票表（合计 538）推导：
  `众院席数 = 选举人票 − 2`，DC 为 1（合计 436 席）。
- **⚠️ 435 vs 436：DC 的那 1 席不占众院投票席位。**
  官方表的 538 张选举人票里，DC 单独占 3 张（宪法第 23 修正案产生）；
  50 州合计 535 张，减去 100 张参院席，剩下 **435 个众院投票席位，全部由 50 州占据**。
  DC 在众议院只有 1 名**无表决权代表（delegate）**，不参与表决、不计入 218 席过半门槛。
  站点因此按 435 席渲染席位格图，DC 单独在图下方注明，避免出现"435 席却画了 436 格"的自相矛盾。
  逐州核对结果：配置与官方表 **51 个辖区全部一致**（含 IL 17 席、MT 2 席、WV 2 席、LA 6 席这几处易错的）。

---

## 五、指标口径

| 指标 | 口径 |
|---|---|
| 席位分布 | 拆成四个数字：①本轮改选席位数 ②未改选且仍由原持有人占有的席位 ③已有官方结果、可判定领先方的席位 ④尚无官方结果的待定席位。**颜色表示"当前领先"，不等于"最终归属"。** |
| 过半所需 | 直接给出"在本轮改选席中至少拿下多少席才能过半"（参院：蓝 ≥16 / 红 ≥19；州长：蓝 ≥21 / 红 ≥17；众院：各 ≥218），而不是简单比较当前席位数——因为未出结果的席位仍会改变归属。 |
| 红蓝总票数 | 仅统计**参议院 / 众议院 / 州长**三类竞选，且排除党派未标注的候选人。**不等于全国普选总票数**，仅作趋势参考。 |
| 已报告比例 | 官方结果中的已报告计票单位 ÷ 应报告计票单位，全站统一为百分数。 |
| 翻转标记 | 参院 / 州长竞选的领先方党派与该席位原持有人党派不同时标注"翻转"。 |
| 席位条读法 | KPI 卡里的席位条是**双向条**：红块从最左端长出、蓝块从最右端长出，中间斜纹区是尚未确定归属的席位（待数据 + 其他）。虚线标出"过半席位"——线画在第 N 席的中心（席位从 1 计，第 N 席占 [N-1, N]，故线在 N−0.5 处）。红块触线 = 共和党过半，蓝块触线 = 民主党过半。条下两端标注"← 共和党 0"与"N 民主党 →"，说明这条是从两端往中间长的。 |

---

## 六、已知限制

- 部分州务卿网站对特定网络来源做了地域或爬虫限制（Cloudflare Error 1009 / Imperva /
  Akamai / CloudFront / DNS 屏蔽），这些州会被标记为"访问受限"，
  并在「官方数据源清单」中给出官方页面直达链接供核对。
- 部分州的官方结果以 PDF、图片或 JS 单页应用形式发布，暂未纳入自动解析
  （例如新泽西州的官方结果按县分散在 ENR 系统中，缺少稳定的州级汇总接口）。
- 认证日之前所有数字均为非官方结果，各州认证日期通常在 11 月中下旬。

---

## 七、技术要点

- **零第三方 Python 依赖**：采集器只用标准库（`urllib` / `html.parser` / `subprocess`）。
- **ECharts 本地化**：`assets/echarts.min.js` 已随仓库提供，不依赖 CDN，可离线打开。
- **适配器设计**：
  - `wa_api_json` —— EnhancedVoting 官方 JSON（`results/public/api/elections/<州>/<选举>/data`），
    华盛顿州、弗吉尼亚州使用；
  - `enr_json` —— SOE Software ENR / Clarity 选举夜报告系统（州级汇总 `sum.json`），
    科罗拉多州、爱荷华州使用；
  - `tx_html` —— 德克萨斯州官方结果页的固定表格结构；
  - `generic_html` —— 通用表格抽取，按表头识别姓名列/党派列/票数列。

  新接一个州通常只需在 `state_sources.json` 里加一条配置 + 选一个适配器。
- **职位分类带州级护栏**：`STATE_LEVEL_GUARD` 先把 "State Board of Education Member –
  Congressional District 1" 这类州级职位挡在外面，避免被 "congressional district"
  之类的联邦关键词误判为众议员。
