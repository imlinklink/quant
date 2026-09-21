# L1 持仓角色纸面前向：启动记录与验收（2026-09-22）

## 一、启动事实

| 项 | 值 |
|---|---|
| 实验 ID | `L1-POSITION-20260922` |
| manifest_hash | `0680ea8b11a1c51ca18d149998a84a9ebbfea64c8a7f6a149a87ce38fee14670` |
| 冻结日期 | 2026-09-22（CST） |
| `start_session` | 2026-08-16（账户与增量生成器回放的共同起点；**约一个月提前量**是必需的，取太晚会永远无候选） |
| 账户 | `SHADOW:L1-POSITION-20260922:R` / `:L`，各 $100,000 |
| 模型 | `deepseek-chat`（`model_id` 进 `decision_id` 与包绑定，改动即另一个问题） |
| 技术包版本 | `technical-packet-v1`（22 项事实，必需 10 项） |
| 提示词 / 输出契约 | `position-action-v1`（绑定 `packet_id`）+ Position v2 |
| 开放动作 | `hold` / `exit`（其余模板不下发、且显式拒绝） |
| 评审频率 | `every_session`（逐 session 逐持仓；写进 manifest 而不是靠默认） |
| 决策截止 | 执行日开盘前 10 分钟（美东 09:20 / 北京 21:20） |
| **调用预算** | **$5.00**（5,000,000 微美元），每次调用**预留** $0.01（10,000 微美元） |
| 预算周期 | **实验生命周期累计，不自动重置**；改预算必须新建 `experiment_id`（值在 `manifest_hash` 里） |
| 调用超时 | 100 秒（且不超过距决策截止的剩余秒数）—— 大于传输层最坏情形 3×30s |
| 排程 | `com.quant.shadow-daily-l1`，北京**周二~周六 17:10 / 18:40 / 20:40** |
| 代码目录 | `/Users/wh1817w/quant-research`（隔离 worktree，账本 schema 10） |
| 账本 / 证据 / 日志 | `…/data/portfolio_shadow/L1-POSITION-20260922/` · 同目录 `evidence/` · `~/Library/Logs/quant/shadow_daily_l1.log` |
| 评估协议 | 主指标 `L_minus_R_return`；评估点 2026-12-22；`min_decisions: 30`；`decision_rule: report_only_no_auto_promotion` |

**预算与预留金额都没有既有配置值**（`config.yaml` 里只有交易成本），按你的要求单独列出：
$5.00 的额度 ≈ 2500 次典型调用（实测单次 ~2033→841 tokens），按 5 仓全占、每交易日 ≤5 次调用算
可覆盖约两年；$0.01 的单次预留 ≈ 典型成本的 5 倍，**故意取保守值**（宁可早停，不可穿透）。
两个数都在 `manifest_hash` 里，改动即新实验。

## 二、启动前补验的两个边界

### 边界一：线程超时 ≠ 请求终止

改为「工作线程 + 主线程按 `min(配置超时, 距截止剩余秒数)` 收口」后，四条都有测试：

| 要证的事 | 证据 |
|---|---|
| 超时后**日作业能退出** | `review()` 在 0.5s 内返回（超时 0.05s）；调用线程是 `daemon` ⇒ 挂死的调用不阻止进程退出 |
| **迟到结果不会被应用** | 迟到的 `exit` 回来时动作仍是 `ABSTAIN/TIMED_OUT`，账本里那条 Application 不是 `POSITION_EXIT` |
| **重跑不重复付费** | 追加一次 `review()`：模型调用次数仍为 1（终态尝试被复用，不重发） |
| **费用未知必须挂账** | `cost_uncertain=True` 进 Application；尝试记录 `status=TIMED_OUT` + `cost_uncertain`。**构造上不会记成零成本**（`_cost_of` 对缺 `cost_micro` 返回 unknown），并用测试钉死（注入 `cost_micro: 0` 即失败） |

### 边界二：预算不能只看已结算费用

占用 = **已结算** + (**金额未知** + **正在飞**) × 每次调用预留。前两者原来都是盲区：

- **金额未知**的尝试记账上是 0 ⇒ 不计入就会穿透；
- **正在飞**的调用（`shadow_job_runs.status='CALL_STARTED'`）还没落账 ⇒ N 个持仓同轮评审会**各自
  以为「剩下的钱够」**，一起穿透上限。

两条各配一条测试，并验证过「把预算改回只看已结算」时两条都失败。
**周期与重置**：实验生命周期累计、无重置路径；唯一的重置方式是新建 `experiment_id`（有测试）。

## 三、启动验收（一次真实作业记录）

```
python3 -m scripts.portfolio_shadow.cli run-daily \
  --manifest …/L1-POSITION-20260922/manifest.json --output …/portfolio_shadow \
  --session 2026-09-18 --model real --etf-raw data/medium_term/LIVE-ETF-20260917
```

结果（机读记录在 `/tmp` 之外的账本里）：

- `settle` 把空账户推进到 **2026-09-18（24 个 session）**，R/L 各 24 条状态、24 条净值；
- `gate: SKIPPED_EXPLICIT_SESSION`（显式 `--session` 由操作者负责 —— 门如实记下，不是"通过"）；
- 评审阶段未执行：`now = 2026-09-21T16:15:58Z` 已晚于 `phase_deadline = 2026-09-21T13:20:00Z`
  ⇒ **09-18 → 执行日 09-21 的决策窗口已过，不补**（设计如此）；
- 账本计数：**applications 0 / opportunities 0 / job_runs 0 / packets 0** ⇒
  **没有强造评审、没有模型调用、没有花任何钱**。

**结论：已启动，等待首个评审对象。** 按你的要求，**不把启动成功写成 LLM 已产生收益贡献** ——
`path_changed` / `applications_written` / L−R 净值差此刻仍然是**尚未测得**（没有样本）。

第一个真实评审对象出现的条件：T 收盘后（北京 17:10 那个槽起）账户里**已有持仓**且 T+1 的
决策截止未过。空仓期间作业每天照常跑完并如实报 `positions: 0`。

## 四、启动过程中发现并修复的一个**生产阻塞**缺陷（重要）

演练旧作业时命中 `refresh` 失败：

```
ValueError: PANEL_HISTORY_CHANGED:SEC-US-META:max|delta|=7.886e-04
```

追下去是**两处独立缺陷**，且**旧作业（M1 生产实验）会因此永久停摆**：

1. **守卫把「复权重述」当成「数据被改写」。** META 于 2026-09-21 新宣告一笔股息（ex_date
   2026-09-21，$0.525）⇒ 行动表新增记录 ⇒ 整段 `asof_close` 按同一因子平移 7.9e-4
   （0.525/700 ≈ 7.5e-4 吻合）。而 asof 面板本就是「锚在最新一天」的复权视图，
   **重述是构造性的，不是损坏** —— 且 P0-4 已证明这些列派生的信号全是**比较型**，
   对整段同因子缩放不变。
   修法：**原始列（OHLCV）与复权列分开判** —— 原始列变了（或行数不一致）⇒ 硬失败；
   复权列变了 ⇒ **放行但把幅度报出来**（`adjusted_max_delta`），不许静默。
2. **刷新会把「当天还没收盘的 bar」追加进面板。** 默认 `--through=今天`，而 LaunchAgent 的补跑
   **不保证在收盘后醒来** ⇒ 盘中跑一次就写进一根部分 bar，下一分钟同行的 `raw_close`/`volume`
   就变了 ⇒ 「只追加」的守卫在下一次刷新时必然失败。
   修法：默认 `through` 取**规则历里收盘已过的最后 session**（复用数据就绪门的同一处判据
   `data_readiness.expected_session`），并**丢掉**已存的不该存在的未收盘行（自愈）。
   实测：2026-09-22 00:09（美东 09-21 盘中）我手动跑刷新踩到过，AAPL 最后一行两分钟内就变了。

顺带修掉守卫里一个**潜伏的结构性漏判**：`(a.isna() ^ b.isna()).any()` 在行数不一致时会因
pandas 的 `skipna=True` 把 NaN 跳过 —— 旧实现只是恰好被 `session` 列的 object dtype 兜住。
现在行数不一致**显式判**，不靠巧合。

## 四之二、我自己造成并已修回的一处数据污染（必须记）

启动过程中我**在美东盘中**（12:09 ET）跑了刷新，把 2026-09-21 这根**还没走完的 bar**
写进了原始库（13 只 TECH 各 1 行 + 实况 ETF 8 行）。而原始库是「追加 + 去重」——
收盘后重取**不会覆盖**它，于是那个错误的收盘价（AAPL 09-21 volume 只有 13.9M，09-18 是 86.6M）
会被明天的决策与记录用上。

**修法**：把 session = 2026-09-21 的行从原始库与实况 ETF 里删掉（备份在 `/tmp/partial-bar-backup`），
让下一次**收盘后**的刷新自己补上正确的值 —— `refresh()` 本来就会 `force_tail_refetch` 当年尾巴，
所以删掉即会重取。

**修后核对**：13 只 TECH 的 raw 与面板都在 2026-09-18、实况 ETF 在 09-18，
逐只对照**0 处不一致**（其余 26 只是三臂前向的宇宙，停在 09-10，由它自己的作业刷）。

这条也说明：**盘中不该跑刷新**这件事以前只靠"排程放在收盘后"来保证，代码层面没有拦。
现在 `refresh_panels` 的默认 `through` 已经改成「收盘已过的最后 session」，加上去掉了
已存的未收盘行 —— 但**原始库那一层**（`download_market_history`）仍可能被盘中跑写进部分 bar，
这是**尚未封堵的同类缺口**，记在这里。

## 五、两条日作业的绑定（互不干扰）

| | 旧作业 | 新作业 |
|---|---|---|
| label | `com.quant.shadow-daily` | `com.quant.shadow-daily-l1` |
| 实验 | `M1-FORWARD-S-20260917`（**显式写进 plist 环境变量**） | `L1-POSITION-20260922`（显式） |
| 代码目录 | `/Users/wh1817w/quant`（schema 9） | `/Users/wh1817w/quant-research`（schema 10） |
| 证据/运行记录 | `…/portfolio_shadow/evidence/` | `…/L1-POSITION-20260922/evidence/`（**独立，避免 `runs/` 互相覆盖**） |
| 日志 | `shadow_daily.log` | `shadow_daily_l1.log` |
| 排程 | 北京 16:40 / 18:10 / 19:40 | 北京 17:10 / 18:40 / 20:40（错开 30 分钟，共用行情刷新锁） |

**为什么必须显式绑定**：脚本的自动发现是「0 个→跳过、多个→**报错拒绝猜**」——这是好设计，
但生产任务不该因此停摆。装上显式身份前，目录里出现第二个实验会让旧作业 FAIL。
**验证**：`install_launchd --check` 三项全绿；旧作业在**两份实验并存**的排练目录上跑通了
发现/绑定路径（随后被就绪门以 `AHEAD_OF_CALENDAR` 拦下 —— 那是美东盘中的正确行为，
**不是**实验目录数导致的）。

**看护**（`ops/watchdog.py`）新增预算检查：用**原始 SQL** 读账本（看护跑在 schema 9 的主
checkout，ORM 的版本守卫会拒绝 schema 10 的账本；这里读的是文本字段，不解释账户状态）。
告警文案**不含次数**，否则每弃权一次就变一次、去重失效 ⇒ 变成每次检查弹一次。
首次耗尽提醒一次、持续耗尽不重复（由 `alert_if_changed` 的问题集合去重保证）。

## 六、下一步

- 等第一个持仓；此后每个交易日按排程跑，真实调用可追踪、动作只进 L1 的纸面账户。
- 报告侧三分：**预算阻断**（`MODEL_BUDGET_EXHAUSTED`，零成本未调用）／**调用失败**
  （`TIMED_OUT`/`FAILED`/`INVALID_OUTPUT`）／**模型主动弃权**（模型自己返回 ABSTAIN）——
  三者在 `ABSTAIN_REASONS` 的分组里已经分开，且失败类进分母（否则缺口会被读成「模型没有价值」）。
- 评估点 2026-12-22、`min_decisions: 30`；**不自动晋级**。
