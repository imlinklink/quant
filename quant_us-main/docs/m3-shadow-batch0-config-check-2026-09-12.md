# M3 批次 0 记录：前向 shadow 配置与实际股票池核对（2026-09-12）

依据 `next-stage-handoff-plan-2026-09-12.md` §6。**本记录只做核对，不运行 shadow**——账本是 SQLite 单写者且与本机实时系统共用，命令须在拥有账本的机器上执行。

## 1. 配置核对（通过）

| 键 | 值 | 要求 |
|---|---|---|
| `llm_decision.engine_v2.selection` | `shadow` | shadow ✓ |
| `llm_decision.engine_v2.account_scope` | `DRY-RUN` | DRY-RUN ✓ |
| `buy_strategy_v2.mode` | `shadow` | shadow ✓ |

未运行 `run_all.py --real`，未打开订单权限。**须另行确认本机只有一个实例写 `data/execution.sqlite3`**（沙箱无法验证）。

## 2. 两个实际股票池 —— **未对齐，须在第一批前处理**

| 用途 | 读取 | 实际内容 | 只数 |
|---|---|---|---|
| `run_daily_selection` | `dip_buy.watch_list ∪ trend_breakout.watch_list` | AXTI LITE MU MULL RAM RKLB SNDK SOXL YINN | **9** |
| `run_daily_setups` | `buy_strategy_v2.watch_list`（为空→退回 `dip_buy.watch_list`） | 同上 9 只 | **9** |

- 两池相同，交集 9 只。
- **与登记的 39 只样本只有 4 只重叠**：`AXTI / LITE / MU / SNDK`。
- 这 9 只中有 **5 只不在登记样本内**：`MULL / RAM / RKLB / SOXL / YINN`。
- 登记 39 只中 **35 只不在** shadow 池内。

**含义**：若现在开跑，前向 shadow 测的是**另一批（且含杠杆 ETF）**，与 39 只存续普通股样本无对应关系，无法与历史 A/B/C 构成同一研究群组的对照。

### 处理（§6 要求：第一批**之前**登记并一致，不得事后换池）

**决策（使用者 2026-09-12）：采用方案 A —— 三池都设为登记的 39 只。**

- 已生成 `data/shadow/config_shadow.yaml`（**gitignored**，`data/` 在 `.gitignore:30`）：`dip_buy.watch_list` = `trend_breakout.watch_list` = `buy_strategy_v2.watch_list` = 登记的 39 只；`futu.host=127.0.0.1`（本机运行）。
- 文件 sha256 前缀：`99fedbb23f8bead2`（登记为配置版本）。
- **未修改被跟踪的 `config.yaml`**（见下方安全提示）。
- 每日命令改为带 `--config data/shadow/config_shadow.yaml`（三个脚本均支持 `--config`）。

### 安全提示（既有问题，非本次引入）

`quant_us-main/config.yaml` **被 git 跟踪**，且 `llm.api_key` 是**真实密钥**（非占位/env），自 **initial commit `eba1eda`** 起即存在于仓库历史。建议：轮换该密钥；把密钥改为环境变量或外部引用；将本地配置纳入 `.gitignore`。本次不改动该文件以**避免再次提交密钥**。

原二选一（供记录）：方案 B 为保留现状、只对 4 只共同候选做增量比较。

## 3. 现有账本批次状态（只读对账）

对已有最新研究批次运行 `reconcile_selection_decision.py --json`（只读）：**`passed: false`**。

- `order_side_effects: 0` ✓（**无订单副作用**）
- `batch_linked` / `single_completed_attempt` / `snapshots_complete` ✓
- `role_validated` / `universe_covered` / `replay_valid` / `shadow_effective_action` ✗
- 其中一条 attempt 的校验错误：`US.MU counterevidence: 引用不存在: evidence_...`（**引用包外证据**，被校验层正确拦下）

按 §6：**对账 `passed` 必须为真**才算有效批次；失败时先修数据/时序/映射，**不放宽买入阈值**。

## 4. 下一步（在本机执行）

1. 确认本机只有一个实例写账本；确认无调度服务会自动重复触发同一作业。
2. 按上表选定并**登记**股票池方案（A/B），写入配置版本。
3. 每个美股交易日收盘后按顺序执行并保存标准输出/退出码/时间：
   ```bash
   python3 scripts/live_trading/run_daily_selection.py --dry-run
   python3 scripts/live_trading/run_daily_selection.py
   python3 scripts/live_trading/reconcile_selection_decision.py --json
   python3 scripts/live_trading/run_daily_setups.py --json
   ```
4. 最低联调量：**两个独立交易日、三个有效批次**；到期结果按 1/3/5/10/20/40 日补算。
