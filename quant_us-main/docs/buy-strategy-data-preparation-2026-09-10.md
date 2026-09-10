# 买入策略实验数据准备规范（2026-09-10）

> 适用项目：`quant_us-main`  
> 适用实验：买入策略 A/B/C/D 与退出策略 E1-E12 组合实验  
> 目标：生成可审计、可复现、无未来数据泄漏的历史时点股票池、日线、15 分钟行情和流动性输入。

## 1. 为什么需要单独准备数据

现有实验基础设施可以冻结配置、股票池和数据文件，也可以生成 A/B/C/D 入场、E1-E12 退出矩阵及固定格式报告。但实验是否可信，首先取决于输入是否满足以下条件：

- 股票在当时确实已经上市且尚未退市；
- 股票当时满足流动性条件，而不是根据今天的观察池倒推；
- 技术指标只使用决策时刻已经产生的数据；
- 日线和 15 分钟行情采用一致的复权、时区和交易时段；
- API 分页、权限或限流没有造成静默截断；
- 数据质量不合格的区间被明确拒绝，不会进入交易样本。

因此，原始行情不能直接送入实验 runner。必须经过采集、标准化、质量检查、历史股票池构建和 manifest 冻结。

## 2. 实施前必须修复的两个问题

### 2.1 流动性门存在未来数据泄漏

当前 `scripts/historical_universe.py` 使用同一天的 `close` 和 `dollar_volume` 决定当天是否进入股票池。如果策略在当天开盘或盘中入场，当日收盘价和全天成交额在决策时还不可知。

正确口径是：

```text
交易日 T 的股票池资格
= T-1 收盘后已经确定的价格
+ 截至 T-1 的历史流动性统计
```

建议使用：

```text
previous_close >= 5 美元
ADV20_as_of_previous_session >= 5,000,000 美元
20 日窗口内至少有 15 个有效交易日
```

流动性指标必须按股票执行 `shift(1)`，确保交易日 T 不读取 T 日尚未结束的数据。

### 2.2 工作日不等于美股交易日

当前脚本使用 `pd.bdate_range` 生成 session，其中会包含部分美国市场休市日，也不能准确表达临时休市和提前收盘。

必须改为真实的 NYSE/Nasdaq 交易日历，并在质量检查时区分：

- 正常交易日；
- 提前收盘日；
- 正常休市日；
- 临时休市；
- 股票自身停牌或无交易。

这两个问题修复前，不应冻结正式的 `BUY-V2-EXP-001`。

## 3. 数据目录

```text
data/
  market_history/
    raw/
      security_master/
      daily/
      intraday_15m/
    normalized/
      security_master.csv
      daily/
        year=2015/
        year=2016/
        ...
      intraday_15m/
        year=2015/
        year=2016/
        ...
      daily_liquidity.parquet
    quality/
      security_master_quality.csv
      daily_quality.csv
      intraday_15m_quality.csv
      coverage_summary.csv
    checkpoints/
      download_state.json

backtests/
  buy_v2/
    input/
      universe.csv
    BUY-V2-EXP-001/
      manifest.json
      config.yaml
      universe.csv
      data_quality.csv
      signals.csv
      rejected_signals.csv
      trades.csv
      outcomes.csv
      metrics.json
      report.md
```

目录规则：

1. `raw` 只追加，不原地清洗或覆盖。
2. `normalized` 由可重复运行的程序生成。
3. 大型行情文件使用 Parquet，并按年份或股票分区。
4. `quality` 保存每次标准化对应的质量结果。
5. 正式实验目录禁止覆盖；任何输入变化都创建新的实验 ID。
6. 原始数据、标准化数据和实验产物原则上不提交 Git，只提交生成代码、schema、质量摘要和 manifest。

## 4. Security Master

### 4.1 最小必需字段

现有 `historical_universe.py` 要求：

```csv
code,listing_date,delisting_date,asset_type
US.AAPL,1980-12-12,,stock
US.MU,1984-06-01,,stock
US.SOXL,2010-03-11,,leveraged_etf
```

| 字段 | 含义 | 规则 |
|---|---|---|
| `code` | 系统证券代码 | Futu 格式，如 `US.AAPL` |
| `listing_date` | 首个可交易日 | `YYYY-MM-DD`，不能为空 |
| `delisting_date` | 最后可交易日 | 仍上市则为空 |
| `asset_type` | 资产分类 | 至少支持 `stock`、`etf`、`leveraged_etf` |

### 4.2 建议扩展字段

```text
name
exchange
cik
currency
country
is_test_security
is_adr
is_etf
is_leveraged
leverage_multiple
underlying_index
former_codes
source
source_record_id
as_of
```

扩展字段用于处理 ticker 变更、普通股与 ETF 分层、SEC 事件关联及数据来源审计。

### 4.3 数据源原则

SEC 的 ticker/CIK/exchange 文件适合关联当前公司和 EDGAR，但不是完整的历史上市证券数据库。Nasdaq Symbol Directory 也主要是当前挂牌快照。这两者可以用于交叉验证当前证券，不能单独恢复十年前已经退市的全部股票。

退市日期需要来自：

- SEC Form 25-NSE；
- 交易所上市和退市历史；
- 或提供 point-in-time security master 的专业数据源。

正式的全市场结论应使用包含已退市股票、ticker 变更和历史资产分类的 point-in-time 数据源。

在正式数据源准备完成前，可以先运行兼容性试验：选取 20～50 只代表性证券，覆盖大中盘普通股、小盘股、次新股、ETF、杠杆 ETF 和当前观察池。该结果只证明实验管道能运行，不能证明策略对全市场有效。

### 4.4 Security Master 质量门

以下情况必须标记失败：

- `code` 为空或重复；
- `listing_date` 为空；
- `delisting_date < listing_date`；
- `asset_type` 不在允许枚举内；
- ticker 变更被错误地当作两个独立公司且没有映射；
- ETF 或杠杆 ETF 被归为普通股票；
- 已知退市证券缺少退市日期；
- 同一证券存在相互冲突的来源记录。

## 5. 历史日线数据

### 5.1 建议 schema

```text
stock
date
raw_open
raw_high
raw_low
raw_close
adj_open
adj_high
adj_low
adj_close
volume
turnover
adj_factor
source
fetched_at
```

最低可接受字段：

```text
stock,date,open,high,low,close,volume
```

但只保存一套 OHLC 会使成交价格、技术指标和公司行动处理难以审计，因此正式数据建议同时保存原始价格和复权价格。

### 5.2 复权口径

- 策略特征和技术指标使用统一的前复权价格；
- 成交模拟优先使用当时可交易的原始价格；
- 保存复权因子，保证二者可以核对；
- 成交量必须与价格复权口径一致；
- 股票拆分、合股和大额分红附近必须进行专项检查；
- 同一次实验不得混用不同数据源或不同复权口径的 OHLC。

### 5.3 时间范围

当前实验划分：

| 区间 | 时间 |
|---|---|
| 开发集 | 2016-01-01 ～ 2020-12-31 |
| 验证集 | 2021-01-01 ～ 2023-12-31 |
| 测试集 | 2024-01-01 ～ 2026-08-31 |

为了计算 MA200、ATR、相对强度和初始 setup，实际下载起点至少提前一年：

```text
建议采集范围：2015-01-01 ～ 2026-08-31
```

测试集只能在策略、参数、成本口径和验收标准冻结后使用。

## 6. 历史 15 分钟行情

### 6.1 建议 schema

```text
stock
timestamp
session_date
open
high
low
close
volume
turnover
session
source
fetched_at
```

### 6.2 时间与交易时段

- `timestamp` 标准化后统一保存为 UTC；
- `session_date` 使用 America/New_York 对应的交易日期；
- 保留原始数据源时间戳，便于复核转换；
- 第一轮正式实验只使用常规交易时段 `09:30–16:00 ET`；
- 盘前和盘后数据如需保留，必须标记 `session`，不能混入常规时段；
- 正常完整交易日应有 26 根 15 分钟 K 线；
- 提前收盘日按交易日历计算理论 bar 数；
- DST 切换由时区数据库处理，禁止用固定 UTC 偏移转换美东时间。

### 6.3 下载方式

Futu 历史 K 线接口可以获取日线和 15 分钟 K 线，也支持复权类型、分页和美股扩展时段。采集器必须：

1. 按股票和时间片分页；
2. 保存每个股票、周期和年份的下载检查点；
3. 检查返回的第一页和最后一页；
4. 验证分页游标已完全耗尽；
5. 对限流、超时和临时失败进行退避重试；
6. 某只股票失败时继续其他股票，最终输出失败清单；
7. 重跑时跳过已通过哈希和覆盖率检查的分区；
8. 不把空响应自动解释成停牌或退市。

如果 Futu 权限或历史覆盖不足，应换用或补充数据源，不允许用当前行情回填历史缺口。

## 7. 每日流动性数据

### 7.1 建议 schema

```text
date
code
previous_close
dollar_volume
adv20
median_dollar_volume_20d
valid_observations_20d
liquidity_as_of
quality
```

### 7.2 计算方法

原始每日成交额优先使用数据源提供的 `turnover`。缺失时可采用：

```python
dollar_volume = raw_close * raw_volume
```

但必须在元数据中记录计算方式。

交易日 T 的流动性数据按以下方式计算：

```python
daily = daily.sort_values(["code", "date"])
daily["previous_close"] = daily.groupby("code")["raw_close"].shift(1)
daily["adv20"] = (
    daily.groupby("code")["dollar_volume"]
    .transform(lambda s: s.rolling(20, min_periods=15).mean().shift(1))
)
daily["median_dollar_volume_20d"] = (
    daily.groupby("code")["dollar_volume"]
    .transform(lambda s: s.rolling(20, min_periods=15).median().shift(1))
)
```

是否进入交易日 T 的股票池，只能读取 `liquidity_as_of < T` 的数据。

### 7.3 初始门槛

```text
previous_close >= 5 美元
adv20 >= 5,000,000 美元
valid_observations_20d >= 15
```

门槛属于实验配置，冻结后不能根据测试结果调整。普通股票、ETF和杠杆 ETF 必须分别统计。

## 8. 数据质量检查

### 8.1 通用检查

- 股票代码和字段完整；
- 日期和时间戳可解析；
- 主键无重复；
- OHLC 满足 `low <= open/close <= high`；
- 价格为正；
- 成交量和成交额非负；
- 数据没有落在上市前或退市后；
- 数据源、下载时间和文件哈希可追踪。

### 8.2 日线检查

- 按真实交易日历计算理论交易日数；
- 股票有效上市区间的覆盖率不低于 98%；
- 第一根和最后一根数据符合预期；
- 无未解释的连续缺口；
- 公司行动附近的复权因子和收益跳变合理；
- 不存在由 API 分页截断导致的固定长度数据集。

### 8.3 15 分钟检查

- 正常完整交易日 bar 数为 26；
- 提前收盘日 bar 数符合日历；
- 没有重复时间戳或跨日错位；
- 常规时段实验不包含盘前盘后 bar；
- 15 分钟聚合后的日 OHLC 与日线数据在容忍范围内一致；
- 入场时点之后的数据不会进入入场特征。

### 8.4 质量处理

关键行情缺失或覆盖率低于 98% 时：

```text
quality = quality_fail
```

该股票区间仍保留在质量报告和拒绝记录中，但不能产生实验交易。不得删除失败样本后只报告剩余结果。

## 9. 历史时点股票池

交易日 T 的候选证券必须同时满足：

```text
listing_date <= T
delisting_date 为空或 T <= delisting_date
asset_type 在允许范围内
T-1 可知的 previous_close 达标
T-1 可知的 ADV20 达标
数据质量通过
```

输出 `universe.csv` 至少包含：

```text
universe_date
code
asset_type
previous_close
adv20
eligible
quality
reason
```

拒绝原因必须可枚举，例如：

```text
NOT_YET_LISTED
ALREADY_DELISTED
ASSET_TYPE_EXCLUDED
PRICE_TOO_LOW
DOLLAR_VOLUME_TOO_LOW
INSUFFICIENT_LIQUIDITY_HISTORY
MISSING_LIQUIDITY
DATA_QUALITY_FAIL
```

正式构建命令预计为：

```bash
python3 scripts/historical_universe.py \
  --master data/market_history/normalized/security_master.csv \
  --liquidity data/market_history/normalized/daily_liquidity.parquet \
  --start 2016-01-01 \
  --end 2026-08-31 \
  --min-price 5 \
  --min-dollar-volume 5000000 \
  --output backtests/buy_v2/input/universe.csv
```

当前脚本只读取 CSV；支持 Parquet 可作为实现数据管道时一并增加的能力。

## 10. 实验冻结

质量检查通过并生成历史股票池后，先提交数据管道代码，保证 Git 工作区干净，再创建实验目录：

```bash
python3 scripts/experiment_manifest.py \
  --experiment-id BUY-V2-EXP-001 \
  --config docs/trading-upgrade-settings.yaml \
  --universe backtests/buy_v2/input/universe.csv \
  --data \
    data/market_history/normalized/security_master.csv \
    data/market_history/normalized/daily_liquidity.parquet \
    data/market_history/normalized/daily/daily.parquet \
    data/market_history/normalized/intraday_15m/intraday_15m.parquet \
    data/market_history/quality/coverage_summary.csv \
  --output-dir backtests/buy_v2/BUY-V2-EXP-001 \
  --root .
```

Manifest 必须记录：

- Git commit 和 dirty 状态；
- 配置文件路径、大小和 SHA-256；
- universe 文件路径、大小和 SHA-256；
- 所有行情和质量文件的路径、大小和 SHA-256；
- 开发、验证和测试区间；
- 固定随机种子；
- 全部成本情景。

冻结后禁止修改或覆盖输入。任何配置、代码、股票池或数据修订都必须创建新实验 ID，并通过 `parent_id` 关联原实验。

## 11. 需要实现的数据工具

建议新增以下工具：

| 工具 | 职责 |
|---|---|
| `scripts/data/build_security_master.py` | 合并当前证券、上市/退市、ticker 变更和资产分类 |
| `scripts/data/download_market_history.py` | 分页下载日线和 15 分钟数据，支持断点续传 |
| `scripts/data/normalize_market_history.py` | 统一字段、时区、交易时段和复权口径 |
| `scripts/data/build_daily_liquidity.py` | 使用 T-1 可知数据生成流动性特征 |
| `scripts/data/validate_market_history.py` | 生成逐股逐区间质量报告和覆盖率摘要 |
| `scripts/data/trading_calendar.py` | 提供真实美股 session 和提前收盘信息 |

同时修改：

- `scripts/historical_universe.py`：使用真实交易日历；读取滞后一日的流动性字段；支持 CSV/Parquet；检查 `liquidity_as_of`。
- `scripts/experiment_manifest.py`：正式实验时要求包含数据质量摘要，并可选校验目录级数据清单。

## 12. 分阶段执行

### 阶段 A：管道验收

使用 20～50 只代表性证券：

1. 构建小型 security master；
2. 下载 2015 至 2026 年日线；
3. 下载同区间 15 分钟常规时段数据；
4. 标准化并生成流动性；
5. 运行质量检查；
6. 构建历史 point-in-time universe；
7. 跑通 A/B/C/D 和 E1-E12；
8. 核对若干股票、日期和交易的原始数据。

阶段 A 只验收管道正确性，不据此决定策略是否有效。

### 阶段 B：正式历史股票池

1. 接入包含退市证券的 point-in-time security master；
2. 补齐普通股票历史行情；
3. 独立生成 ETF 和杠杆 ETF 子样本；
4. 达到覆盖率要求；
5. 冻结 `BUY-V2-EXP-001`；
6. 运行预先登记的正式实验。

### 阶段 C：增量维护

正式历史集完成后：

- 每个交易日收盘后追加日线和 15 分钟数据；
- 更新流动性特征和质量摘要；
- 不修改已经冻结的实验数据；
- 新增数据仅用于新的实验版本或 shadow outcome。

## 13. 用户侧前置条件

开始开发数据管道前，需要确认：

1. Futu OpenD 可以正常登录；
2. 当前账户具有美股历史日线权限；
3. 当前账户具有所需年份的 15 分钟历史行情权限；
4. 是否可以接受阶段 A 存在幸存者偏差、只用于管道验收；
5. 正式实验是否准备使用专业 point-in-time 数据源。

除数据权限和正式数据源选择外，其余采集、标准化、质量检查和实验冻结均应由代码自动完成。

## 14. 完成标准

只有同时满足以下条件，数据准备才算完成：

- security master 覆盖实验股票且通过字段、日期和分类检查；
- 日线和 15 分钟数据完成分页下载，无静默截断；
- 时区、交易时段和复权口径已固定；
- 每日流动性只使用 T-1 可知信息；
- point-in-time universe 使用真实美股交易日历；
- 关键行情覆盖率不低于 98%；
- 质量失败样本进入拒绝记录；
- 数据文件和质量报告均有 SHA-256；
- Git 工作区干净；
- 正式实验 manifest 创建成功并能通过独立校验。

