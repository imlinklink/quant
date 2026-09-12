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

## 尚未完成（M1 剩余项）

1. **不复权日线批次 + 公司行动原始响应 + 哈希**：需按 §4.2 为 39 只另存**同一批次的不复权日线**（QFQ 只作交叉比较，不得充当原始成交价）。此前仅下载了 QFQ；不复权需另跑一次下载（`fetch_pages(..., autype=AuType.NONE)`）。
2. **价格/行动逐只审计**：除权日前后股数与价格转换、美元成交额、跳空退出、分红处理；未解析并购/分拆标 `corporate_action_unresolved` 并剔除绩效。产出 `price_action_checks.csv`。
3. **ticker 映射失败清单** `ticker_mapping_failures.csv`。
4. **`source_manifest.json` / `quality_summary.json`**，逐项注明证据来源、问题证券/日期、处理决定与复核人。

## 放行判定（§4.3）

**M1 未放行**：a) 18 只上市日未核验（历史区间不得入 M2 的已核验集）；b) 不复权执行价与逐日 as-of 特征价尚未构造。按方案，此时 **M2 标 `blocked`**，可并行推进 M3 前向 shadow（但须在拥有账本的本机执行）。

## 下一步

按 §4.2 下载 39 只**不复权**日线并保存原始响应哈希，随后产出 `price_action_checks.csv` 与 `quality_summary.json`；`build_price_views.py` 现要求 `--as-of`，逐日回放须按每个决策时点重建 as-of 视图（不得用 2026 年一张快照算所有历史均线）。
