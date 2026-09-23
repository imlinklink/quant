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

## 四之二、运行版本表（2026-09-22 晚更新：日报管线变更后）

| 运行 checkout | pin 的 commit | 内容 | 约束 |
|---|---|---|---|
| `quant-runtime-main` | `ef9a2dc` = `88d1dbc` + 定时任务页 | M1 影子实验、三臂前向、**实盘服务**、可视化页面（7 页） | **store schema 9 —— 不得再往前移**（M1 的账本是 schema 9；合并后的 main 已是 schema 10） |
| `quant-runtime-research` | `c43b215`（合并后的 main HEAD） | L1 持仓实验 | schema 10 |

### 四之四、第三次移动 runtime-main 的 pin（2026-09-22 下午）：决策可视化六页

**改了什么**：新增 `ops/analytics_export/`（只读快照导出层）、`ops/build_web_snapshots.py`、
`ops/analytics_scopes.json`（来源登记）、`web/analytics.py` + 六个模板、
`report.py` 的 `vocab` 形参与 `position_rows`。交付记录见
`docs/decision-visibility-batch1-2026-09-22.md`。

**为什么安全**：全部改动**不在** `frozen_code` 的 12 个文件里；`report.py` 在 pin 与 dev
**逐字节相同**且不在冻结集 ⇒ 移动后冻结哈希 **12/12 一致**（已核）。

**这次用的是「选择性 checkout」而不是 `cherry-pick`**：
`git checkout <main 的三个提交> -- <明确列出的路径>`。理由是本轮要**刻意排除**
`ops/install_launchd.py` —— 它只服务开发 checkout 的 `--install`/`--check`（看护从开发
checkout 跑），而 pin 上的那一份还停在运行版本隔离**之前**的版本；把它带过去只会制造
无谓冲突。逐一列路径也让「带了什么」在 `git status` 里看得见。

**核验**：从 pin 跑导出器成功写出一代（26 文件 / 10 个范围全 OK）；新测试在 pin 上
24 passed；三项部署漂移核对全绿；服务 health 与九个路由全 200（服务已
`launchctl kickstart -k` 重启才加载新路由 —— **这步别忘，否则页面 404 而看起来像部署失败**）。

**新增排程**：`com.quant.web-snapshots`（北京周二~周六 21:30 / 22:30），排在所有写账本的
作业之后。

### 四之五、第四次移动 runtime-main 的 pin（同日）：可视化第二批

`31c59ff` → **`574eb5a`**（`main` 的 `caa478f`）。第二批是**纯新增**（6 文件 +316 行、零删除），
仍全部不在 `frozen_code` 里，冻结哈希 **12/12 一致**（已核）。

**为什么不并进 `31c59ff` 一次移动**：第二批引入了**运行版本内的新 section 形状**
（`normalize_study` 的四种产物映射）。分成两次提交，将来回看 pin 历史时能分清哪一批带来了
什么；并进去就只剩一个「可视化」大提交。

**同一教训再记一次**：模板改动后**必须 `launchctl kickstart -k` 重启服务**才生效
（Flask 在 `debug=False` 下缓存 Jinja 模板，而服务此前已经渲染过这些页面）——
不重启的表现是「页面 200 但内容还是旧的」，比 404 更难发现。

### 四之六、第五次移动（同日）：页面把 JS 当正文吐出来（用户实测发现）

**用户报的现象**：「页面显示，全是代码」。两个真 bug，**五个新页面全中**：

1. `_base.html` 里 `{% block script %}{% endblock %}` 写在了 `</script>` **外面**
   ⇒ 每个子模板的 JS 被当成 HTML 正文文本渲染出来。
2. 总览/持仓/机会/LLM 四页**从未调用 `boot()`** ⇒ 就算位置对了也永远停在「加载中…」。

**为什么两轮测试都没抓到**：我只断言了「200 + 字节数」。页面对 HTTP 是**成功**的 ——
状态 200、`Content-Type: text/html`、体积正常、Jinja 标记渲染干净、`node --check` 之后
语法也对。**检查手段（状态码/体积）与失败模式（内容错）不匹配** —— 这是本仓库反复出现的
「机制看起来在工作、实际没有」，只是这次它发生在页面上而不是调度里。

**补的三条回归（都验过「把 bug 放回去即红」）**：
- 剥掉 script 块后，正文里不得出现 `window.__render` / `out.push` / `=>`；
- 每页必须真的调用启动函数（`boot(window.__render)` / `bootExperiments()`）；
- `<script>` 与 `</script>` 必须配对。

**顺带修一处会误导人的显示**：切到研究范围时 `/positions` 原来说「本范围当前没有持仓」
—— 那句话是错的（研究结论里当然有成交，只是不是账户级的），现改为明说
「这是历史研究范围：只有研究产物，没有账户、持仓与评审对象」。

**教训（值得当规矩用）**：**页面类改动的验收不能只看状态码**。至少要有一条断言
「渲染出来的正文里没有出现本该被执行的代码」。

### 四之七、第六次移动（同日）：用户 review 的 4 条修复

`f558eff` → **`21cdfe8`**（`main` 的 `d616f8d`）。详见 `交付总结-2026-09-22.md` §9。

**最该记的一条**：四个页面读 `env.sections`，而接口把 `sections` 放在**顶层** ⇒
`undefined` ⇒ **有数据也显示成假空白**（M1 那 2 条持仓一直在接口里，我此前却把稀疏
页面解释成"空仓造成的"—— 用错误解释掩盖了一个 P1）。修法是接口**只交一种形状**。

**补上了一条真正能抓住它的测试**（用户点名要的验证）：
`tests/unit/live/test_analytics_render_node.py` 把页面渲染出的 `<script>` 抽出来，
在 node 里用最小 DOM/fetch 桩跑 `window.__render(真实接口载荷)`，断言产出的 HTML 里
**真有** `SEC-US-LITE` / `934.88` / `783.17` / 账户 id / `ABSTAIN` / `DECISION_DEADLINE_MISSED`。
**只断言「200 + 字节数」抓不到字段路径错** —— 这条测试补的正是那个盲区。
（node 不存在时自动 skip，不让 Python 套件硬依赖 node。）

顺带：新增状态 `NOT_IMPLEMENTED`，把**"没做"与"没到"分开** —— 页面上的占位展示原先标
`NOT_COLLECTED`，读起来像"数据到了就会自动出现"。

### 四之八、第七次移动（同日）：生命周期贡献分析 + **一个 pin 的陷阱**

`21cdfe8` → **`88d1dbc`**（`main` 的 `1e600cf`，用户自己写并提交的：新增
`ops/analytics_export/insights.py`，把此前标 `NOT_IMPLEMENTED` 的两块补上，并新增
「模型参与与执行」「L−R 收益率差」两块）。冻结哈希 **12/12 一致**，全量 **1670 passed**。

**⚠️ 这次暴露的陷阱（值得当规矩）：`runtime-main-pin` 分支可能落后于 checkout 的 HEAD。**

现象：runtime checkout 的 **HEAD 已经是 `88d1dbc`**（代码在盘上、是对的），但
**`runtime-main-pin` 分支仍停在 `21cdfe8`** —— 提交落在 **detached HEAD** 上，分支没跟着走。
后果有两层：

1. **分支落后** ⇒ 那个提交是**悬空的**（无引用、可能被 gc），远端记录也是错的；
2. **服务还在跑更早的代码** —— 实测服务是 16:11 启动的，而代码 17:13 才落地 ⇒
   8890 上**看不到**新页面（`grep` 实测两个新块都不在）。这正是本项目反复踩的
   「**装上去的东西是旧的**」，只是这次多了一层：**代码在盘上 ≠ 服务在跑它**。

**因此「部署到运行 checkout」是四件事，缺一不可**：
① 把改动的文件放进 runtime checkout（选择性 checkout / cherry-pick）
② **把 `runtime-main-pin` 分支移到新的 HEAD**（否则提交悬空、远端记录错）
③ **重建快照**（导出层代码变了，快照内容才会变）
④ **重启服务**（Flask 缓存模板与已加载的模块——服务启动时刻早于代码落地就一定没生效）

**当前实现的一次 `--check` 抓不到这件事** —— crontab/plist/配置记录三项都「与预期一致」，
因为**它们核对的是部署定义，不是运行版本**。要自动抓住它，得有一条「服务进程的启动时刻
晚于 runtime checkout 的 HEAD 提交时刻」的核对。**这是留给下一轮的缺口，未实现。**

### 四之九、第八次移动（同日）：定时任务页 —— 顺手把上面那个缺口补上了

`88d1dbc` → **`ef9a2dc`**（`main` 的 `d1883cd`）。用户问「有哪些定时任务，能否写在一个页面上」，
新增 `/schedule`。

**顺带把 §四之八 留下的那个缺口做成了可查的**：页面上核对
**`runtime checkout HEAD 的提交时刻` vs `常驻服务进程的启动时刻`**，判出
`OK`（服务晚于提交 ⇒ 已加载当前代码）/ `STALE`（服务早于提交 ⇒ 很可能仍跑旧代码）。

**它当场就演了一遍**：第一次生成快照时服务是 17:20:42 启动的、而 pin 已在 17:28 提交 ⇒
页面报 **STALE**；重启服务后重新生成快照 ⇒ 变 **OK**。**这个 STALE→OK 的翻转本身就是它有效的证据。**

**只采"已安装"的事实**（`~/Library/LaunchAgents/*.plist` + `crontab -l` + `launchctl list`），
不采定义文件 —— 本仓库最贵的教训就在这条线上。采集在**导出器**里做（批处理，允许跑外部命令），
web 仍只读 JSON。

**修两个自己踩的 bug**：① `ps -o lstart=` 在中文 locale 下输出「二 9/22 …」，定长格式串
匹配不了 ⇒ 必须 `LC_ALL=C`；② 运行版本核对用**字符串前缀**判断目录归属 ⇒
`/Users/x/quant-runtime-main` 被判成在 `/Users/x/quant` 里、误报"服务仍跑旧代码"。
**本仓库第二次栽在子串匹配上**（前一次是快照删除守卫）。

**还修了渲染验证的一个真缺口**：旧渲染桩只调同步 `window.__render`，于是
**boot 类页面（`/experiments`、`/schedule`、`/decisions`）静默渲染出 0 字符，
而测试"全部通过"** —— `/experiments` 的对比表其实从来没被验证过。现在渲染桩等异步
boot 落地，且 `_render` **起步先断言非空（>200 字符）**。
这与"JS 当正文"「读错字段」是同一类：**检查手段与失败模式不匹配**。

### 四之三、第二次移动 runtime-main 的 pin（2026-09-22 午间）：段首日公司行动

**症状**：三臂前向自 `start`（09-21）起从未推进过。第一次真跑日作业即 `FAIL refresh 失败，
本次不推进` —— `refresh_data --universe forward-arms` 在 `SEC-US-JPM` 上抛
`ACTION_FACTOR_UNRESOLVED:SEC-US-JPM:2015-01-02`。

**根因**：JPM 有一条 `cash_dividend`，`ex_date` 正好是它原始日线的**第一根 bar**（2015-01-02）。
`_factors_by_ex_date` 用 `first <= ex <= last` 收集行动，于是这条落进窗口；而 `_prev_close`
要求一根**严格早于**除息日的 bar，段首日没有 ⇒ 抛错。但这个因子只会被乘到空切片
（`build_asof_panel` 里 `factor[:i] *= f` 且 `i=0`），`scale_to_next` 也只读 `by_ex[nxt]`
而 `nxt` 不可能是段首日 ⇒ **对该段每一行都没有影响**。对股息而言该错误**只可能**在
`ex == 段首日` 触发（窗口判据已保证 `ex >= first`）⇒ 这个 fail-closed 守卫唯一的效果
就是让段首日除息的面板**永远无法重建**。

**安全性证据**：剔除该条后重建 JPM 面板，历史段与库中已有的那份逐格差 **≤5.7e-14**
（`HISTORY_TOL=1e-9`）⇒ **库里的面板本就是"没有应用该因子"算出来的**，改判据不改变任何
已有输入。全 39 只里**只有 JPM** 有落在段首日的可调整行动 ⇒ 对 13 只 TECH 是**构造性无操作**。

**改动**：`_factors_by_ex_date` 的窗口判据改 `first < ex <= last`（抽成 `_affects_segment`
一份定义，merger 检查同用），与 `generate_historical_setups._raw_asof_snapshots` 里早有的
同一条规则对齐。修在**共享原语**里而不是调用方 —— 两处调用方各需要一次，正是"同一件事
两份定义"的温床。fail-closed 未放宽：段内不可换算的股息仍阻断（有测试钉死）。

**核验**：`frozen_code` 12 个文件哈希 **0 处不一致** ✓；三臂 `run-day --session auto` 推进到
**session=2026-09-21**（重放 1 个 session、三臂 eq=100,000、pos=0）✓；32 只观察宇宙面板全部
到 09-21 ✓；`install_launchd --check` / `install_cron --check` / `check_config_record` 三项全绿 ✓；
服务 health **200** ✓；全量测试 **1622 passed**（修前 1620 passed + 2 failed，那 2 条正是
数据完整性守卫在真实数据上报出的"面板落后于原始库"，随刷新完成自动转绿 —— 守卫本身工作正常）。

**注意两个不对称**：① 面板目录里另有 **7 只**（`US_HON/LIN/ORCL/QCOM/SNDK/TSM/UNH`）停在
09-10，它们**不在**三臂的 32 只宇宙里（属 39 只幸存者审计集，只被已停用的刷新路径覆盖），
不影响观察；② `quant-runtime-research` **没有**跟这次 pin —— L1 的刷新只覆盖 13 只 TECH
与 ETF，构造上碰不到这条路径。

**顺带**：`main` 同步提交为 `e2a9170`（此项两处改动）。`runtime-main-pin` 分支已随之前移；
**推送**：`main` `9210da3..eba7f4d`、`runtime-main-pin` `7271033..cf2440d` 已于 2026-09-22
推送到 `origin`（`git@github.com:imlinklink/quant.git`），推送后三个分支与 origin 逐一核对一致。
推送前按用户口径先做了历史凭证审计，见 §六。

**合并（`3ff2b7f`）之后，两个运行 checkout 只差 schema 9/10 这一件事** —— 这正是它们必须分开的原因。

**日报管线变更（`c43b215`）怎么进的两个 runtime**：`quant-runtime-research` 直接移到合并后的 main；
`quant-runtime-main` **不能**跟着移（schema 会跳到 10、M1 账本立刻不可读）⇒ 用
`runtime-main-pin` 分支在 `47a8c95` 上 **cherry-pick 日报那一个提交**（`7271033`）。
安全性有据：日报两个文件（`market_digest.py`/`publish_digest.py`）**都不在**前向观察的
`frozen_code` 里 ⇒ 移动后冻结集仍 **0 处不一致**（已核）；且 `market_digest` 只被影子作业用、
**不被实盘服务用** ⇒ 无需重启服务。

**兜底语义实测**（跑固定 checkout 自己的代码）：只有热榜文件时选中热榜 ✓；加入真日报后日报胜出 ✓。

## 五、还没做

- ~~推送前的历史凭证审计（4.4，单独处理）~~ **已完成，见 §六**。
- 研究分支合并进 main：现在**可以**做了（不再有冻结冲突），但合并本身会让开发 checkout 与
  运行 checkout 的差异扩大到主线上 —— 建议作为独立一步、合并后立刻跑完整测试。

## 六、推送前的历史凭证审计（4.4，2026-09-22 完成）

用户裁定的口径是「先做**不输出明文凭证**的历史审计、确认远端与推送范围；推送单独处理」。
审计**只报告命中位置与掩码后的形态，不打印凭证原文**；本文件也不记录任何凭证内容。

**范围**：全部可达历史的**每一个 blob**（`git cat-file --batch-all-objects`），共
**1995 个 blob / 51.9 MB**。不是抽样、不是只查 HEAD。

**结果：全历史未发现凭证。** 逐项：

| 检查 | 结果 |
|---|---|
| `sk-` 形态密钥（DeepSeek/OpenAI 风格） | 0 处 |
| GitHub token（`ghp_`/`github_pat_`/…）、AWS `AKIA…`、Google `AIza…`、Slack/Stripe | 0 处 |
| `-----BEGIN … PRIVATE KEY-----` | 0 处 |
| 带引号的口令类赋值（`api_key`/`password`/`secret`/`access_token` = `"…"`） | 0 处 |
| 富途交易口令字段（`trade_pwd`/`login_pwd`/`unlock_pwd`/…） | 0 处 |
| 敏感文件名（`.env`/`id_rsa`/`*.pem`/`*.key`/`*credential*`/`*secret*`/`*.sqlite3`） | 0 个 |

**那份明文 key 的去向已确证**：`HEAD:quant_us-main/config.yaml` 是 **93 行**的初始版本，
`api_key`/`sk-`/`deepseek` **一个都不含**；而本地那份 419 行、带明文 key 的 `config.yaml`
靠 **skip-worktree** 对 git 隐藏 ⇒ **从未进入任何提交**。（key 不轮换是用户 2026-09-19 的决定，
本审计只回答"它在不在历史里"，答案是**不在**。无凭证的记录版是 `docs/llm-decision-settings.yaml`。）

**推送范围**（已确认后执行）：`main` `9210da3..eba7f4d`（3 个提交：可视化需求文档、段首日行动修复、
本文档更新）、`runtime-main-pin` `7271033..cf2440d`（1 个提交：段首日行动修复的 cherry-pick）。
共 4 个提交，只触及 1 份 PRD 文档、`asof_feature_panel.py` 与其测试、本文档。

**本审计的边界**（不要读成"绝对安全"）：判据是**已知形态的模式匹配**，未做全量熵值分析 ⇒
若存在**格式上不像凭证**的凭证（例如被改名成普通标识符、或拆散拼接），本审计不会发现。
按现有证据（含"明文 key 从未入库"已确证）判断，其余风险不构成阻塞推送的理由。

### 2026-09-23：建议页共享目录漏接修复

实盘空仓与建议生成无关。运行目录的建议存储按 checkout 父目录定位，
`quant-runtime-main/.quant_suggestions` 缺失，导致 API 返回宏观建议时间为空、研究批次为空，
尽管开发目录已有数据。补建软链到 `/Users/wh1817w/quant/.quant_suggestions`，
与 data 共享方式一致；未修改交易代码、账户或冻结实验。后续重建运行目录时必须保留此连接。

修复后实际访问 8890 `/api/suggestions`：宏观批次生成于 2026-09-23 00:52:55 UTC
（北京时间 08:52:55），fresh，5 条候选；研究批次为 2026-09-21 22:01:38 UTC，
错误 llm_failed，0 候选。两者是独立来源，不能将研究调用失败解读为模型建议空仓。
GET 每次重新读取文件，本次目录连接修复无需重启服务或重新调用模型。
