# 下一阶段交接执行方案（2026-09-12）

## 1. 交接目标与当前结论

下一阶段同时推进两件事：**冻结并核实存续普通股 A/B/C 的数据口径**，以及**从冻结日以后积累真实交易日的 LLM selection shadow 决策与结果**。先验证系统确实在同一批股票、同一时点留下完整可回放的决策，再判断 LLM 在选股、买入和卖出各环节是否有可测增量。不要为了让确认台出现信号而降低规则门槛。

用户已决定**不纳入退市样本**。所有历史结论只针对从当前存续证券中核验为普通股的样本，不得称为历史全市场或无幸存者偏差。39 只候选证券已经登记在 [样本框架](sample-frame-registration-2026-09-12.md)，资产类型仍需逐只核验；历史测试区间的收益已被查看，因此这段历史不能重新包装成盲测。D 组严格历史证据仍为 0，保持 `inconclusive`；富途财报缺少可信 `observed_at`，只能做诊断。39 只 QFQ 回放也不能替代原始执行价与逐日 as-of 特征价的审计。

本次复核后的本地代码尚有未提交改动。相关测试 **62 项通过**，在允许富途库写入本机日志的测试环境中全量 **548 passed、16 warnings**。接手者必须先记录新的 commit 与测试结果，不要把旧交接文档中的 `HEAD` 或通过数当作当前值。此前 `BUY-WD-ABC-SURVIVOR-001/002/003` 均保留为诊断产物，不能覆盖、修改 Manifest 或借新代码直接宣称旧实验通过。

## 2. 完成标准

| 里程碑 | 交付物 | 放行判据 |
|---|---|---|
| M0 代码基线 | commit、工作区状态、全量测试日志、变更清单 | 干净工作区；测试结果可复现；未改旧实验产物 |
| M1 数据质量 | 39 只逐证券质量表、原始价与公司行动审计、缺口清单 | 上市日、ticker、价格、公司行动的未知项逐一标明；未经核验项不得写成 `verified` |
| M2 ABC 重新冻结 | 新 run-id、Manifest、A/B/C 132 单元、分层报告 | 使用合格原始执行价和正确时点特征；预登记规则与成本未因结果调整；无未解释质量失败 |
| M3 前向管道联调 | 至少 2 个独立美股交易日、累计 3 个有效研究批次、每批对账记录 | 输入、模型 attempt、输出、setup 引用及无订单副作用均可对账；这只证明管道工作 |
| M4 增量观察 | 选股、买入、卖出各自的对照定义与到期 outcome | 分母包含拒绝、资料不足和失败；相同候选/时点/成本比较；样本未成熟时结论 `inconclusive` |

M1 与 M3 可以并行。M2 等待 M1；买入/卖出 LLM 权限升级等待 M3、可观测的 outcome 和单独审查，不因 Web 确认台为空就升级。

## 3. M0：先冻结这次复核代码

执行位置：`/Users/wh1817w/Documents/quant/quant_us-main`，Git 根目录是父目录。先读 [复核后的工作总结](handoff-work-summary-2026-09-12.md) 与 [技术设计](historical-universe-and-evidence-handoff-technical-design-2026-09-12.md)，确认本轮修复涉及 `price_views`、`evidence_store`、`replay_historical_selection` 及对应测试。记录：

```bash
git status --short
git diff --check
python3 -m pytest -q
git rev-parse HEAD
```

若富途库在沙箱内因用户目录日志权限导致测试收集失败，使用允许写入该**本地日志目录**的受控测试环境重跑；不要把收集失败写成代码测试失败。本轮已验证过该方式，得到 `548 passed`。接手者提交修复时应另建 commit，提交前审查 diff；提交后再次记录 `HEAD` 与干净工作区。现有实验 Manifest 绑定旧 commit，报 `GIT_COMMIT_MISMATCH` 是预期行为，不得编辑旧 Manifest 来绕过。

## 4. M1：先做数据真实性审计，再谈历史收益

### 4.1 样本及来源

固定 2026-09-12 登记的 39 只候选证券。检查主数据每只证券的 `security_id`、资产类型、交易所、真实上市日、当前 ticker 和已知改名历史。富途 `1970-01-01` 等占位上市日不能视为真实日期；只有可核对来源的日期才能升级为 `verified`。Futu 按当前 ticker 生成的稳定 ID 不能自行证明历史改名时仍是同一证券。缺历史映射时标缺口或排除对应区间，不要推断。ETF、杠杆 ETF、ADR 与普通股分开；不补退市证券。

核对富途批量下载、本地保留和研究使用条件，结论写入 [来源评估](source-assessment-template-2026-09-12.md)。旧评估中仍有退市探针及待决策文字，按本次范围决定忽略；不要为了通过测试而运行退市探针。Futu OpenD 连接地址依执行环境而定：Mac 本机通常 `127.0.0.1:11111`，此前沙箱联调使用 `172.16.10.254:11111`；先用只读探针确认，不把后者写成普遍固定地址。

### 4.2 价格与公司行动

为 39 只保存同一数据批次的不复权日线、直接取得的拆合股/股息表及原始响应哈希。QFQ 只用于交叉比较，不得充当原始成交价。逐只检查拆股、反向拆股、现金股息及未解析的并购/分拆：检查除权日前后股数与价格转换、美元成交额、跳空退出和分红处理。没有来源证明的行动不假定不存在；不完整区间标 `corporate_action_unresolved` 并从绩效中剔除。汇总每证券每年覆盖率、重复 bar、异常 OHLC、缺失交易日、T−1 流动性时间、ticker 映射失败数。

`build_price_views.py` 现在**必须**传 `--as-of YYYY-MM-DD`，输出的 as-of 特征视图只含决策日及以前的 bar。接手者若做多年回放，应按每个历史决策时点重建或等价地实现逐日 as-of 视图，并用拆股前后 fixture 验证；不能用 2026 年的一张最终复权快照给所有历史决策计算均线。

建议保留如下审计文件，放入新的不可覆盖 run 目录：`security_quality.csv`、`price_action_checks.csv`、`ticker_mapping_failures.csv`、`source_manifest.json`、`quality_summary.json`。每项注明证据来源、问题证券/日期、处理决定和复核人。只有没有阻断性问题的证券日期进入 M2；其余作为拒绝分母保留。

### 4.3 M1 放行门

- `security_id` 映射无歧义，上市前 bar 不可入选；上市日未知的历史区间不能被标为已核验。
- T 日 universe 只使用 T−1 已知价格与流动性；改 T 日成交额不影响 T 日纳入结果。
- 执行价来自不复权序列；特征价按决策时点构造；未来 bar 与未来公司行动不进入过去快照。
- 未解析公司行动、数据缺口、来源冲突均在报告中计数和阻断，不被最后一行或缺省值覆盖。
- 若达不到以上条件，M2 标为 `blocked`，继续 M3 的前向 shadow，不生成新的“正式收益结论”。

## 5. M2：仅在质量门通过后冻结新的 A/B/C 实验

执行前登记新实验编号、样本名单与选择日、代码 commit、数据版本/哈希、开发/验证/测试日期、11 种退出、4 种成本、拒绝/右删失规则、普通股主样本和预设接受标准。与 `BUY-WD-ABC-SURVIVOR-003` 分离，不覆盖任何旧目录。Manifest 登记原始价和每决策日特征价的生成方法，不能只填版本字符串却把 QFQ 直接送入执行模拟。

按顺序完成：主数据和公司行动审计 → 不复权行情及逐日特征 → `security_id` 桥接 → 历史 universe → setup → Manifest → runner → `exit_matrix.py --groups ABC` → `buy_strategy_report.py --groups ABC`。每步保存命令、输入/输出哈希、行数、拒绝数与失败样本。命令的具体参数以脚本当前 `--help` 为准；不要照搬旧文档中的占位路径或 2026-09-12 的历史 run-id。

报告至少单列：普通股、ETF、杠杆 ETF；每年、每成本、每退出模板的 A/B/C 独立交易数、期望收益与区间、右删失比例、集中度、共同候选与增量差。39 只样本已被用来看过测试期结果，所以新报告应称“存续样本历史再现/敏感性分析”，把真正前瞻观察单列。若 C−B 仍为负，应呈现而非改日线确认参数寻找正结果。

## 6. M3：从下一交易日开始前向 shadow

先核对 `config.yaml`：`llm_decision.engine_v2.selection: shadow`、`buy_strategy_v2.mode: shadow`、`llm_decision.engine_v2.account_scope: DRY-RUN`；确认本机只有一个实例写 `data/execution.sqlite3`。**不要运行 `run_all.py --real`，不要打开订单权限**。无需迁移数据库副本。若已有调度服务在运行，先确认它是否会自动触发同一作业，避免手工重复启动。

尤其核对两个股票池：`run_daily_selection.py` 使用 `dip_buy.watch_list ∪ trend_breakout.watch_list`；`run_daily_setups.py` 优先使用 `buy_strategy_v2.watch_list`，否则退回 `dip_buy.watch_list`。它们不会自动等于登记的 39 只。接手者先导出/记录本轮两份实际列表和交集；若要跑同一研究群组，在**第一次前向批次前**登记配置版本并使两者一致，不能在观察到收益后换股票池。若有意保留不同列表，只对共同候选做增量比较并报告各自分母。

在拥有 OpenD 和账本的本机，每个美股交易日收盘、行情完成后按此顺序执行，保存标准输出、退出码与时间：

```bash
python3 scripts/live_trading/run_daily_selection.py --dry-run
python3 scripts/live_trading/run_daily_selection.py
python3 scripts/live_trading/reconcile_selection_decision.py --json
python3 scripts/live_trading/run_daily_setups.py --json
```

`run_daily_selection.py` **没有 `--json` 参数**；其 `--dry-run` 只生成输入包而不调用 LLM，正式 shadow 命令会调用当前配置的模型并保存研究批次。`run_daily_setups.py` 可能因个别股票质量未通过返回非零，应保存逐只原因，不能只用进程退出码推断整批没有产生数据。`reconcile_selection_decision.py` 只核对最新研究批次；若批次重跑或调度并发，使用 `--decision-id` 指定对象。

每批逐项核对：股票池和数据截止时点、模型与 prompt 版本、packet/输入哈希、单次模型 attempt、结构化输出、接受/观察/拒绝/资料不足分母、setup 对 selection decision ID 的引用、`falling` 周线门与高开/止损拒绝、重跑幂等性、无 `order_intent_created`/`order_submitted`/`fill_received`。对账 JSON 的 `passed` 必须为真；失败时留下原始日志与账本 ID，先修数据/时序/映射，不放宽买入阈值。确认台为空不是失败判据，先查完整漏斗。

到期结果按 1/3/5/10/20 个交易日逐步补算，可用 `python3 scripts/live_trading/run_outcomes.py --latest` 对最新批次结算，再核对 `pending_future_bars` 是否随着时间转为有效结果。该命令写结果到账本，应由单个本机进程执行。最低联调量是**两个独立交易日、三个有效批次**；这不足以证明收益优势。实际每日只跑一次时，第三批至少需要第三个交易日，不能为了凑批次在同一天重复调用相同输入。

## 7. M4：让 LLM 逐步承担选股、买入、卖出的可测职责

按一个角色一个对照来做，不把三个角色同时升级。每个对照均固定相同候选、决策截止时刻、价格/成本和机械基线，保存 LLM 原始输出、引用证据、拒答/资料不足、规则最终动作与后续结果；禁止仅统计 LLM 接受的赢家。

1. **选股 selection（先做）**：比较同一基础池中的 LLM 排序/接受与预登记的规则排序、未入选股票及 SPY；记录覆盖率、方向、行业和同一主题集中度。优先回答“LLM 是否提供独立信息”，不只看被选股票绝对涨幅。
2. **买入 entry（随后）**：只在规则 setup 到达且可成交条件满足时，记录 LLM 的 `accept/defer/reject/missing` 与理由；用同一时点的机械入场作反事实基线。检查 T 日收盘信号最早 T+1 成交、跳空/止损/费用，先维持 shadow，不改变确认台及仓位。
3. **卖出 position（最后）**：先在既有持仓/模拟持仓上记录 LLM 建议与机械止损、超时、移动保护线的差异；硬止损不可由 LLM 放宽。对照必须含同一持仓的实际机械退出价与 LLM 首次建议后的可实现价格，不能只比较事后最高价。

任何一层在缺少独立决策数、结果尚未到期、引用证据无法复核、日志无法配对或收益区间不稳时维持 `inconclusive`。历史 D 只有在 `published_at`、`observed_at` 有可审计证明后才允许做**受限证据的回放探索**；不使用伪时间或伪标签。若未来考虑让 LLM 影响真实订单，需要另做权限、风控与人工审批设计；本交接范围只到 shadow。

## 8. 失败处理和交接包

| 失败表现 | 先检查 | 处理与状态 |
|---|---|---|
| 富途连接失败 | OpenD、主机地址、行情权限、是否已有调度进程 | 记录环境故障；不生成空行情“成功批次” |
| 上市日为 1970/空、ticker 冲突 | 来源原始响应、映射窗口 | 标 `unverified`/拒绝区间；不得填首根 K 线冒充上市日 |
| 原始价/公司行动对不上 | 除权日前后原始 bar、公告日期、ratio/cash | 阻断受影响证券日期的 M2 绩效 |
| 严格历史 packet 为空 | `observed_at` 来源证明与拒绝日志 | D=`inconclusive`；诊断包不得进严格回放 |
| selection 对账失败 | decision ID、attempt、快照、禁用副作用 | 保存 JSON 与账本 ID；修复后另起批次，不改旧记录 |
| setup/确认台为空 | 质量→selection→周线→日线→可成交漏斗 | 逐层计数；不因空台调低阈值 |
| outcome 长期 pending | 决策基准 bar、后续交易日数量、数据抓取 | 到期前不计算收益；缺行情保留缺口 |

接手者每完成一个里程碑提交一页 `docs` 记录：执行日期和操作者、commit、配置哈希、数据来源/许可、命令与退出码、输入/输出哈希、数量漏斗、失败样本、审阅结论及下一个可执行动作。数据与账本留本机，不把密钥、数据库或原始许可受限数据提交到 Git。交接时给出明确状态 `engineering_pass`、`research_ready_abc`、`forward_validation`、`inconclusive` 或 `blocked`；禁止用“代码能跑”替代策略有效性的判断。
