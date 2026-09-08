# quant_us 决策优化路线（A–E）交付说明

对应 `docs/next-optimization-roadmap.md` 的任务 A–E。本文档记录每项任务的改动文件、验收清单、是否需要重启、未覆盖边界，供独立分支提交与后续验收使用。

建议按 A → B → C → D → E 的顺序拆成独立提交；A、B 完成后可先做一次模拟账户验收。

---

## 任务 A：决策可观测性

**目标**：区分 LLM 配置/尚未调用/成功/失败/资料不足；确认页展示不可批准原因与最近扫描时间；修正漏斗口径。

### 改动文件

| 文件 | 改动 |
| --- | --- |
| `scripts/live_trading/decision_ledger/decision_health.py` | 新增：`unapprovable_reason` 原因码、`llm_state`、`build_health`、`record_scan_heartbeat` |
| `scripts/live_trading/decision_ledger/funnel_report.py` | 改：规则拒绝不再显示为 `unhandled`，改为 `rule_rejected` |
| `web/app.py` | 改：新增只读 `/api/decision-health`；`/api/approvals` 每项附 `unapprovable_reason` + 中文 label |
| `web/templates/approvals.html` | 改：顶部健康摘要 + 卡片「暂不能下单」原因 |
| `scripts/live_trading/dip_buy_monitor.py` | 改：每轮扫描后调 `record_scan_heartbeat` |
| `scripts/live_trading/trend_breakout_monitor.py` | 改：同上 |
| `tests/unit/live/test_decision_health.py` | 新增：11 用例 |

### 验收清单

- [x] 规则拒绝/无提案 → `rule_rejected`，模型请求数 0
- [x] 已配置 LLM 未请求 → `never_called`（尚未调用）
- [x] 请求失败/资料不足 → 各自原因码且不能批准
- [x] 计划修订后旧评估清空，需重新评估
- [x] 账户作用域隔离（两个 namespace 互不混入）
- [x] 扫描心跳与「去重后信号事件时间」区分

### 重启

Web 服务（`run_all.py`）与两个监控器需重启后生效。

### 未覆盖边界

- `web/app.py` 的接口本环境无 `flask` 只做编译级验证，未做 HTTP 冒烟。
- 心跳用秒级去重控制事件量，长期运行 `decision_events` 仍会增长；如需可改为「只保留最新一条」的独立表。

---

## 任务 B：建议池时效与来源

**目标**：历史建议不被当作实时信号；区分报告说法与已核实数据。

### 改动文件

| 文件 | 改动 |
| --- | --- |
| `scripts/live_trading/llm_suggestions/freshness.py` | 新增：`parse_time`（时区/未来/无效/缺失）、`assess` |
| `scripts/live_trading/llm_suggestions/store.py` | 改：`_atomic_write`（临时文件+`os.replace`）+ `_file_lock`（`fcntl` 跨进程锁） |
| `scripts/live_trading/llm_suggestions/run_suggestions.py` | 改：`generated_at` 带时区（UTC）；`reports` 加 `pre_mtime`/`post_mtime` |
| `web/app.py` | 改：`/api/suggestions` 调 `assess` 附加时效字段 |
| `web/templates/suggestions.html` | 改：显示生成时间/距今/freshness/来源时间 |
| `config.yaml` | 改：新增 `llm_suggestions` 段（`fresh_seconds`/`stale_seconds`） |
| `tests/unit/live/test_suggestions_freshness.py` | 新增：10 用例 |

### 验收清单

- [x] 带时区新记录 / 无时区旧记录（标「时区未知」，不默认 UTC）
- [x] 陈旧来源 / 新生成但旧来源
- [x] 未来时间、损坏 JSON
- [x] 原子写无 `.tmp` 残留
- [x] 加入观察池不创建批准记录或订单（架构上 add 流程只改 config + 建议文件，不触碰 proposal/execution）

### 重启

Web 服务需重启；`run_suggestions.py` 为独立脚本，重新运行即生成带时区的新记录。老记录下次加载会显示「时区未知」。

### 未覆盖边界

- `web/app.py` `/api/suggestions` 未做 HTTP 冒烟（无 `flask`）。
- 「加入观察池不创建订单」依赖两条链路天然分离，未写集成冒烟，建议补一条。

---

## 任务 C：模拟联调可复跑工具

**目标**：用一致步骤留下证据，不靠目测日志。

### 改动文件

| 文件 | 改动 |
| --- | --- |
| `scripts/run_simulate_acceptance.py` | 新增：只读验收 + 闭环时间线 + 脱敏 JSON |
| `tests/unit/live/test_run_simulate_acceptance.py` | 新增：4 用例 |

### 验收清单

- [x] 默认只读，不触发 LLM/批准/下单/写库
- [x] 脱敏（不含 api_key/base_url/授权信息）
- [x] 数据库缺失标 warn「不是零持仓证据」，读失败标 fail
- [x] 账户作用域唯一性检查
- [x] 闭环时间线 signal → plan → review → proposal → order → fill → trade
- [x] DRY-RUN / SIMULATE / REAL 三种模式明确区分

### 重启

无需重启（独立只读脚本）。真实 SIMULATE 闭环需人工点单后回跑 `--trace`。

### 未覆盖边界

- 部分成交一致性、unknown 不重试、重启恢复等由离线 mock 测试覆盖（`test_broker_execution.py`、`test_deployment_acceptance.py`），本脚本只读不重复。
- PID/端口防重复启动是 `run_all.py` 进程管理职责，本脚本不启动监控，未纳入。

---

## 任务 D：成交更正与费用补齐

**目标**：终结订单新增成交/数量倒退进入明确更正流程，不静默改写；费用保持未知不冒充。

### 改动文件

| 文件 | 改动 |
| --- | --- |
| `scripts/live_trading/execution.py` | 改：`apply_report` 两处差异从 raise 改为 `_flag_reconciling`；新增 `apply_correction`、`_flag_reconciling`、`record_broker_sample` |
| `tests/unit/live/test_execution_correction.py` | 新增：6 用例 |

### 验收清单

- [x] 累计数量倒退 → 待核对，不静默改写
- [x] 终结订单新增成交 → 待核对 → `apply_correction` 应用
- [x] 费用缺失→补齐→上调→退款
- [x] 更正幂等（`correction_id` 重复回放不重复应用）
- [x] R0 冻结，更正不重写 `initial_r0`
- [x] 事务一致（`registry.transaction` 内）
- [x] 券商字段样例脱敏留存

### 重启

需重启系统（`run_all.py`）后生效。

### 未覆盖边界 / 需拍板

- **行为变化**：`apply_report` 对「终结订单新增成交」由原来 `raise`（中断当轮对账）改为「标记待核对 + 返回 `reconciling`」。这是文档要求的更正流程，但语义更宽容；若需上层感知差异，可让 `_flag_reconciling` 额外抛专用异常。
- 真实券商字段样例需先留存（`record_broker_sample`）再校准适配器，当前未接真实成交。

---

## 任务 E：补证据，再评估 LLM 效果

**目标**：按缺口补数据；样本外评估；不把单次成功/模型信心/少量盈利当证明。

### 改动文件

| 文件 | 改动 |
| --- | --- |
| `scripts/live_trading/decision_ledger/input_quality.py` | 新增：missing_information 分布 + 状态分布 |
| `scripts/live_trading/decision_ledger/sample_split.py` | 新增：按日期划分 train/val/test，重叠持仓不跨分 |
| `scripts/live_trading/decision_ledger/llm_effectiveness.py` | 新增：样本量/占比/费用覆盖率/净R/回撤/区间不确定性 |
| `tests/unit/live/test_evidence_quality.py` | 新增：4 用例 |

### 验收清单

- [x] 输入质量统计（缺口分布、资料不足/失败/过期占比、延迟）
- [x] 样本外划分（按 signal 日期，无效日期进 unassigned）
- [x] 效果报告（样本不足时保守结论，不声称有效）

### 重启

无需重启（独立统计/划分/报告脚本）。依赖事件账本有真实评估记录后才有意义。

### 未覆盖边界

- 重大事件对接（`register_material_event`）与影子复核（`position_review.py`）已存在，本轮未改。
- `comparison.py` 的样本外重放需要真实事件 + 历史行情 + 锁定的实验参数，尚未跑通完整 A/B/C/D。

---

## 测试命令

```sh
# 决策链路针对性回归
python3 -m pytest tests/unit/live/test_decision_workflow.py -q

# 本轮新增回归
python3 -m pytest tests/unit/live/test_decision_health.py \
    tests/unit/live/test_suggestions_freshness.py \
    tests/unit/live/test_run_simulate_acceptance.py \
    tests/unit/live/test_execution_correction.py \
    tests/unit/live/test_evidence_quality.py -q

# 执行与恢复回归（临时库、mock 券商）
python3 -m pytest tests/unit/live/test_broker_execution.py \
    tests/unit/live/test_deployment_acceptance.py -q

# 改动影响公共状态或执行层时，运行完整单元测试
python3 -m pytest tests/unit -q
```

## 已知限制

- 本环境未安装 `flask` / `futu`，所有 `web/app.py` 改动仅做编译级验证；确认页/建议页的时效渲染需在有依赖环境打开页面冒烟。
- 真实 SIMULATE 闭环、真实券商成交更正、样本外重放均依赖 OpenD + 模拟账户 + 历史行情，未在本轮跑通，属后续验收项。
- 每项提交请走独立分支（如 `codex/decision-observability`）；保持 `trd_env=SIMULATE`，账户与密钥不提交。

---

# LLM 深度参与路线（F–J）进度

对应 `docs/llm-expanded-role-roadmap.md`。原则：LLM 只做可归因、可回放、可撤销的建议，程序独占账户选择、数量/风险计算、止损底线、成交对账与事务。LLM 不生成最终订单参数，也不放宽程序保护。

## 首个迭代：选股影子排序 ✅（已端到端验证）

**目标**：为每日可交易基础池生成版本化 Evidence Packet，LLM 输出结构化 Top-N 研究排名，落版本化研究批次，页面与交易提案分开展示。

### 改动文件

| 文件 | 改动 |
| --- | --- |
| `decision_ledger/evidence_packet.py` | 新增：冻结 packet（身份/行情/策略/事件/基本面/账户/数据质量），事实可回源 |
| `mutifactor/llm/selection_review.py` | 新增：selection schema + prompt + `validate_selection`（越权/引用校验） |
| `scripts/live_trading/llm_selection.py` | 新增：`rank`（基础池内排序，失败/越权只记 error 无订单副作用） |
| `decision_ledger/selection_outcomes.py` | 新增：固定窗口 1/3/5/10 会话收益/MFE/MAE、rank IC、Top-N 超额 |
| `decision_ledger/counterfactual.py` | 新增：反事实冻结与分组（后续 H3 用） |
| `llm_suggestions/store.py` | 改：版本化研究批次持久化（JSONL + 跨进程锁） |
| `scripts/live_trading/run_daily_selection.py` | 新增：每日冻结基础池 + 调度 rank（`--dry-run` 只拉行情不调 LLM） |
| `scripts/live_trading/run_selection_outcomes.py` | 新增：回填历史批次固定窗口结果 |
| `web/app.py` + `suggestions.html` | 改：`/api/suggestions` 返回 `research`，页面紫色「研究候选（非交易信号）」区块 |

### 验收清单

- [x] 相同输入/模型/prompt 可重放（`packet_id` + `research_batch_id`）
- [x] 基础池、未入选、空列表都保存（无幸存者偏差）
- [x] LLM 不能加基础池外代码（`validate_selection` 越权拒绝）
- [x] 事实引用有效，未来/陈旧/冲突/缺失显式标记
- [x] 模型失败/超时不改观察池/扫描/订单
- [x] 研究建议不产生 proposal/approval/order_intent
- [x] 固定窗口结果按当时可得行情 + 统一价格规则
- [x] 页面标「研究建议，不是交易信号」，与交易提案分开

### 端到端验证记录（2026-09-08）

- `run_daily_selection.py --dry-run`：8 只基础池全部拉到行情、生成 8 个 packet。
- `run_daily_selection.py`：LLM（deepseek-chat）返回 8 只候选，`error=null`，引用/越权校验通过。
- 修复过两个真实 bug：`BASE_DIR` 路径（`parents[1]`→`parents[2]`）；packet 缺可引用证据导致 LLM 误引 `packet_id`（现已生成「程序行情快照」证据，`evidence_` 前缀）。
- 页面 `/suggestions` 紫色区块正常展示 8 只候选。

### F2 数据源补充（✅，含一次修正）

- `signal_context.fetch_event_evidence`：富途资讯/公告转不可变 evidence（`evidence_id`/`content_hash`/`kind`），公告/评级=`filing`、资讯=`news`，可被 LLM 引用。
- `run_daily_selection`：每只股票拉富途事件并入 packet（不再只有行情快照）。端到端验证：LLM 的 `catalyst_evidence_ids` 已引用真实富途事件（Bernstein/Evercore 评级、财报等），部分候选带 counterevidence。
- **修正**：初版复用 `fetch_signal_context` 会串行拉 Yahoo 财报（404）/FINRA 空头（403），每只超时拖慢；改为只用富途源。

### 日常使用

```sh
python3 ./run_daily_selection.py            # 冻结基础池 + LLM 排名 + 落批次
python3 ./run_selection_outcomes.py         # 回填历史批次固定窗口结果
```

### 待评估（权限升级前）

- 攒够样本后看 `rank_ic` 是否稳定为正、`topn_excess` 是否为正（门槛：≥100 规则通过候选、30 可比较执行样本、覆盖多市场状态、锁定样本外区间）。
- 候选普遍缺「基本面财务数据」（`missing_information` 如实标出）——按 F2「先统计缺口再补」，下一批可考虑估值/财务字段。

## 第二个迭代：买入评审增强 🟡（核心已做，离散计划模板待评估）

**已落地**：

- `trade_review.py`：`REVIEW_SCHEMA` 加稳定原因码 `reasons`（event_risk / regime_conflict / weak_confirmation / stale_evidence / poor_asymmetry / data_gap），`defer`/`oppose_execute` 须给原因码或资料缺口。
- `counterfactual.py`：`freeze` 冻结每个候选的规则计划/LLM/人工动作/行情/统一执行假设后续结果，`compare` 按 `human_action × recommendation` 分组统计，判断 defer/oppose 是否真的避开亏损。

**待评估（不急于做）**：

- 离散计划模板（标准入场/等待确认）与仓位档位 `0 / 0.5x / 1.0x`——属于「允许模型影响什么」的权限扩展，文档要求先影子记录、证明校准后才开放 `0.5x` 降档权，不开放加档权。建议等首个迭代攒够反事实样本后再评估。

## 第三个迭代：持仓 thesis ledger ✅（影子版）

**目标**：把持仓期的 LLM 判断记录成版本化、可回放的 thesis 账本，供后续与程序退出逐笔对比；不参与任何交易决策。

### 改动文件

| 文件 | 改动 |
| --- | --- |
| `decision_ledger/thesis_ledger.py` | 新增：版本化状态机 `established→strengthened/unchanged/weakened→invalidated→closed`；`apply_transition`（无新证据不改状态）；`build_delta`（程序算新增/撤销证据引用）；`record_review` / `mark_closed` / `load_updates` / `current`；`chain_report` / `compare_actual_exit` |
| `scripts/live_trading/position_review.py` | 改：有效评审（`status=complete` 且含 `thesis_state`）后写入 thesis 账本；纯影子不改持仓 |
| `tests/unit/live/test_thesis_ledger.py` | 新增：13 用例 |

### 关键约束（文档 I2）

- 无新增证据且未触及保护线 → 模型建议的状态变化不采纳（保持原状态）。
- `invalidated` 为终态，不回改；`closed` 由实际平仓驱动。
- delta 由程序计算（本次引用证据集 vs 上一版），不依赖 LLM 自述。
- 状态未变化的评审不产生噪音版本（只写真正变化）。

### 待评估

- 需真实持仓跑起来后，用 `compare_actual_exit` 对比「LLM 判 invalidated 时点」vs「程序实际退出」，判断 LLM 是否提前识别风险。

## 第四个迭代：权限评估 ⬜（未开始）

依赖前三迭代攒够影子样本 + 真实 SIMULATE 闭环验证通过后，用锁定样本外数据分别评估选股/买入/卖出增量价值，只升级通过门槛的单项权限；不整体切换成「LLM 自动交易」。

## 阶段 J（未开始）

模型评测集（J1）、多模型按任务路由（J2）、权限治理门槛（J3）。

