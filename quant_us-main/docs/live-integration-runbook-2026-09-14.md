# 下一阶段联调手册（严格版）

> 起始：2026-09-14（周一）。以当日冻结的代码/配置/服务为基线，按日期严格执行。
> 所有「观察 → 核对 → 记录」步骤只读，不主动调用模型、不人为制造信号。

## 0. 基线（冻结，改前必须先记录）

- 服务：PID `26056`，命令 `python3 run_all.py --dry-run`，启动于 2026-09-14 17:10 CST。
- 代码提交（`main`）：`7ce496c`(test: 提案完成态恢复) → `c264e2d`(feat: shadow 联调) → `b548b09`(feat: P1/P2)。working tree 干净。
- 配置（已核实，非秘密字段）：
  - `shadow_integration.enabled: true`
  - `llm_decision.engine_v2: {selection: shadow, account_scope: DRY-RUN}`
  - `buy_strategy_v2: {enabled: true, mode: shadow, schedule_time: '16:30'}`
  - `llm_decision.outcomes: {enabled: true, time: '17:30'}`
- 观察池（9 只，取自 `dip_buy.watch_list`，`buy_strategy_v2.watch_list` 为空则回落）：
  `US.SNDK / US.MU / US.SOXL / US.YINN / US.LITE / US.AXTI / US.RAM / US.MULL / US.RKLB`
- 日志：`logs/quant_us_<YYYY-MM-DD>.log`（按天滚动）。
- 决策账本：`data/execution.sqlite3`（`decision_events` 表，ShadowJobs 读写的权威源）+ 导出 `data/decision_ledger/events-v1.jsonl`。
- 当前状态：无持仓；`shadow_job_*` 事件 0 条（未到首个收盘批次，符合预期）。

## 1. 关键时间表（美东 → 上海）

| 事件 | 美东 ET | 上海 CST（夏令时 9/14–11/1） | 冬令时（11/1 后） |
| --- | --- | --- | --- |
| 美股收盘 | 16:00 | 次日 04:00 | 次日 05:00 |
| `selection_and_reconcile` | 16:20 | 次日 04:20 | 次日 05:20 |
| `daily_setup_shadow`（需 selection 成功） | 16:30 | 次日 04:30 | 次日 05:30 |
| `selection_outcomes` | 17:30 | 次日 05:30 | 次日 06:30 |

- 调度实现：`scripts/live_trading/outcome_scheduler.py::_integration_tick`，每 30s 一次。
  先过交易日历闸门（`trading_calendar.sessions`，非交易日直接返回），再按上表触发。
- shadow_job 的 session 键用 **纽约本地日期**（9/14 收盘 → session=`2026-09-14`）。
- 夏令时/冬令时切换点：2026-11-01（美东回拨一小时），此后 CST 顺延一小时。

## 2. 首个收盘批次核对（2026-09-14 收盘 → 9/15 凌晨）

### 2.1 盘前准备（9/14 白天，随时）
- [ ] 服务仍在：`ps aux | grep run_all.py | grep -v grep`（应见 PID 26056）。
- [ ] 健康：`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8890/api/decision-health` → 200。
- [ ] 富途 OpenD 在线（Selection 与 setup 都依赖 Futu 数据）。
- [ ] 电脑不休眠、不断网。

### 2.2 04:20 CST（= 9/14 16:20 ET）之后 —— Selection + 对账
- [ ] 观察 `logs/quant_us_2026-09-14.log` 是否出现 selection / reconcile 相关行。
- [ ] 核对 shadow_job 事件：
  ```bash
  sqlite3 data/execution.sqlite3 "SELECT event_type,json_extract(body,'$.payload.job'),json_extract(body,'$.payload.status'),json_extract(body,'$.payload.exit_code'),json_extract(body,'$.payload.session'),json_extract(body,'$.payload.attempt') FROM decision_events WHERE event_type LIKE 'shadow_job%' ORDER BY rowid"
  ```
  或 `grep '"event_type":"shadow_job' data/decision_ledger/events-v1.jsonl`
  - 期望：`shadow_job_started`(selection_and_reconcile, running) → `shadow_job_finished`(selection_and_reconcile, succeeded, exit_code=0)。
- [ ] 若 finished 为 failed / exit_code≠0：**不重跑**。先看子进程输出（`run_daily_selection.py` / `reconcile_selection_decision.py` 的 stderr 与退出码）定位，按证据修复后手动补跑一次**对账脚本**（非模型）。
- [ ] 记录本轮 Selection 的 decision_id 与通过只数。

### 2.3 04:30 CST（= 16:30 ET）之后 —— daily setup
- [ ] 前提：2.2 的 `selection_and_reconcile` 必须 succeeded（代码闸门 `jobs.succeeded(...)`）。
- [ ] 核对 `shadow_job_finished`(daily_setup_shadow)。期望 succeeded、exit_code=0。
- [ ] 核对 setup 输出（`run_daily_setups.py --json`）：质量 pass/失败；`US.RAM` 之类日K不足 250 根会明确拒绝。**命令退出码 1 = 存在质量失败，不是批次崩溃**。
- [ ] 若 setup failed：最多重试 3 次、间隔 ≥5 分钟（`max_attempts=3`）；仍失败则保持失败，**不降低 250 根门槛**。

### 2.4 05:30 CST（= 17:30 ET）之后 —— outcome
- [ ] 核对 `shadow_job_finished`(selection_outcomes)。期望 succeeded、exit_code=0。
- [ ] outcome 独立结算，其失败不影响 selection / setup 的判定。

### 2.5 次日白天复盘（9/15）
- [ ] 汇总三个 job 的状态与退出码，写一页复盘（放 `data/runtime_audit/<RUN-ID>/`）。
- [ ] 核对账本：候选数、对账通过、订单副作用 = 0（DRY-RUN 不应产生真实订单）。

## 3. 失败处理红线（严格执行）

1. Selection 失败 **不自动重调模型**；只在证据明确后手动补一次对账。
2. setup / outcome 失败最多重试 3 次、间隔 ≥5 分钟；达到上限后保持失败。
3. 进程中断后遗留 running 的 shadow_job **不自动抢跑**，需人工核查后再定。
4. 任何修复先落证据（log 片段、事件 payload、子进程退出码），再动手；不重复调用结果不明的模型任务。
5. 常规周末/节假日不启动日任务；**临时休市日仍未接入官方日历**（见 §5）。

## 4. T+1 与有持仓退出（后续交易日，非今日）

- T+1 跨日处理、以及出现持仓后的退出链路，要到后续交易日验证；没有候选时**不人为制造信号**。
- 有持仓后，chandelier 出场 / 硬退出在盘中跑（当前 log 已见 60s 循环），届时额外核对 exit 事件与对账。

## 5. 已知缺口 / 红线（本手册范围内不擅自改）

- **临时休市日未接入官方交易日历**（`trading_calendar.sessions` 只含常规节假日）——收盘批次会误触发，节假日前需人工关闭或补日历。
- **未安装开机自启/系统守护**——服务被杀后需按 §6 手动重启。
- **旧式提案完成记录重启后不在确认台恢复**（展示层缺口，已在 `审查与修复清单.md`「四-6」单独记录）。

## 6. 停止 / 重启（需要时）

- 停止：先 `ps aux | grep run_all.py | grep -v grep` 核对 PID（当前 26056）确实对应本服务，再 `kill -TERM 26056`；优雅退出约 20s。
- 重启：`cd /Users/wh1817w/Documents/quant/quant_us-main && python3 run_all.py --dry-run`（沿用原启动方式），启动后先过健康检查，再等下一批次。
- 重启后不抢跑遗留 running 的 shadow_job（§3.3）。

## 7. 产物与记录

- 每轮核对结果写 `data/runtime_audit/<RUN-ID>/`，保留 `final-check.json` / `reconcile.json` / `pytest.log` / `service.log` 等证据。
- 复盘文档放 `docs/`，命名带日期，如 `live-integration-close-batch-2026-09-14.md`。
