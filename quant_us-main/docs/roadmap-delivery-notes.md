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
