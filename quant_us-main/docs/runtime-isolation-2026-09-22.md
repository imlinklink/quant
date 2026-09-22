# 运行版本隔离（4.2）：把实验钉到不可变 commit，让开发主线解冻

日期：2026-09-22。用户裁定：「**近期保持隔离，但不接受必须冻结 main 19 个月。冻结对象应是
实验运行版本，不是开发主线**」。

## 一、做的事

| | 迁移前 | 迁移后 |
|---|---|---|
| M1 影子实验（`M1-FORWARD-S-20260917`，schema 9） | 开发 checkout `/Users/wh1817w/quant` | **`/Users/wh1817w/quant-runtime-main` @ `47a8c95`** |
| B3 三臂前向观察（schema 9） | 同上 | 同上 |
| 实盘服务（`run_all.py --dry-run`） | 同上 | 同上 |
| L1 持仓实验（schema 10） | **分支 worktree** `/Users/wh1817w/quant-research`（**随每次研究提交漂移**） | **`/Users/wh1817w/quant-runtime-research` @ `2ea981c`** |
| 看护 `watchdog` | 开发 checkout | **不变**（它要跟踪部署定义，部署定义在开发 checkout） |

两个运行 checkout 都是 `git worktree --detach` 出来的**不可变 commit**，`data` 软链到同一份
真实数据（`quant/quant_us-main/data`）。换运行版本 = 改 `RUNTIME_MAIN`/`RUNTIME_RESEARCH`
两个常量 + `--install` + 重跑核验（见 §三）。

**顺带修掉的一个真实缺陷**：此前**没有任何地方记录「每个实验实际由哪份代码驱动」**。
manifest 里的 `parent_code_hash` 是**策略溯源**（M1 填的是更早的 commit），不是运行版本 ⇒
L1 的代码在我今天的每次研究提交后都悄悄变了，而记录上完全看不出来。迁移前记录在
`data/strategy_research/runtime-isolation-before.json`。

## 二、核验（迁移前 → 迁移后）

| 核验项 | 结果 |
|---|---|
| 运行 checkout 的 12 个冻结文件哈希 | runtime-main **0 处不一致** ✓（前向观察不受影响） |
| `quant-runtime-research` 的冻结集 | **4 处不一致 —— 预期且正确**：研究分支本来就在改那四个文件（`paper_engine`/`schema`/`experiments`/`candidate_adapter`），**它绝不能跑前向观察** |
| M1 续跑一致 | 两边对**已结算** session 各跑一次：都 rc=0、**账本指纹零变化**、结果完全一致 ✓ |
| 三臂前向续跑一致 | 两边 `run-day --session auto` 行为完全一致（都因数据 09-18 < 起点 09-21 拒绝）、账本零变化 ✓ |
| 部署一致性 | `install_launchd --check` / `install_cron --check` / `check_config_record` **三项全绿** ✓ |
| 服务健康（trading-service 重启后） | `GET /api/decision-health` → **200** ✓ |
| 从运行 checkout 跑刷新 | 成功，且**面板真的前进**：13 只 TECH `09-18 → 09-21, appended 1`、历史最大差 **0.00e+00**、live ETF 到 09-21 ✓ |

## 三、这一轮撞出并修掉的两个真缺陷（都不是隔离本身）

1. **我自己引入的回归：面板刷新漏掉「追加新 session」**（提交 `47a8c95` / 分支 `2ea981c`）。
   改「只刷到收盘已过的 session」时把最后一步弄丢了 ⇒ `new_file` 只保留 ≤ 上周期末的行 ⇒
   **面板永久冻结、影子作业静默停摆**（每天照常跑、判「没有新 session」、无任何异常 —— 与
   「今天没事」长得一模一样）。追出来的路径是：修完检查点后刷新输出里 `panels.to=09-18,
   appended=0` 与原始库已有 09-21 的矛盾。
   补了一条**最基本职责**的测试 `test_new_sessions_are_appended` —— 我原来的三条只覆盖
   「拒绝改写历史」与「丢掉未收盘行」，**没有一条检验「新数据进得来」**。反证验过。
2. **检查点与分区不同步**（87 条：**15 条是我今天移除未收盘 bar 造成的**，72 条是既有的 ——
   文件改动日期 09-12/09-13，属三臂 32 只宇宙里的 AXTI/IWM/SOXX/SPY/SNDK 等，那些名字 M1 的
   刷新不碰所以一直没暴露）。表现为 `FileExistsError: 已有分区哈希与检查点不一致`，
   **今晚两个作业的刷新都会失败**。已按当前内容重算记录（备份
   `/tmp/download_state.before-repair.json`，逐条日志 `/tmp/partition-repair-log.json`），复核 0 条失效。

## 四、现在起边界变了（这是本项的全部意义）

- **开发主线可以继续演进**：合并研究分支、继续提交，都不再影响在跑的实验与实盘服务 ✓
- **但运行版本要显式换**：改 `RUNTIME_MAIN`/`RUNTIME_RESEARCH` + `--install` + 重跑 §二的核验。
  直接改开发 checkout 不影响在跑的东西 —— 这正是隔离要的效果。
- **"代码路径变化 ≠ 策略变化"**（用户口径）：换运行 checkout 不等于换策略；只要运行版本本身
  未被改动、且续跑逐字段一致，观察记录就仍然有效 ✓。

## 五、还没做

- 推送前的历史凭证审计（4.4，单独处理）。
- 研究分支合并进 main：现在**可以**做了（不再有冻结冲突），但合并本身会让开发 checkout 与
  运行 checkout 的差异扩大到主线上 —— 建议作为独立一步、合并后立刻跑完整测试。
