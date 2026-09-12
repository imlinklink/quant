# 15只存续股票后续测试交接手册（2026-09-13）

## 0. 交接目标与当前结论

本手册交给下一位工程师执行两件事：**先完成可复现的工程联调，再启动从冻结后新增交易日开始的前向独立验证**。股票仅限当前质量门放行的15只普通股；用户不要求退市样本。当前已完成历史 `raw_asof` 工程审计及本地 Futu 来源一致性核验，**尚无独立前向收益结论**。旧 2016-01-01—2026-06-30 样本已被查看，只能用于诊断，不可再称“未见测试集”。

主要依据：[15只原始价审计](m2-raw-asof-raw-universe-audit-2026-09-12.md)、[来源核验和前向协议](m2-survivor15-provenance-and-forward-validation-2026-09-12.md)、[M2 原始价交接](m2-raw-asof-exit-and-abc-handoff-manual-2026-09-12.md)。本机 `data/` 默认不入 Git；交接时须保存文件哈希与不可覆盖的 run-id，而不只提交文档。

### 已知硬阻断（先读）

| 组件 | 当前实际行为 | 前向验证所需状态 |
|---|---|---|
| `run_daily_setups.py` → `FutuUSDataFetcher.fetch_stock_kline` | 请求固定 `autype='qfq'`；运行的是 shadow setup 状态机 | 不能直接声称它验证了历史 `raw_asof` 规则；须建立同口径原始价前向观察器或明确只作联调 |
| `run_daily_pipeline.py` | 下载与组装默认 `qfq`，旧股票池流程 | 不可直接用于原始执行价股票池；用 `download_market_history.py --autype none` 采集，并按 v2 原始价门构造 |
| `experiment_manifest.py` 命令行 | 创建时把开发/验证/测试时间写死为历史窗口，当前 `test_end=2026-08-31` | 前向正式 Manifest 需先支持显式时间参数或单独前向 schema；不能传 `--formal` 后仍登记旧测试期 |
| 当前 `data/shadow/config_shadow.yaml` | 39只观察池 | 前向15只实验须单独冻结15只列表与哈希；39只运行结果只能算系统 shadow 联调 |
| Futu 2026-09-12 当前 basicinfo | 15/15 与本地映射、首根日线一致，历史 `source_observed_at` 为空 | 不得把当前目录回填说成历史时点核实；前向每日归档真实采集时间 |

**2026-09-13 实施更新：**B1 的不可覆盖 `AuType.NONE` 日快照、行动快照和哈希校验已由 `capture_forward_raw_day.py` 实现；B2 的 T日 pending/T+1 原始开盘结算由 `run_forward_raw_day.py` 实现；B3 已支持 `--periods-json` 并校验冻结早于前向起点；B4 对 2026-06-22→23 做了15只工程回放，3/3 setup ID 与批处理一致、5条 A/B/C 入场、0拒绝。前向登记模板位于 `experiments/forward-survivor15-20260914/`。这轮历史回放约544秒；结算改为直接换算后降至约1.7秒，T日完整状态重建仍约271秒。

当前状态为 **`forward_frozen / awaiting_first_collection_session`**。正式实验统一使用 `FWD-SURV15-20260914-003`，Manifest 位于 `data/forward_runs/FWD-SURV15-20260914-003/frozen/manifest.json`，`formal=true`，收集期为 2026-09-14—2027-03-14。本轮代码基线全量测试为600项通过；结果成熟前不执行“正式前向绩效报告”。`001` 是首日前预检目录；`002` 是发现纯文档提交也会触发严格 `GIT_COMMIT_MISMATCH` 后保留的冻结演练，不作为正式运行。手册提交完成后才最后生成 `003`，此后正式收集期间不得切换 HEAD；若必须提交修复，关闭该 run-id 并创建新批次。

2026-09-13 已完成首日前工程预检：归档 2026-09-11 的 `AuType.NONE` 日线和行动快照，15/15 证券成功、哈希齐全；决策重建产生1条 `US.BAC` pending setup。预检产物固定放在 `preflight/2026-09-11/`，**不计入**正式 A/B/C 样本。2026-09-14 日线完成并归档后，可以单独结算该预检信号以验证 T+1 链路，但结算输出也必须留在 `preflight/`，不得写入正式 `entries/`。

实现后的每日命令骨架如下；`SESSION` 必须是已收盘的纽约交易日，`RUN_ID` 与输出目录每日唯一。结算命令在 T+1 快照完成后执行，`--pending` 和 `--prior-actions` 必须指向 T 日产物：

```bash
python3 scripts/data/capture_forward_raw_day.py \
  --codes experiments/forward-survivor15-20260914/codes.csv \
  --symbols data/security_master_runs/futu-survivor39-20260912/symbol_history.csv \
  --session SESSION --output-dir data/forward_runs/RUN_ID/snapshots/SESSION

python3 scripts/data/run_forward_raw_day.py --mode decision --day SESSION \
  --baseline-daily data/m2_raw_audit/M2-RAW-AUDIT-20260912-002/raw_daily_verified_v3.csv.gz \
  --baseline-actions data/corporate_actions_runs/futu-survivor39-20260912/corporate_actions.csv \
  --snapshots-dir data/forward_runs/RUN_ID/snapshots \
  --codes experiments/forward-survivor15-20260914/codes.csv \
  --config experiments/forward-survivor15-20260914/strategy_config.yaml \
  --master data/security_master_runs/futu-survivor39-20260912/security_master_v2.csv \
  --symbols data/security_master_runs/futu-survivor39-20260912/symbol_history.csv \
  --output-dir data/forward_runs/RUN_ID/decisions/SESSION

python3 scripts/data/run_forward_raw_day.py --mode settle --day NEXT_SESSION \
  --baseline-daily data/m2_raw_audit/M2-RAW-AUDIT-20260912-002/raw_daily_verified_v3.csv.gz \
  --baseline-actions data/corporate_actions_runs/futu-survivor39-20260912/corporate_actions.csv \
  --snapshots-dir data/forward_runs/RUN_ID/snapshots \
  --codes experiments/forward-survivor15-20260914/codes.csv \
  --config experiments/forward-survivor15-20260914/strategy_config.yaml \
  --master data/security_master_runs/futu-survivor39-20260912/security_master_v2.csv \
  --symbols data/security_master_runs/futu-survivor39-20260912/symbol_history.csv \
  --pending data/forward_runs/RUN_ID/decisions/SESSION/pending_setups.csv \
  --prior-actions data/forward_runs/RUN_ID/snapshots/SESSION/corporate_actions.csv \
  --output-dir data/forward_runs/RUN_ID/entries/NEXT_SESSION
```

当前决策实现为确定性全历史重建，15只约271秒；只运行一个实例，不用并发任务写同一 run-id。T+1 结算约1.7秒。实际首次运行前先执行各脚本 `--help` 并保存退出码。

## 1. 环境、责任人与安全边界

在仓库 `quant_us-main/` 下运行文中命令。要求 Python 依赖已装、Futu OpenD 已登录并拥有美股历史日线权限；需要前向 LLM 联调时才要求当前模型配置可用。所有测试和前向观察维持 `selection: shadow`、`buy_strategy_v2.mode: shadow`、`account_scope: DRY-RUN`；不运行 `run_all.py --real`，也不点击确认台下单。配置可能含密钥，交接文档只记 SHA-256，不复制内容到日志或 Git。

开始时记录操作者、上海/纽约时间、`git rev-parse HEAD`、`git status --short`、Python 版本、配置文件路径与哈希。若 Git 脏工作区属于其他工程，不清理别人的改动；创建实验 Manifest 的工作区必须由负责人隔离为干净 checkout。Futu SDK 导入会写用户日志，测试在受限沙箱收集失败时，应在有相应权限的本机环境执行并保留完整退出码，不把收集失败记为测试通过。

在本机核对已有产物：

```bash
cd /Users/wh1817w/Documents/quant/quant_us-main
git rev-parse HEAD
git status --short
python3 scripts/experiment_manifest.py --validate data/m2_raw_audit/M2-RAW-AUDIT-20260912-003/engineering_manifest.json --root .
python3 -m pytest -q tests/unit/live/test_audit_futu_survivor_provenance.py tests/unit/live/test_buy_strategy_experiment_runner.py
```

旧 `engineering_manifest.json` 固定的是提交 `eee42ac`，在后续新 HEAD 上运行 `--validate` **预期返回 `GIT_COMMIT_MISMATCH`**。该文件仍是旧工程审计的证据；不要修改它使校验变绿。只校验文件哈希可用 `validate_manifest(..., require_git=False)`，新运行则创建新编号、新 Manifest。

## 2. 阶段 A：当天可做的基线复核

1. 查看 `data/m2_raw_audit/M2-RAW-AUDIT-20260912-003/audit_summary.json`：固定应有 4,001 setup、8,048 A/B/C 入场、35 股票池拒绝、354,112 矩阵单元、351,332 `good`、2,780 `right_censored`、0 `quality_rejected`。这些数字**仅验证本机历史工程产物未变**，不作为未来交易日的固定目标。
2. 查看 `data/m2_raw_audit/M2-SOURCE-CHECK-20260912-003/summary.json` 与 `security_provenance.csv`：15/15 `source_consistent`，但 `listing_historically_verified=0`。核对输入 SHA-256；若文件不同，另建目录重跑来源审计，不改旧产物。原始 Futu basicinfo 在 `data/source_archive/futu_basicinfo/RUN-20260912/raw/security_master.csv`。
3. 逐只记录 15 个代码：`US.AAPL, US.AMD, US.AMZN, US.ARM, US.BAC, US.CRWV, US.GOOGL, US.HD, US.INTC, US.LITE, US.META, US.MSFT, US.MU, US.NFLX, US.NVDA`。映射必须唯一；`ORCL/QCOM/TSM/UNH` 仍因行动历史缺口隔离。
4. 执行相关单测后在本机跑全量 `python3 -m pytest -q`。当前交接基线为 **592 passed、16 warnings**；后续测试数可随代码变化，但失败为0。保存命令、退出码、完整日志位置与 commit。

需要复核来源时，用**新目录**替换以下输出路径，七个输入必须仍来自同一批次；目录已有内容时脚本会拒绝覆盖：

```bash
python3 scripts/data/audit_futu_survivor_provenance.py \
  --quality data/survivor_sample_audit/research_quality_intervals-v3.csv \
  --basic data/source_archive/futu_basicinfo/RUN-20260912/raw/security_master.csv \
  --master data/security_master_runs/futu-survivor39-20260912/security_master_v2.csv \
  --symbols data/security_master_runs/futu-survivor39-20260912/symbol_history.csv \
  --bars data/m2_raw_audit/M2-RAW-AUDIT-20260912-002/raw_daily_verified_v3.csv.gz \
  --actions data/corporate_actions_runs/futu-survivor39-20260912/corporate_actions.csv \
  --action-checks data/survivor_sample_audit/price_action_checks.csv \
  --output-dir data/m2_raw_audit/M2-SOURCE-CHECK-NEW-ID
```

检查 `summary.json.issues` 为空。未来除息日尚无两侧日线并不等于历史缺口；必须区分 `future_actions_without_bars` 与 `unexplained_missing_sides`。真正失配、重复主键、上市日晚于首根 bar、代码映射歧义都阻断该证券入样，不以手工填 `verified` 绕过。

## 3. 阶段 B：补齐前向原始价接线（开发任务，完成前不得宣布开始验证）

**B1 原始价采集与不可变日快照。**`download_market_history.py --autype none` 已有，但它按年份维护缓存，增量下载可能更新同年分区；它不是天然不可变的“每日证据快照”。开发者应在每个纽约交易日收盘后，使用固定15只代码和 `AuType.NONE` 拉取完成的日 K，保存 `trade_date × code × observed_at` 的原始响应、请求参数、页数、SHA-256、OpenD 返回状态和异常原因到新 run-id。保持原先日线作为热身窗口，但冻结后的决策只用当时已经归档的快照。SPY 等用于市场/行业门的参考证券也须同时归档、声明角色，不能把它们算进15只交易样本。

已有采集器可先做**只读能力检查**；下例使用另建的 15只 `code` 清单 CSV，下载到独立目录，具体日期由交易日历确定。它只证明能取得原始日线，不自动完成逐日 as-of 冻结：

```bash
python3 scripts/data/download_market_history.py \
  --master data/forward_runs/15-stock-codes.csv \
  --start YYYY-MM-DD --end YYYY-MM-DD --autype none \
  --output-root data/forward_runs/RAW-RUN-ID/cache \
  --checkpoint data/forward_runs/RAW-RUN-ID/checkpoint.json
```

`15-stock-codes.csv` 至少有 `code` 列、恰好上述15只且不重复，实际冻结版须写入 run 目录并记录哈希。采集程序要拒绝空响应、未完成日 K、重复 `code/date`、负成交量、价量异常；缺一只时留失败记录和拒绝分母。不能把 QFQ 值重命名为 `raw_close`。单测至少覆盖分页、重复请求幂等、同年缓存变化、新/旧 SHA、早到未完成 bar、OpenD 错误。

**B2 前向计算与一致性。**把每日原始快照按 `security_id` 接到 `generate_historical_setups.py --price-basis raw_asof` 的同一特征构造语义；T日收盘后才计算 setup，T+1 原始开盘价必须到来后才能登记实际入场，不能在 T 日用未来开盘创建成功交易。公司行动仅在 `ex_date <= 决策日` 时进入该日特征；T+1 跨行动日的止损、高开门换算与现有历史 `raw_asof` 路径一致。日线不含当天分钟路径时沿用现有 OHLC 退出假设并披露，跳空按原始开盘价。新增 `price_basis/price_version` 检查，拒绝与 QFQ 股票池混接。

逐日 T−1 流动性与股票池复用 `build_daily_liquidity.py`、`build_historical_universe_v2.py` 的逻辑，但后者输入要求 `security_id,date,previous_raw_close,adv20_usd,liquidity_as_of`，而前者直接输出的是 `code,previous_close,adv20`；中间必须经 `symbol_history` 映射并显式转换字段。前向运行不得复制旧 `SURVIVOR39-QFQ` universe。对每个交易日输出候选/质量拒绝/股票池拒绝/可入场/成交/未成交漏斗，并以 `setup_id, security_id, decision_time, observable_at` 去重。

**B3 Manifest 与报告时间窗口。**现有 CLI 创建 Manifest 时写死历史 `test_end=2026-08-31`。先新增显式的前向 `collection_start/collection_end/outcome_end`（或独立前向 schema），并测试交叉区间、当前代码提交、输入哈希、唯一 run-id、15只清单、版本、费用、质量表必填；不要通过改系统日期或直接编辑生成的 JSON 来冒充预登记。`buy_strategy_report.py` 当前按完整 A/B/C × E1–E11 × 四成本检查矩阵，样本尚在积累时不可运行最终报告；可以做独立的覆盖/质量进度表。

**B4 联合回放。**用冻结前已存在的 15只历史日线做一次**纯工程回放**：同一截止日、同一输入下，批处理与逐日重放的 setup ID、阶段、T+1 入场、质量拒绝和退出会计一致。特别包含拆股前后、除息、跳空穿止损、右删失、跨代码映射失败、当天未完结 bar。回放数据已被查看，不能进入前向绩效。B1—B4 测试全绿且有代码审查记录，才将状态改为 `forward_capture_ready`。

## 4. 阶段 C：冻结实验（首个前向交易日之前）

以 [前向协议](m2-survivor15-provenance-and-forward-validation-2026-09-12.md)为接受标准，不按 B4 的回放收益修改。冻结记录至少包含：新实验编号、干净 commit、完整15只清单与纳入日期、NYSE/Nasdaq 交易日历版本、配置哈希、原始价/行动/质量表版本、A/B/C 定义、E1–E11、总成本 0.1/0.2/0.5/1.0%、主切片 E5+0.2%+C−A、六个月连续收集窗、40交易日结果等待、缺失与右删失政策、样本不足判 `inconclusive` 的阈值。D 组没有历史时点 LLM 标签，单列 `inconclusive`，不可生成伪标签。

开始日期必须是**冻结后首个真实美股交易日**，不能在看到那天收盘行情后回填冻结。正式 Manifest 需 `formal=true` 且时间字段真实反映前向区间；现有旧工程 Manifest 保持不可覆盖。`experiment_manifest.py --validate` 与独立校验脚本都应通过，并归档清单 SHA-256 和 UTC 创建时间。若开发中修改规则、成本、样本或主指标，关闭旧 run-id，登记新编号，原 run-id 保留为失败/诊断证据。

## 5. 阶段 D：每个前向交易日的标准作业

这些是**阶段 B 完成后的操作要求**，不是现有仓库已可直接复制执行的一键命令。具体新增命令及参数由 B 阶段开发者填入 run-id 专属 `commands.md`，以 `--help` 和实际退出码核对后交给运行人员。

| 时点 | 动作 | 必留产物 | 通过条件 |
|---|---|---|---|
| 开盘前 | 核对前日快照、行动与证券映射；记录当前代码/配置哈希 | `preflight.json` | 15只清单不变；无未解释质量异常 |
| T 日收盘且日 K 完成后 | 拉取 `AuType.NONE` 日线及参考证券；保存原始响应与观察时间 | `raw_response`、`daily_snapshot`、SHA-256 | 已完成日 K、日期/代码/价格/成交量有效 |
| 同一时点 | 仅以 T 日及更早快照运行 `raw_asof` setup；写规则和拒绝日志 | `setups`, `rejections`, `decision_ledger` | 决策时间不早于所有所用数据 `observed_at` |
| T+1 开盘后 | 用实际原始开盘与当日行动判断是否入场；不要补填无法成交信号 | `entries`, `non_fills` | T+1 严格晚于 setup；现金/保护线同尺度 |
| 后续各日 | 更新 E1–E11 退出；先查既有止损，再更新下日保护线 | `exit_events`, `right_censored` | 跳空价、拆股股数、现金分红会计一致 |
| 每日结束 | 审核摘要，不看尚未到期的绩效判定 | `daily_quality.json`, `checksums.json` | 行数闭合、无静默丢失或订单事件 |

如果团队仍运行 `run_daily_selection.py`、`run_daily_setups.py` 或确认台，仅作为**39只/LLM/系统 shadow 的并行联调**，使用明确的 shadow 配置和 DRY-RUN 账本；不要把这些 QFQ setup、LLM 研究卡片或人工观察按钮混入15只 `raw_asof` 前向 ABC 结果。任何订单事件须立即停止并保留日志。

## 6. 阶段 E：结算、统计和验收

连续六个自然月收集期结束后，再等最长40个实际交易日，并核实每笔交易的观察窗已到期或被显式右删失。每个 entry 应对应 11退出×4成本=44 行；A/B/C 分母、质量拒绝、组合最多三仓的拒绝均可追溯。`good` 行核对

`gross_pnl_pct = (shares_at_exit × exit_price + cash_dividend_per_initial_share) / entry_price − 1`，`net_pnl_pct = gross_pnl_pct − cost_scenario`；右删失行不得写已实现收益。抽查全部拆股/行动日期、所有跳空止损、随机日常交易。成本、日内路径和幸存者样本限制写入报告。

先产出唯一主比较 E5、成本0.2%、C−A 的独立组联合 bootstrap 差值及95%区间，**同时**给出参与率、组合收益/回撤、证券与月份集中度。主切片任一组少于100笔已到期交易、少于30独立周组，或资料缺口超过5%，结论 `inconclusive`。满足门槛且区间下界>0、组合层方向一致，才写“本次前向样本支持继续研究”；区间跨零写“未定”，方向相反写“不支持”。其他 E1–E11/成本情景仅为敏感性分析，不能挑最优值替代主指标。A/B/C 的结果不能证明 LLM 增量；D 继续 `inconclusive`。

报告必须列出每日覆盖、总候选、入场漏斗、质量失败、未成交、右删失、行动事件、成本/退出切片与限制。冻结后的任何修复以新代码版本、新 run-id 另起，不静默修改已存快照；旧批次可以标 `invalidated` 并说明原因。结果只用于研究决策，不改变真实资金权限。

## 7. 异常处置与交付清单

| 异常 | 立即动作 | 何时恢复 |
|---|---|---|
| OpenD 无响应、权限或部分股票数据缺失 | 保存返回码/缺失代码，标数据质量失败；不使用前日/复权价替代 | 当日完整数据可按预登记时限取得且观察时间可证明，否则当日保留缺口 |
| 代码映射歧义、上市日期冲突、行动日期/比例/现金额冲突 | 隔离受影响证券和区间，保留拒绝分母 | 新证据核验并新版本重新登记 |
| 当前配置或代码哈希与冻结值不同 | 停止该 run-id 的前向决策 | 找回冻结环境，或建新 run-id |
| 未来 bar 进入 T 日特征、T 日信号被当日成交 | 判该批次无效，保存错误样本 | 修复并回放单测后从新批次开始 |
| 右删失被记为盈利、行动日机械跳空被记为止损 | 停止绩效报告 | 会计修复与全量回归通过，旧报告标无效 |
| 测试收集受 Futu 日志权限阻断 | 在授权的本机环境重跑并存日志 | 取得实际 `pytest` 通过结果 |

每周交接一页状态：负责人和时间、run-id、commit、配置/15只清单/数据哈希、准确命令与退出码、已完成/缺失交易日数、候选和拒绝漏斗、质量异常证券/日期、是否发生订单事件、下一步。最终交付原始日快照、质量与拒绝表、冻结 Manifest、每日账本、退出矩阵、统计脚本输出及结论记录；`data/` 被 gitignore 时需另行按团队方式备份，但不得把密钥写入共享仓库。

**当前下一动作：**不要提前采集或推断 2026-09-14 的完成日 K。等该纽约交易日收盘且 Futu 返回完整日线后，先写入 `snapshots/2026-09-14/`，再运行正式 `decisions/2026-09-14/`；同时可用同一份已归档快照将 2026-09-11 的预检 pending 结算到 `preflight/2026-09-14-entry/`。正式信号只能在下一实际交易日快照到来后结算，所有操作保持只读行情、shadow/DRY-RUN，不产生订单或真实资金流。
