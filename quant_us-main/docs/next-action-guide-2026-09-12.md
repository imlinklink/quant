# 下一步行动指南（2026-09-12）

> 2026-09-13 交接入口：后续15只股票测试的执行顺序、当前阻断、逐日留痕和验收标准，见 [15只前向测试交接手册](survivor15-forward-test-handoff-manual-2026-09-13.md)。

> M2 的最新可执行交接步骤见 [原始价退出链路与 A/B/C 重跑交接手册](m2-raw-asof-exit-and-abc-handoff-manual-2026-09-12.md)。下文 A 节保留最初实施顺序；实时状态以其后的更新及重跑记录为准。

> 2026-09-12 更新：公司行动退出会计和质量门已完成，v3 放行的15只完成原始价股票池复核，见 [最新审计](m2-raw-asof-raw-universe-audit-2026-09-12.md)。此前 [15只重跑记录](m2-raw-asof-15-stock-rerun-2026-09-12.md)的入场筛选沿用了旧 QFQ 股票池，入场和矩阵结果已被取代。当前应先冻结可复现的工程清单并核验主数据来源，再决定正式 ABC 实验；ORCL/QCOM/TSM/UNH 在行动历史补齐前继续排除。

> 后续核验：15只的本地 Futu 来源链检查与前向独立验证协议见 [来源核验及前向验证登记](m2-survivor15-provenance-and-forward-validation-2026-09-12.md)。本地一致性已通过；历史来源时点仍未获独立证明。历史窗口已被查看，独立验证只使用冻结后新增行情。

## 当前判断

**主工程任务：完成 M2 价格与特征口径接线，但在 M1 质量门放行前不发布新收益结论。并行运维任务：用已登记的 39 只配置启动 M3 前向 shadow，先解决旧批次对账失败。**两项互不等待：M2 消除历史实验的数据口径问题，M3 依靠真实交易日积累 LLM 增量证据。不得用回测结果决定是否修复、调整规则阈值或修改已冻结产物。

当前基线与边界见 [最新架构](system-architecture-2026-09-12.md)、[M1 审计](m1-data-audit-record-2026-09-12.md)、[M2 接线记录](m2-wiring-record-2026-09-12.md)和 [M3 批次 0](m3-shadow-batch0-config-check-2026-09-12.md)。本地 `data/` 下的原始行情、审计表、shadow 配置和 SQLite 不纳入 Git。用户明确不纳入退市样本；历史结论仅针对选定存续证券。18/40 上市日未知，`US.HON` 行动未解析；这两个缺口不得被默认值掩盖。

## A. M2 接线：下一位工程师先做这四步

1. **锁定输入和不变量。**为每只标的固定同批不复权 OHLCV、公司行动、样本选择日、`security_id`、原始文件哈希。`asof_feature_panel.py` 生成的 T 日特征只含 T 日已生效行动和已完成 bar；执行日开盘价来自不复权行情。跨行动日的 T 日止损/高开门要换算到 T+1 原始尺度。无前收导致行动因子不可计算、跨证券行动、重复日线键、ticker 歧义时阻断该证券区间，留下拒绝原因。新修复已使面板遇到无法计算的行动因子显式失败；`attach_execution_prices.py` 改用开盘比率，但它只适用于旧 QFQ setup 的迁移诊断。
2. **接入完整 setup。**改造 `scripts/data/generate_historical_setups.py`，让周线、日线 MA/ATR、回撤、枢轴、稳定化、状态机与 `signal_close` 都使用同一决策日的 as-of 视图。当前面板只提供日线 MA/ATR；不能只替换这几个字段却继续让周线和状态机消费全快照 QFQ。保持原有 setup ID、候选分母和拒绝日志可追溯；如改变特征语义，提升 `feature_version` 并另建实验编号。
3. **接入入场与退出。**`next_open_price` 使用 T+1 不复权开盘，执行价 `$5` 门和成交规则用原始价；`initial_stop`、`signal_close`、`atr14` 在决定执行时转换到 T+1 原始尺度。`exit_matrix.py` 输入必须是同一批不复权日线，遇拆股、股息和未解析行动按审计政策处理，不得把除权机械跳空误当止损或把分红漏算后直接称总回报。先验证“同一证券无行动区间旧/新信号相同”，再验证拆股日、除息日及 `US.HON` 的阻断。
4. **冻结新实验，最后打开收益。**新 run-id、Manifest、配置/数据/行动/质量文件哈希、样本选择日、成本、A/B/C 分组和预登记比较指标必须先写入不可覆盖目录。18 只上市日未知的历史区间只可排除或标未核验，不能写 `verified`。质量门通过才运行 `exit_matrix.py --groups ABC` 与报告；旧 `SURVIVOR-003` 只作诊断对照。输出应逐项说明有多少 setup、入场、退出因价格口径改变或质量门被拒绝，不能仅比较最终收益。

M2 最小确定性测试：拆股前 T 日特征不含未来拆股；`feature_drift` 的两种对照截在同一决策日；跨拆股/除息执行日的保护线与原始开盘价同尺度；未解析行动失败关闭；多证券行动不能串用；QFQ 迁移工具不读取执行日收盘来算开盘换算；原始价缺失不会被前复权价补齐。全量测试在允许富途本机日志写入的环境下执行。接线完成仍要经 M1 放行审查，不能仅以测试全绿认定策略有效。

## B. M3 前向 shadow：下一交易日即可开始

已选方案 A：本机未跟踪的 `data/shadow/config_shadow.yaml` 将 selection 与 setup 的三个观察池都登记为 39 只。**每条命令显式传 `--config`**；不传会退回被跟踪 `config.yaml` 的 9 只旧池。运行前记录配置 SHA-256、`git rev-parse HEAD`、本机 OpenD 地址及是否已有调度服务。确认只有一个进程写 `data/execution.sqlite3`，且配置维持 `selection: shadow`、`buy_strategy_v2.mode: shadow`、`account_scope: DRY-RUN`。不得启动 `run_all.py --real`。

在美股交易日收盘、日 K 完成后由拥有该账本的本机执行：

```bash
python3 scripts/live_trading/run_daily_selection.py --config data/shadow/config_shadow.yaml --dry-run
python3 scripts/live_trading/run_daily_selection.py --config data/shadow/config_shadow.yaml
python3 scripts/live_trading/reconcile_selection_decision.py --config data/shadow/config_shadow.yaml --json
python3 scripts/live_trading/run_daily_setups.py --config data/shadow/config_shadow.yaml --json
```

`run_daily_selection.py` 没有 `--json` 参数；第二条会调用当前配置的模型并落研究批次。每条记录运行时间、退出码、批次/decision ID、股票池数、packet 数、模型 attempt、接受/观察/拒绝/资料不足数、setup 引用数及是否出现任何订单事件。旧最新批次对账 `passed=false`，其中有 `US.MU counterevidence` 引用包外证据；**不可放松引用校验或删除失败批次**。如果新批次仍失败，按保存的 packet 与模型原始输出定位 ID 的生成/映射和结构化输出错误，修好后用新批次复核；`passed=true` 才计有效批次。

至少两个独立交易日、累计三个有效批次只证明管道可联调，不能证明 LLM 产生收益优势。同一输入反复调用不能凑批次数。1/3/5/10/20 个交易日 outcome 到期后再用 `python3 scripts/live_trading/run_outcomes.py --config data/shadow/config_shadow.yaml --latest` 结算，未到期留 `pending_future_bars`。历史 D 组严格证据仍为 0，保持 `inconclusive`。

## C. 判断顺序与交接产物

| 检查点 | 通过条件 | 未通过时 |
|---|---|
| C1 新代码安全性 | 相关测试和全量测试通过；新构件无静默缺失或跨证券污染 | 修代码和回归测试，不跑新绩效 |
| C2 历史数据口径 | 同批原始执行价、逐日 as-of 完整 setup、公司行动和上市日质量门可审计 | M2 继续 `blocked`；保留拒绝分母 |
| C3 前向决策账本 | 39 只配置实际生效、单次模型 attempt、`reconcile passed=true`、无订单副作用 | 保留失败 ID 与日志；不计有效批次 |
| C4 LLM 增量 | 同候选/时点/成本的规则对照，结果已到期，缺失和失败在分母 | `inconclusive`，继续 shadow |

每次交接提交一页记录：代码 commit、配置哈希、数据哈希、准确命令、测试数、行数/拒绝漏斗、失败样本、修复决定与下一动作。`config.yaml` 曾被纳入 Git 且历史中存在非占位模型密钥；任何共享或推送前应按既有安全记录处理密钥，不把本机 shadow 配置或密钥输出到日志/文档。本文只规划 DRY-RUN/shadow 和历史研究，不提升任何真实订单权限。
