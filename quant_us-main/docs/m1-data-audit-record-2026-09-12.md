# M1 里程碑记录：39 只候选证券数据真实性审计（2026-09-12，进行中）

依据 `next-stage-handoff-plan-2026-09-12.md` §4。数据与审计文件留在本机 `data/`（gitignore），本文只记录方法与结论。

## 已完成：逐证券主数据核验

命令（可复现）：读取 run `SURVIVOR39-QFQ-20260912-b/security_master.csv` 与富途 basicinfo 归档，输出 `data/survivor_sample_audit/security_quality.csv`。

| 项目 | 结果 |
|---|---|
| 证券数 | 40（**39 普通股 + `US.SPY`**） |
| 资产类型（核验后） | stock 39 / etf 1（SPY） |
| 富途 `stock_type` | STOCK 37 / ETF 3 —— **其中 2 只被误标** |
| **修正** | `US.AMT`（American Tower）、`US.PLD`（Prologis）实为股票，被富途标成 ETF；已纠正。注意名称匹配须用**词边界**：`Netflix` 含子串 `etf`，否则会误判 |
| 上市日有真实值 | 22 / 40 |
| **上市日占位或未知** | **18 / 40** |

### 缺口清单（§4.3：不得标 `verified`）

上市日为 `1970-01-01` 占位或未知，其历史区间**不能标为已核验**，均记 `unverified`：

```
US.AMT  US.AVGO  US.AXTI  US.CAT  US.COHR  US.CVX  US.DIS  US.DUK  US.JNJ
US.JPM  US.KO    US.NBIS  US.NEE  US.PG    US.PLD  US.SHW  US.SPY  US.XOM
```

（`US.SPY` 仅作交易日历，不入主样本。）有真实上市日的 22 只可直接用于区间判定。

## 已完成：不复权日线与价格/行动对账（§4.2）

- 下载器新增 `--autype none`（`download_market_history.py`）；为 40 只下载**不复权**日线：**449 个分区、0 失败**，落在 `raw/day/none/year=YYYY/`（与 QFQ 分目录，互不覆盖）。
- 为 39 只取公司行动 **1601 条**（1545 分红 + 56 拆股）。
- 逐条核对窗口内（2015 起）行动：把 QFQ/不复权 的复权因子步长与行动预期对比。
  - **窗口内行动 850 条，一致 843 条（99.2%），不一致 3 条**。
  - 不一致**全部在 `US.HON`**：2026-06-29 的"拆股"步长 0.916≠0.5，且其因子出现 >1（0.75~1.09），符合**分拆/非简单拆股**特征 → 标 **`corporate_action_unresolved`**，M2 中剔除其绩效。
  - （先前看起来"不一致"的 BAC/CAT/AMZN 等，是**窗口外的历史拆股**，属正常，不计入。）
- 产出：`data/survivor_sample_audit/price_action_checks.csv`、`price_action_mismatches.csv`、`quality_summary.json`。

## 已完成：逐日 as-of 特征价（§4.2 硬前提）

- 新增 `scripts/data/asof_features.py`：
  - `asof_features(bars, actions, decision_day)` —— 只用 `ex_date <= decision_day` 的行动构造 as-of 序列，再算均线/ATR；
  - `full_snapshot_features(...)` —— 对照用的**错法**（as_of=最新）；
  - `feature_drift(...)` —— 量化两者的相对差异。
- 4 项测试（含拆股 fixture）：**拆股前**的决策日，正确特征保持拆股前价格尺度（100），错法被未来拆股缩到 50（差 100%）；拆股后两者收敛。

### 前视影响量化（本样本）

窗口内有拆股的 8 只，`setup` 落在拆股日之前者共 **1526 / 11290（13.5%）**：

| 标的 | 首次拆股 | 拆股前 setup |
|---|---|---:|
| US.HON | 2025-10-30 | 357 |
| US.AVGO | 2024-07-15 | 270 |
| US.GOOGL | 2022-07-18 | 204 |
| US.NEE | 2020-10-27 | 192 |
| US.AMZN | 2022-06-06 | 181 |
| US.NVDA | 2021-07-20 | 170 |
| US.AAPL | 2020-08-31 | 152 |
| US.NFLX | 2015-07-15 | 0 |

**判断**：这些 setup 的**特征尺度**被未来拆股改变，但本策略的信号多为**尺度不变**（周线门用比值、止损/高开门用 close±k·ATR），且样本内拆股前价格均远高于 $5 门槛，故**方向性影响预计很小**。但按方案要求，**M2 必须**用**不复权执行价 + 逐决策日 as-of 特征**重建，不能用 QFQ 快照充当执行价或历史均线。

## 尚未完成（M1 剩余项）

1. **ticker 映射失败清单** `ticker_mapping_failures.csv`。
2. **`source_manifest.json`**（原始响应与哈希、下载时间、请求参数）。

## 完成（M1 全部审计产物）

| 产物 | 结果 |
|---|---|
| `security_quality.csv` | 40 只；资产类型/交易所/上市日质量；2 只 ETF 误标已纠正 |
| `price_action_checks.csv` / `price_action_mismatches.csv` | 窗口内 850 条行动，843 一致（99.2%），3 条不一致（全在 HON） |
| `ticker_mapping_failures.csv` | **空**（40/40 映射成功，0 未映射/0 歧义） |
| `source_manifest.json` | QFQ 520 分区、不复权 449 分区、检查点、basicinfo 归档、公司行动原始响应的聚合哈希 |
| `quality_summary.json` | 汇总（含 `corporate_action_unresolved: [US.HON]`） |

全部位于 `data/survivor_sample_audit/`（本机，gitignore）。

## M1 放行判定（§4.3）

| 门 | 状态 |
|---|---|
| `security_id` 映射无歧义 | **通过**（0 失败） |
| T 日 universe 只用 T−1 价格/流动性 | **通过**（代码强制 + 测试） |
| 未解析公司行动计数并阻断 | **通过**（US.HON 已标） |
| 执行价来自**不复权**序列、特征价按决策时点构造 | **未通过**——SURVIVOR-003 用的是 QFQ 执行价与全快照特征；已备好不复权数据与 as-of 构造器，须在 M2 重建 |
| 18 只上市日未知的历史区间不得标 `verified` | **已登记缺口**，M2 须据此排除或降级 |

**结论：M1 未放行 → 按方案 M2 标 `blocked`**。可在 M2 前完成的前置已就绪（不复权数据、as-of 特征构造器、行动对账）；M2 需用**不复权执行价 + 逐决策日 as-of 特征**重建 A/B/C，并与 `BUY-WD-ABC-SURVIVOR-003` 分离、另建实验编号。

## 放行判定（§4.3）

**M1 未放行**：a) 18 只上市日未核验（历史区间不得入 M2 的已核验集）；b) 不复权执行价与逐日 as-of 特征价尚未构造。按方案，此时 **M2 标 `blocked`**，可并行推进 M3 前向 shadow（但须在拥有账本的本机执行）。

## 下一步

按 §4.2 下载 39 只**不复权**日线并保存原始响应哈希，随后产出 `price_action_checks.csv` 与 `quality_summary.json`；`build_price_views.py` 现要求 `--as-of`，逐日回放须按每个决策时点重建 as-of 视图（不得用 2026 年一张快照算所有历史均线）。
