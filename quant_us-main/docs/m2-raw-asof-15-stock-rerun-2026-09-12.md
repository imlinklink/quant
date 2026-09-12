# M2 raw_asof 15只质量放行样本重跑（2026-09-12）

## 结论

使用 `research_quality_intervals-v3.csv` 放行的15只普通股完成第二轮不可覆盖工程审计，状态为 **`engineering_pass`**。本轮没有质量区间拒绝；新旧 setup 在相同股票和截止日下 4,001/4,001 全部配对，证明首轮118个单边 setup 全部随 ORCL/QCOM/TSM/UNH 的行动历史缺口被隔离。

这仍不是正式冻结收益实验：历史区间已经被查看，样本是当前存续股票，并且尚未创建绑定干净 commit、预登记和全部输入哈希的正式 Manifest。本文不报告或选择任何收益最优参数。

## 输入冻结

- 本机目录：`data/m2_raw_audit/M2-RAW-AUDIT-20260912-002/`（gitignore，不覆盖首轮目录）。
- 股票：AAPL、AMD、AMZN、ARM、BAC、CRWV、GOOGL、HD、INTC、LITE、META、MSFT、MU、NFLX、NVDA。
- 不复权日线：39,182行，2015-01-02至2026-09-10。
- setup 决策区间：2016-01-01至2026-06-30。
- 质量区间截至2026-08-31；提前截止 setup，确保最长40交易日观察窗有核验覆盖。
- 公司行动仍使用本机 `futu-survivor39-20260912/corporate_actions.csv`；v3 已隔离行动历史不完整的四只。

## 数量漏斗

| 阶段 | 数量 |
|---|---:|
| `raw_asof` setup | 4,001 |
| T+1 恰逢行动、价格门需换算 | 31 |
| universe 前 A/B/C 入场 | 8,083 |
| universe 接受 / 拒绝 | 7,889 / 194 |
| A / B / C | 3,507 / 2,915 / 1,467 |
| E1–E11 × 4成本矩阵 | 347,116 |
| `good` | 344,456 |
| `quality_rejected` | **0** |
| `right_censored` | 2,660 |
| 组合约束后接受 | 78,804 |

右删失来自40日观察窗内仍未触发退出的动态退出方案，`gross_pnl_pct` 为空，不当作已实现收益。矩阵约136.7秒完成。

## 一致性检查

- 对全部 `good` 行按 `(shares_at_exit × exit_price + cash_dividend_per_initial_share) / entry_price - 1` 重算，总回报与记录值最大绝对误差 **4.996e-16**。
- 352行经历拆股，32,300行包含现金分红；保护线、结构枢轴和持股数量均使用行动后的同一每股尺度。
- 相同15只、相同结束日、按 `stock + session + strategy` 去重：raw_asof 4,001，旧 QFQ/full-snapshot 4,001，共同4,001，双方单边均为0。
- 这一配对只证明候选方向及时点在当前15只中保持一致；真实执行价、分红总回报和退出路径仍应以 raw_asof 矩阵为准。

机器可读汇总与输入/输出 SHA-256 位于本机 `audit_summary.json`。核心矩阵为 `exit_matrix_raw_asof.csv.gz`。

## 下一步放行条件

1. 审阅当前代码 diff，提交后获得干净 commit，并重跑全量测试。
2. 将本轮数据、行动、质量区间、配置、universe 和接受标准冻结到新 Manifest；正式实验使用新编号，不能把本审计目录改名冒充。
3. 预登记 A/B/C、E1–E11、4种成本和报告指标后，运行正式 `--groups ABC` 报告。
4. ORCL/QCOM/TSM/UNH 只有在取得并核验完整行动后才能重新加入；否则持续排除并在样本边界中披露。

达到以上条件后，状态才可从 `engineering_pass` 提升到 `research_ready_abc`。D 组仍为 `inconclusive`。
