# LLM 交易决策演进 — 代码实现总结（供 review）

> 对应设计：[llm-trading-evolution-technical-design.md](./llm-trading-evolution-technical-design.md)（详细规格）
> 实现范围：PR4–8 的代码骨架 + 核心单元测试。PR1–3（硬退出、决策存储、回放 CLI）此前已提交，本文不重复。

---

## 1. 交付概览

按技术设计 §23 的 PR 拆分，本次补齐了 PR4–8 中"纯代码可独立交付"的部分：

| PR | 设计章节 | 状态 |
|---|---|---|
| PR4 选股 v4 | §7 | 契约已有（selection_v4.py），本次补 SYSTEM/prompt 与 packet 适配器 |
| PR5 入场 v2 + defer | §8 | 契约 + 校验器 + 模板计算（entry_v2.py） |
| PR6 持仓 v2 + 论文状态机 | §9 | 契约 + 状态机 + 模板（position_v2.py） |
| PR7 指标 | §16/§17/§21 | outcome 结算 + 指标投影（outcome_jobs.py / project_decision_metrics.py） |
| PR8 权限晋级/降级 | §13/§7.2 | 权限应用 + 降级检测（action.py / permission_guard.py） |
| 统一编排 | §12/§14 | DecisionEngine / review_scheduler |

**关键边界**：本轮交付的是「决策与账本层」的实现与校验逻辑，**尚未接入监控器/页面/券商链路**（见 §7 已知缺口）。所有新增能力默认 shadow，不改变现网行为。

> **更新（2026-09-09 code review 后）**：review 指出 8 个问题（3 P0 + 5 P1），已全部修复并补回归测试。详见 §9 修复记录。修复后，本批代码可视为「合同与账本层 v2」：核心逻辑完整、可独立测试，但**仍未接入真实决策链路、不提升任何 LLM 权限**。

---

## 2. 文件清单

### 新增（9 个）

| 文件 | 职责 | 对应设计 |
|---|---|---|
| `mutifactor/llm/contracts/entry_v2.py` | 四类入场模板 + execute_now/defer/reject Schema + 校验 + 强制动作 | §8 |
| `mutifactor/llm/contracts/position_v2.py` | 论文状态机 + 旧状态迁移 + 四类持仓动作模板 + 校验 | §9 |
| `mutifactor/llm/validators/action.py` | model action → effective action 裁剪 | §13 |
| `mutifactor/llm/validators/risk.py` | 模板/保护线/漂移边界校验 | §8.2/§9.5 |
| `scripts/live_trading/decision_engine.py` | 三类决策统一编排（`_decide` 模板方法） | §12 |
| `scripts/live_trading/decision_ledger/permission_guard.py` | 权限快照 vs 当前取更低 + 版本回 shadow + 降级检测 | §13/§7.2 |
| `scripts/live_trading/decision_ledger/outcome_jobs.py` | 1/3/5/10/20d 结算 + 反事实路径 + 独立样本 | §16 |
| `scripts/live_trading/review_scheduler.py` | 选股时段 / 入场去重 / 持仓去重+优先级 | §14 |
| `scripts/live_trading/project_decision_metrics.py` | 从投影表生成指标 | §17/§21 |

### 修改（2 个）

- `mutifactor/llm/contracts/selection_v4.py`：补 `SELECTION_V4_SYSTEM`、`build_selection_prompt`、`validate_selection_packet`（把 §7.1 的 `SelectionPacket` 适配到既有 `validate_selection_v4`）；修了 `raw_observered_at` → `raw_observed_at` 拼写。
- `mutifactor/llm/validators/evidence.py`：跨股票引用检查从 `('selection','position')` 扩展到含 `'entry'`（依据 §8.4「引用其他股票证据 → 强制 reject」）。

### 测试（5 个，43 用例）

`tests/unit/live/test_{entry_v2,position_v2,action_risk_validators,permission_guard,decision_engine}.py`

---

## 3. 贯穿性设计决策（review 请重点看这里）

1. **fail-closed 统一风格**：契约校验返回「错误列表」（空 = 通过），不抛异常；由 DecisionEngine 决定如何处置（有错 → 状态 `failed`，不产生动作）。这与旧 `validate_review`（抛异常）风格不同，但与 `selection_v4.validate_selection_v4` 一致。

2. **model action / effective action 分离**（§13.3）：`action.apply_permission` 输出 `{model_action, effective_action, permission_level, shadow_difference, requires_confirmation}`。前端/下游只认 `effective_action`。shadow → 角色基线（`rule_baseline` / `rule_ranking` / `hold`）；recommend → 模型动作但 `requires_confirmation=True`；constrained_action → 逐权限 `PERMISSION_ACTION_SCOPE` gate，越界动作降为基线。

3. **权限取更低 + 版本回 shadow**（§13.2）：`PermissionGuard.effective_level` 在「快照 vs 当前」间取更低，`prompt/output_schema/model_id/feature` 任一变化 → shadow。

4. **输入先于模型持久化**（§12）：`_decide` 先 `save_input_snapshot` → `record(decision_requested)` → 再 `call_model`。模型成功但后续保存失败 → 不执行动作（校验失败直接 fail-closed）。

5. **幂等**：`decision_id` 由 `build_context` 用稳定字段生成；引擎对已存在 `validated/failed` 的 run 直接重建返回，不二次调模型；attempt 用 `attempt_id = stable_id('attempt', decision_id, model_id, timestamp)` 区分。

6. **硬退出不属 LLM 权限**：hard_exit_router 直连执行器，本批代码不触碰它。

---

## 4. 各模块要点

### 4.1 entry_v2.py
- `build_entry_templates`：standard=程序数量、half_size=floor(50%)、wait_for_confirmation=0 且带复审触发器、reject=0。
- `validate_entry_v2`：动作↔模板映射（execute_now 只能 standard/half_size 等）、原因码 role 校验、claim 引用/跨股票/过期、counterevidence 或 missing、defer 必须选触发器。
- `forced_entry_action`：未来数据/撤销/跨股票/schema 错 → reject；质量门不含 entry → defer。

### 4.2 position_v2.py
- `transition_thesis`：终态（INVALIDATED/REALIZED/EXPIRED/CLOSED）不可恢复；`WEAKENING→CONFIRMED` 需 `counterevidence_added=True`；无新证据不改状态；UNKNOWN 保留上一态并标 `REVIEW_REQUIRED`。
- `legacy_thesis_state`：旧状态（established/strengthened/weakened/invalidated/closed）映射到新枚举。
- `build_position_action_templates`：hold/tighten/reduce(25%/50%)/exit(=剩余量)。
- `validate_position_v2`：tighten 不得低于当前保护线、exit 数量必须等于剩余、reduce 必须选模板、原因码 role 校验。

### 4.3 decision_engine.py
- `_decide(role, subject_type, packet, contract)` 模板方法；三个入口 `decide_selection/entry/position`。
- 记录事件：`decision_requested / model_attempt_started / model_attempt_completed|failed / decision_validated|validation_failed / permission_applied / decision_effective_action`。
- `DecisionResult` frozen dataclass，字段与 §12 对齐。

### 4.4 outcome_jobs.py
- `simulate_trade`：沿收盘价序列先触止损/目标即退出（跳空按当日收盘近似第一可成交价）。
- `selection_outcomes_for_code` / `entry_path_outcome` / `position_outcome`：纯计算。
- `OutcomeSettlement`：写 `decision_outcomes_v2` + `outcome_observed` 事件。

### 4.5 review_scheduler.py / project_decision_metrics.py
- 前者是**只读调度判定**（时段、去重键、优先级）；后者 SQL 出 `overview/action_distribution/{selection,entry,position}_metrics/health`。

---

## 5. 我做的判断与偏离（需要你确认）

以下是我在规格没有明确时自行决定的点，review 时请逐个确认：

1. **selection_v4 适配器**：`validate_selection_packet` 把 §7.1 的 `stocks[].evidence` 映射成既有 `validate_selection_v4` 期望的 `packets[].events`。既有 `validate_selection_v4` 是按 llm_selection.py 旧 shape 写的——是否该直接重构它消费 §7.1 结构，而不是加适配层？（建议后者更省事，但可能有重复字段语义。）

2. **evidence.py 扩展到 entry 的跨股票检查**：原文件只对 selection/position 查跨股票，我按 §8.4 加上了 entry。这会影响 entry 校验行为，请确认没有副作用（entry 的 market/sector 级证据 subject_code 应为 None，不受影响）。

3. **forced_entry_action 的运行时条件没接上**：价格漂移、组合快照版本变化、输出过期三个「强制 defer」条件，我留了可选参数（`price_drifted/portfolio_version_changed/stale`）但 **DecisionEngine 没有传入**（这些是运行时状态，不在冻结 packet 内）。当前引擎只应用 packet 内的强制条件（质量门/未来数据/撤销）。**这是需要补的缺口**。

4. **selection 的 model_action 语义**：引擎把选股的模型动作定为 `llm_ranking`，shadow→`rule_ranking`、recommend+→`llm_ranking`。这是 §7.4 的简化表达，未实现「recommend 只影响建议页观察优先级」的细粒度路由。

5. **论文旧状态 `unchanged` 映射**：`legacy_thesis_state('unchanged')` 返回 `UNKNOWN`（映射表值为 None 时统一回 UNKNOWN）。严格说 §9.3 要求 unchanged →「保留上一状态」，但纯映射函数没有上一态上下文，完整语义只能靠 `transition_thesis` 或调用方维护。请确认这里是否需要显式的「keep previous」约定。

6. **WEAKENING→CONFIRMED 的「反向证据」**：我实现为 `counterevidence_added`（新增反对证据被推翻）。规格只写「必须有新增反向证据」，措辞可再校准。

7. **`_record` 吞异常**：事件写失败只 log 不抛出（避免影响主流程）。但设计 §11.1 把事件当审计真相，吞异常可能掩盖审计缺口。是否要对关键事件（decision_requested / decision_validated）改为硬失败？

8. **save_attempt 用 INSERT OR REPLACE**：复用 `decision_run_store` 既有实现；attempt_id 带时间戳故冲突概率极低，但理论上会覆盖同 id。是否需要改为 INSERT + 冲突报错？

---

## 6. 已知缺口（未实现 / 未接入）

按设计 §25「完成定义」逐条对照，以下尚未闭环：

- [ ] **defer 状态机运行时**：§8.5 的 `DeferredReview` 持久化、触发器消费、到期/最大次数/新信号处理，只有 `review_scheduler` 的去重键，无真正的状态机。
- [ ] **DRY-RUN 集成 fixtures**（§20.6 六个场景）未录制。
- [ ] **metrics API + 页面**（§17 的 `/api/llm/*`）未实现，`project_decision_metrics` 只是数据层。
- [ ] **outcome 结算的数据源**：`outcome_jobs` 是纯函数 + 写库，未接行情价格序列/定时任务。
- [ ] **replay 的 rerun 模式**：仍是 stub（离线默认拒绝），未实现「历史输入 + 指定模型重采样」。
- [ ] **decision_engine 未接入监控器**：现有 `position_review.py` / `llm_selection.py` / `workflow.start_review` 仍走 v2/v3 旧链路，未切到 DecisionEngine。
- [ ] **`constrained_action` 的执行落地**：权限到 `recommend`/`constrained_action` 后，实际下单仍要靠 `execution.py`，本次未改执行器。
- [ ] 三个模块无直接测试：`review_scheduler`、`outcome_jobs`、`project_decision_metrics`。

---

## 7. 测试与验证

- 运行方式（沙箱无 pytest，用 unittest 显式模块路径）：
  ```bash
  cd quant_us-main
  python -m unittest tests.unit.live.test_entry_v2 \
    tests.unit.live.test_position_v2 \
    tests.unit.live.test_action_risk_validators \
    tests.unit.live.test_permission_guard \
    tests.unit.live.test_decision_engine
  ```
- 结果：新增 43 用例全绿。
- 全量 271 用例：6 个报错均为 `test_broker_execution` 等依赖 futu 券商 SDK 的既有用例（`ModuleNotFoundError: No module named 'futu'`），与本次改动无关。

---

## 8. 建议 review 顺序

1. `validators/action.py`（权限裁剪语义，最核心）
2. `decision_engine.py`（编排顺序 + fail-closed + 幂等）
3. `contracts/entry_v2.py` + `contracts/position_v2.py`（校验规则）
4. `permission_guard.py`（快照/版本/降级）
5. `outcome_jobs.py` + `project_decision_metrics.py`（结算口径）
6. 对照 §5 的偏离清单逐个确认

---

## 9. 修复记录（2026-09-09 code review 后）

review 提出 8 个问题，全部修复并补回归测试（`tests/unit/live/test_decision_regressions.py`）。

| # | 问题 | 修复 |
|---|---|---|
| 1 | P0 子权限被 `exit_review`/`entry_review` 绕过 | `action.py` 引入「伞级 + 具体子权限」适用集，取最严格；`permission_guard.most_restrictive` 取 min；引擎按 `applicable_permissions(role, action)` 分别 gate。position 的 tighten/reduce/exit 分别受 protection_tighten/thesis_reduce/auto_exit_thesis 约束；entry execute_now 受 entry_review+plan_template+position_scale 约束。并补上 `llm_permission.PERMISSIONS` 缺失的 protection_tighten/thesis_reduce |
| 2 | P0 未来证据通过 Entry 校验 | `evidence.py` validate_claims 拒绝 `effective/published/observed_at > as_of`；`forced_entry_action` 默认取 packet context 的 as_of；引擎显式传 as_of |
| 3 | P0 decision_id 未绑定输入 | `decision_run_store.finalize_decision_id` 纳入 `input_snapshot_id`；引擎先存快照再 finalize，且深拷贝 packet 不在快照内回填 decision_id（修复幂等 + 快照自洽） |
| 4 | P1 快照保存/读取键不对称 | `save_snapshot`/`save_permission_snapshot` 按 key 存储，`get_snapshot`/`load_permission_snapshot` 用同 key |
| 5 | P1 outcome 主键覆盖股票/模板 | `decision_outcomes_v2` 主键加 `subject_key`（code/template path/trade_id），投影 schema 升 v2 并迁移；`write_outcome` 事件 key 含 subject_key |
| 6 | P1 metrics SQL 报 a.role | `overview` 用表别名 + `r.role` |
| 7 | P1 replay validate 未真校验 | `ReplayEngine.validate` 重算输入快照哈希比对 `input_snapshot_id`，并调用角色契约校验器重校验每个 attempt 的 parsed_response |
| 8 | P1 关键审计事件写失败被吞 | `decision_engine._record` 对 `decision_requested/validated/validation_failed/permission_applied/effective_action` 硬失败 |

**测试结果**：新增回归用例全部通过；全量 284 个用例 0 失败，仅 6 个 `futu` 券商 SDK 缺失导致的既有 error（与本次无关）。

**仍需人工确认的语义判断**（review 未强制、但影响后续权限晋级）：
- entry 的 `execute_now` 采用「entry_review + plan_template + position_scale 三者取最严格」的保守模型——即三者都升到 constrained_action 才能自动执行。若你期望「entry_review 单独控制信号流、plan_template/position_scale 只控制模板/缩量子字段」，需要改成字段级 gate，请明确。
- `unchanged` 旧论文状态在纯映射函数里只能回退为 `UNKNOWN`，「保留上一状态」需调用方维护。

**仍未完成（与 review 一致的缺口，见 §7）**：defer 状态机运行时、DecisionEngine 接入现网、outcome 接真实行情与定时结算、metrics API/页面、constrained_action 接执行器、DRY-RUN 场景测试。
