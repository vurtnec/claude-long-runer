# 市场信息采集方案草案

目标：让盘前、盘后 scheduler 专注于稳定收集信息、过滤噪音、沉淀上下文。不在这里决定交易动作，不修改现有触发时间。

## 总体原则

1. 数据优先级：官方数据源 > 有文档的免费 API > CSV/RSS > 网页抓取。
2. 模型只负责解释和归纳，不负责临场寻找全部数据。
3. 每次采集都落本地 JSON，保留来源、时间、原始值、变化值、链接和置信度。
4. 新闻不是越多越好，应该由数据异常和固定关注主题触发。
5. 免费源要接受延迟。这个系统适合日度/盘前/盘后判断，不适合实时交易。

## 账号和 API Key

### 必须或强烈建议注册的免费 key

| 来源 | 是否需要注册 | 用途 | 建议 |
| --- | --- | --- | --- |
| FRED | 需要免费账号和 API key | 美债、利率、信用利差、通胀预期、美元流动性 | 建议注册 |
| BEA | 需要免费 API key | GDP、PCE、个人收入、个人消费 | 建议注册；注册邮件里的 activation link 必须点开，否则接口会返回 `UserId is not active` |
| EIA | 需要免费 API key | 原油、天然气、库存、能源价格 | 建议注册 |
| Alpha Vantage | 需要免费 API key | 股票/ETF 日线、外汇、部分商品、部分新闻情绪 | 可注册，作为行情备源 |
| Guardian Open Platform | 需要免费 developer key | 英文新闻补充源 | 可注册，非必须 |

### 不需要注册即可先跑的源

| 来源 | 是否需要注册 | 用途 | 备注 |
| --- | --- | --- | --- |
| SEC data.sec.gov | 不需要 key | 公司 filings、10-K、10-Q、8-K、Form 4、XBRL | 需要设置合规 User-Agent |
| SEC RSS | 不需要 key | 最新 filings / SEC 动态 | 适合事件触发 |
| U.S. Treasury Daily Rates | 不需要 key | 名义利率、实际利率、收益率曲线 | 官方 XML/CSV |
| Cboe VIX historical CSV | 不需要 key | VIX、VVIX、GVZ、OVX 等波动率指数 | 官方日度 CSV |
| GDELT | 不需要 key | 全球新闻、地缘、主题热度 | 噪音大，需要过滤 |
| 官方 RSS | 不需要 key | Fed、BLS、BEA、Treasury、EIA 新闻发布 | 优先级高 |
| Google News RSS | 不需要 key | 关键词新闻 fallback | 非正式 API，只做备选 |

### 暂不建议作为核心依赖

| 来源 | 原因 |
| --- | --- |
| NewsAPI 免费层 | 免费层偏开发测试，有 24 小时延迟和请求限制，不适合作长期核心源 |
| Yahoo Finance 非官方接口 | 稳定性和条款不如官方/文档化 API |
| 直接抓财经网页正文 | 容易被反爬，版权和结构变动风险高 |
| 付费实时行情 | 你的需求不是实时交易，现阶段没有必要 |

## 建议的环境变量

放进 `.env`，没有 key 的源先跳过，不阻塞整体采集。

```dotenv
FRED_API_KEY=
BEA_API_KEY=
EIA_API_KEY=
ALPHA_VANTAGE_API_KEY=
GUARDIAN_API_KEY=

# SEC 要求自动访问提供身份信息；建议用真实邮箱。
SEC_USER_AGENT="vurtnec-loom market collector contact@example.com"
```

## 采集模块设计

建议新增 `collectors/`，每类源一个小模块，所有模块输出统一结构。

```text
collectors/
  __init__.py
  __main__.py
  base.py
  market_prices.py
  treasury_rates.py
  cboe_volatility.py
  fred_macro.py
  bea_macro.py
  eia_energy.py
  sec_filings.py
  news.py
  snapshot.py
```

统一输出：

```json
{
  "as_of": "2026-06-06T20:30:00+08:00",
  "source": "treasury",
  "source_url": "https://home.treasury.gov/...",
  "category": "rates",
  "symbol_or_series": "10Y",
  "name": "10-year Treasury yield",
  "value": 4.35,
  "previous_value": 4.28,
  "delta": 0.07,
  "delta_pct": 1.64,
  "unit": "percent",
  "freshness": "same_day",
  "confidence": "official",
  "notes": ""
}
```

## 盘前采集内容

盘前重点是“今天有哪些新信息和风险源”，不是给操作建议。

### 必采数据

| 类别 | 指标 |
| --- | --- |
| 美股 ETF / 指数代理 | VOO 或 SPY、QQQ、RSP、DIA |
| 波动率 | VIX、VVIX、GVZ、OVX |
| 利率 | 2Y、10Y、30Y、10Y-2Y、10Y real yield |
| 汇率和商品 | DXY 或美元代理、黄金、WTI/Brent |
| 事件日历 | Fed/FOMC、CPI、PCE、非农、GDP、零售销售、EIA 库存、重要财报 |
| SEC 事件 | 重点公司 8-K、10-Q、10-K、Form 4 |

### 固定新闻主题

| 主题 | 关键词 |
| --- | --- |
| Fed/利率 | Federal Reserve, FOMC, Powell, rate cut, inflation expectations |
| 通胀/就业 | CPI, PCE, payrolls, unemployment, wages |
| AI/大科技 | Nvidia, Microsoft, Apple, Amazon, Google, Meta, Broadcom, Tesla |
| 地缘风险 | oil supply, Iran, Russia, Ukraine, Taiwan, Red Sea |
| 黄金 | gold, central bank gold, dollar, real yields |
| 港股/中国 | Hang Seng, China stimulus, PBOC, Hong Kong stocks |

## 盘后采集内容

盘后重点是“今天实际发生了什么”，用于沉淀第二天上下文。

### 必采数据

| 类别 | 指标 |
| --- | --- |
| 收盘表现 | SPY/VOO、QQQ、RSP、DIA 收盘价、涨跌幅、成交量 |
| 市场宽度 | RSP vs SPY、QQQ vs SPY，用来判断是否少数权重股驱动 |
| 波动率 | VIX、VVIX、GVZ、OVX 收盘和变化 |
| 利率 | 2Y、10Y、30Y、收益率曲线变化 |
| 商品 | 黄金、WTI/Brent |
| 行业 | XLK、XLF、XLE、XLU、XLY、XLP、XLV |
| 重点公司 | NVDA、MSFT、AAPL、AMZN、GOOGL、META、AVGO、TSLA |
| 新闻归因 | 当天大幅波动是否能被官方发布、财报、SEC 文件或高可信新闻解释 |

### 输出文件

```text
data/market/
  snapshots/
    2026-06-06-premarket.json
    2026-06-06-postmarket.json
  latest_premarket.json
  latest_postmarket.json
  daily_market_context.json
```

`daily_market_context.json` 用于给 scheduler 的 inline prompt 读取，避免每次让模型重新搜。

## 新闻过滤规则

新闻采集不要全量推送，先入候选池，再筛。

### 候选池来源

1. 官方 RSS：Fed、BLS、BEA、SEC、Treasury、EIA、公司 IR。
2. GDELT：广覆盖新闻扫描。
3. Guardian：英文新闻补充。
4. Google News RSS：只做 fallback。

### 触发式搜索

只有当数据有明显变化时，才扩大新闻搜索。

| 数据变化 | 触发新闻搜索 |
| --- | --- |
| 10Y 单日上行/下行明显 | Treasury yields, Fed, inflation, auction |
| VIX 明显上升 | volatility, S&P 500 selloff, risk off |
| 黄金明显上涨 | gold, real yields, dollar, central bank, geopolitical |
| 油价明显上涨 | oil supply, EIA inventory, OPEC, Middle East |
| 大科技显著波动 | 公司名 + earnings/guidance/SEC/antitrust |
| 港股显著波动 | Hang Seng, China stimulus, PBOC, property, tariffs |

### 新闻评分

每条新闻打分后再交给模型摘要：

```json
{
  "title": "Example headline",
  "url": "https://example.com/news",
  "publisher": "Reuters",
  "published_at": "2026-06-06T12:00:00Z",
  "matched_topics": ["rates", "fed"],
  "source_rank": 4,
  "freshness_score": 5,
  "asset_relevance": ["VOO", "gold", "USD cash"],
  "dedupe_key": "sha256:title+publisher",
  "summary": "One or two sentence factual summary."
}
```

建议权重：

| 来源类型 | 分数 |
| --- | --- |
| 官方发布 / SEC 文件 / 公司 IR | 5 |
| Reuters / AP / Bloomberg / WSJ / FT 等一线媒体 | 4 |
| Guardian / CNBC / MarketWatch 等 | 3 |
| 普通媒体和聚合源 | 2 |
| 社媒、论坛、转述 | 1，默认不进最终摘要 |

## 模型摘要格式

盘前：

```text
今日信息雷达

1. 新增事实
- ...

2. 数据变化
- ...

3. 需要关注的事件
- ...

4. 噪音和不确定信息
- ...

5. 盘后需要验证
- ...
```

盘后：

```text
盘后事实归档

1. 市场实际表现
- ...

2. 主要变化来源
- ...

3. 官方/高可信新闻
- ...

4. 未确认或低可信信息
- ...

5. 明天盘前上下文
- ...
```

## 分阶段落地

## 当前命令行入口

已经可以用下面的命令生成本地快照：

```bash
python -m collectors --session premarket
python -m collectors --session postmarket
python -m collectors --session daily --skip-gdelt
```

快速测试时可以跳过 GDELT，避免等待新闻限流：

```bash
python -m collectors --session manual --symbols SPY,VOO,QQQ,GLD,USO --skip-gdelt
```

输出位置：

```text
data/market/latest_premarket.json
data/market/latest_postmarket.json
data/market/daily_market_context.json
data/market/snapshots/YYYY-MM-DD-session.json
```

### 第一阶段：不注册也能跑

先接：

1. Treasury Daily Rates XML/CSV。
2. SEC submissions API + SEC RSS。
3. Cboe VIX CSV。
4. GDELT DOC API。
5. 官方 RSS。
6. Google News RSS fallback。

这个阶段已经能覆盖利率、波动率、SEC 文件、官方新闻和全球新闻。

### 第二阶段：注册免费 key 后增强

接入：

1. FRED：宏观时间序列和信用利差。
2. BEA：GDP、PCE、个人收入。
3. EIA：油价和库存。
4. Alpha Vantage：ETF/股票日线备源。
5. Guardian：新闻补充。

### 第三阶段：质量控制

1. 对关键价格做双源校验。
2. 对新闻做去重和来源评分。
3. 每条摘要保留 source URL。
4. 采集失败不让 scheduler 整体失败，只标记 `missing_sources`。
5. 每周输出一次“数据源健康检查”。

## 官方参考链接

- FRED API key: https://fred.stlouisfed.org/docs/api/api_key.html
- BLS Developers: https://www.bls.gov/developers/
- BEA API signup: https://apps.bea.gov/api/signup/
- EIA Open Data: https://www.eia.gov/opendata/
- SEC EDGAR APIs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- SEC RSS feeds: https://www.sec.gov/newsroom/press-releases/rss-feeds
- Treasury Daily Rates: https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve
- Treasury XML feed: https://home.treasury.gov/treasury-daily-interest-rate-xml-feed
- Cboe VIX historical data: https://www.cboe.com/tradable-products/vix/vix-historical-data
- GDELT data: https://www.gdeltproject.org/data.html
- Alpha Vantage docs: https://www.alphavantage.co/documentation/
- Guardian Open Platform: https://open-platform.theguardian.com/access/
- NewsAPI pricing: https://newsapi.org/pricing
