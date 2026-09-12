# M2 原始价退出链路与 A/B/C 重跑交接手册（2026-09-12）

## 1. 任务与当前状态

目标是把已接通的 **逐决策日 as-of setup + T+1 不复权入场**，延伸到持仓期退出、质量门和新编号的 A/B/C 历史再现。只研究用户选定的**当前存续普通股**，不补退市证券；结论须标明幸存者偏差，已看过的历史区间不能重新称为盲测。D 组没有严格历史 LLM 标签，不用伪标签，维持 `inconclusive`。本手册不涉及真实订单。

截至撰写时，`generate_historical_setups.py --price-basis raw_asof` 已从不复权日线和按 `security_id` 关联的公司行动构造完整周/日特征，T+1 使用不复权开盘价，跨行动日换算价格门；旧 `legacy_qfq` 默认路径仍在。随后已实现 `exit_matrix.py` 的第一版公司行动会计：拆股/反向拆股换算股数与保护线，现金分红进入总回报，ATR 使用逐日 as-of 口径，raw 路径要求显式 `--actions` 和 `--quality`。`build_research_quality_intervals.py` 已把现有审计生成质量区间，实际结果为 19 个 verified、21 个 rejected。新增改动仍在工作区，**尚未提交**；当前首要剩余项是实际 39 只的 raw setup/退出逐股审计，不能直接跳到正式收益。接手时先复核 `git status --short`，不要假定已有新 commit。测试收集时富途库需能写本机日志。细节见 [M2 接线记录](m2-wiring-record-2026-09-12.md)。

M1 审计已发现：40 个映射 0 歧义；39 只股票加 SPY 日历；18/40 的上市日未知或是占位值，不能标 `verified`；`US.HON` 有未解析公司行动。原 `SURVIVOR-003` 使用 QFQ 执行价与全快照特征，不能覆盖或改名当作新实验。M2 当前状态是 **`blocked`**，可继续开发，但质量门通过前不能发布新收益结论。

## 2. 接手后先核实这些文件

| 位置 | 当前作用 | 接手动作 |
|---|---|---|
| `scripts/data/generate_historical_setups.py` | 生成 `raw_asof` setup；输出 `security_id`、`price_basis`、`scale_to_next` 和执行日尺度的价格门 | 检查实际 39 只逐股输出、重复键、耗时与拒绝分母 |
| `scripts/data/asof_feature_panel.py`、`price_views.py` | 逐日 as-of 均线/ATR、行动因子、跨日尺度 | 退出日 ATR 要复用同一口径，不可对裸原始价直接做跨拆股 TR |
| `scripts/buy_strategy_experiment_runner.py` | 从 setup 生成 A/B/C/D 入场并做高开/初始止损门 | 保留 `price_basis/security_id`；禁止 raw setup 与 QFQ 日线混用 |
| `scripts/exit_matrix.py` | E1–E11、4 成本及组合三仓模拟 | 实现行动会计后才移除上述阻断；保持 E1–E11 的原定义 |
| `scripts/experiment_manifest.py`、`scripts/buy_strategy_report.py` | 不可覆盖实验、`--groups ABC` 与报告 | 新编号、质量文件、预登记、成本及数据哈希齐全后再跑 |
| `data/survivor_sample_audit/` | 本机审计及 as-of 面板，受 gitignore 保护 | 确认来源/哈希/行数；不把本机原始行情复制入 Git |

先在工程目录执行并保存输出：

```bash
git status --short
git diff --check
git rev-parse HEAD
python3 -m pytest -q tests/unit/live/test_generate_historical_setups_raw_asof.py tests/unit/live/test_exit_matrix.py tests/unit/live/test_asof_feature_panel.py
```

`python3 -m pytest -q` 是提交前的全量门。若只因富途导入尝试写 `~/.com.futunn.FutuOpenD/Log` 被沙箱拒绝，应在允许该本机日志写入的受控环境重跑；不要把收集失败误报为策略测试失败。实际输入路径先用 `rg --files data/survivor_sample_audit` 和对应 run 的 manifest 核实；手册中的路径/编号是**目标产物名**，不是声称已经存在的文件。

## 3. 第一阶段：先完成退出会计，再打开矩阵

### 3.1 增加显式数据契约和 fail-closed 检查

建议给 `run_exit_matrix` / `simulate_daily` 增加关键字参数 `actions`、`price_basis`（或封装一个 `RawExecutionContext`），命令行增加 `--actions` 与 `--price-basis raw_asof`；不要依赖仅看某一列的隐式猜测。每条入场须能解析 `security_id`、`stock`、`entry_time`、`entry_price`、`initial_stop`；日线须有 `security_id`、交易日及不复权 OHLCV；行动须有 `security_id/action_type/ex_date/ratio/cash_amount` 和来源记录。检查唯一 `(security_id, session)`、OHLC 合法、交易日单调、入场日存在、入场后所需价格覆盖，以及相同 `stock` 不跨 `security_id` 混接。若输入行动表的覆盖能力无法证明“无行动”，不得把空表自动视为完整历史。

入场与执行使用**同一批**不复权数据，输入数据版本写入 manifest。`add_atr` 当前直接用原始价计算 TR，跨拆股会产生虚假的巨额 ATR；raw 路径必须改成每日收盘时点的 as-of ATR，且仅用于次日生效的保护线。若行动、ATR 或原始 bar 缺失，输出逐笔 `data_quality`/拒绝原因并阻断绩效；不要以 QFQ 值回填。`raw_asof` 和 `legacy_qfq` 的输出禁止拼成同一矩阵。

### 3.2 明确每日事件顺序

对每笔多头持仓，以初始**股数 1**及每股入场价作为会计基准，或用等价的 `position_usd / entry_price` 股数。每天按以下顺序处理，并写单元测试锁定：

1. **开盘前**应用当天生效且已核验的公司行动。拆股比率 `r = 新股数/旧股数`：`shares *= r`，所有每股保护线、历史高点和成本参考价除以 `r`；反向拆股同理。现金分红 `d`：只有在除息日前一交易日收盘仍持有者，才记 `cash += 当时 shares × d`，并把每股保护线/历史高点减 `d`，避免机械除息缺口触发止损。多个行动同日、股票股息、分拆、并购或行动参数不完整，未核定次序与价格衔接前一律 `corporate_action_unresolved`，不猜测。
2. **当日开盘**先用已换算的旧保护线检查跳空；低于保护线时按原始开盘价成交。入场日不存在“先以开盘成交、再被同一个开盘跳空止损”的时间顺序：只在入场成交后检查该日盘中区间。若 `entry_price` 与日线开盘不一致，要校验并记录允许的滑点，不得使用日线开盘替代已登记成交价。
3. **盘中**用当日低价检查旧保护线。若触线，现有模型按止损线虚拟成交，应标明这是日线 OHLC 模拟，不能保证真实盘口成交；跳空按开盘处理。固定持有的 E1–E4 是否有硬止损，保持已冻结实验定义，不在本次偷偷改变。
4. **收盘**若仍持有，再更新高点、结构止损及 as-of ATR 派生的下一日保护线；保护线在当日不得凭当日高点追溯触发。E1–E11 的持有日计数、40 日观察窗及右删失语义与旧版一致。若同日同时满足不同事件，按预先固定的顺序处理，输出 `exit_reason`。

纯拆股示例：100 买入 1 股，旧止损 95，翌日 2:1 拆股且原始开盘 50、低价 49.5。开盘前变 2 股、止损 47.5；不能因原始开盘 50 小于旧止损 95 而报 `GAP_STOP`。若价格 50 退出，交易现金仍是 100。纯现金分红示例：100 买入 1 股，旧止损 95，翌日除息 2、开盘 98；有资格持有时现金记 2、保护线调为 93；若随后按 98 退出，**总回报**为 0。若在除息日前已退出，不应获得这 2。

### 3.3 收益、风险统计与删失

定义 `gross_pnl_pct = (shares_at_exit × exit_price + cumulative_cash - initial_notional) / initial_notional`；`net_pnl_pct` 在此基础上扣除已有成本情景。若成本模型随股数/换股规模变化，需要显式版本化，不能沿用一个百分比却称真实费用。保留 `price_return_pct`、`cash_dividend_usd`、`split_factor_cumulative`、`shares_at_exit` 供审计，不把原始价跌幅直接叫亏损。MFE/MAE 应用**同一经济权益口径**（当前股数 × 当时高/低价 + 已归属现金），否则拆股日的原始价低点会造成假 MAE。右删失的 `mark_price`、股数与现金同样留痕，不得把右删失当已实现交易。初版可阻断数据不充分的证券区间，优先保证正确性。

## 4. 第二阶段：接入自动质量门与真实样本审计

新增明确的质量表输入（建议 `--quality`），不能仅靠目前的人工 `--exclude-security-id`。逐证券逐区间提供 `security_id,from_session,to_session,quality_status,reason,source_hash`；门应覆盖 setup 决策日、T+1 入场及最多 40 个交易日的退出观察窗。上市日未知的 18 只历史区间不得写为 `verified`；可在本次**已核验普通股主样本**中排除，并在漏斗中单列，不用首根 K 线伪造上市日。`US.HON` 的未解析行动跨过相关区间时阻断绩效；SPY 只作日历，不入交易样本。若希望保留未核验组做探索，须单独实验编号、`unverified` 标签及独立报告，不能混入主结果。

真实数据审计按 `security_id × 年` 汇总：原始 bar 数/重复/缺失、行动数和异常数、as-of 特征可用日数、setup 数、A/B/C 入场数、退出数、右删失数及各拒绝原因。额外抽查全部拆股日前后、全部不一致行动及至少若干现金分红日，逐笔核对股数、现金、旧/新保护线和收益恒等式。对无公司行动且价格一致的区间，旧与新 setup 的**信号方向和时点**应可解释；setup ID 因 `feature_version` 改变不要求相同，须用 `security_id + session + strategy` 配对。任何差异都要归因于特征口径、数据修正、质量门或算法变更，不用单一最终收益掩盖。

## 5. 必补测试与放行检查

| 测试 | 必须断言 |
|---|---|
| T 日拆股、T+1 买入 | T 日不含未来行动；门价经 `scale_to_next` 后与 T+1 原始开盘同尺度 |
| 持仓期 2:1 / 1:2 拆股 | 股数与止损反向换算；纯机械行动的经济权益不跳变，且不产生假跳空止损、假 ATR/MAE |
| 现金分红 | 仅除息日前持有者得到现金；保护线相应调整；总回报含现金，未持有者为 0 分红 |
| 跳空及入场日盘中触线 | 跳空按开盘，盘中按既定止损价；入场日不会对入场前的开盘执行止损 |
| 当日更新保护线 | 先检查旧线，再更新下一日线；止损只收紧（除公司行动的单位换算） |
| 缺失/冲突/跨证券行动 | `security_id` 不串用；无法解算行动与缺 raw bar 必须失败关闭并计数 |
| 质量门 | 未知上市日与 `US.HON` 相关区间不得进主绩效；拒绝分母仍在审计输出 |
| 兼容性 | 旧 `legacy_qfq` 路径产物保持可复现；`raw_asof` 在行动会计完成前继续抛阻断错误 |
| 报告完整性 | `--groups ABC` 有 3×11×4=132 个组/退出/成本单元；D 明确 `inconclusive`；右删失不算已实现收益 |

先跑针对性测试，再跑全量测试及 `git diff --check`。测试通过只是 `engineering_pass`。只有质量门、真实样本对账和预登记也通过，才可设 `research_ready_abc`；否则继续 `blocked`，并列出具体缺口。不得因结果不理想而改阈值、11 种退出定义或成本矩阵；如需调整，另开实验版本。

## 6. 第三阶段：冻结并重跑新 A/B/C

**以下是实施顺序，当前不能直接执行正式矩阵。**先完成 §3–5，提交代码并记录干净 commit；`experiment_manifest.py` 的正式模式会检查 Git 脏工作区。新建不可覆盖目录（建议 `BUY-WD-ABC-SURVIVOR-RAW-001`），记录样本选择日、证券/行动/原始价/as-of 方法版本、所有输入与质量表 SHA-256、开发/验证/测试区间、预登记指标、4 种成本、11 个退出、`--groups ABC`、历史已看过及幸存者偏差说明。Manifest 的 `--quality` 是必填；`--formal` 还需 run-id、版本、样本日期和接受标准。以各脚本当时的 `--help` 确认参数，不复制旧实验目录里的 CSV 后修改时间戳。

目标执行链为：

```text
原始日线/行动/主数据来源与哈希 → 自动质量门 → raw_asof setups
→ build_abcd_entries / universe 过滤 → 冻结 manifest
→ exit_matrix --groups ABC（不复权 + 行动会计）
→ buy_strategy_report --groups ABC → 逐股逐年审计与旧实验配对差异表
```

每阶段保存准确命令、退出码、输入/输出哈希、接受/拒绝行数。报告除总体收益外，至少列出 A→B、B→C 的同候选漏斗、按年/证券/成本/退出的效果、独立组置信区间、右删失、集中度和行动相关的退出数。历史 D 仍为 0，写 `LLM_INCREMENT_STATUS=inconclusive`，不由 A/B/C 推断 LLM 增量。旧 `SURVIVOR-003` 只作诊断对照；该历史窗口已被查看，最终表述为“存续样本历史敏感性分析”。

## 7. 交接包与停止条件

交付给下一位工程师的包应有：代码 commit 与干净状态、原始数据/行动/质量表来源和哈希、自动测试输出、行动逐笔对账样例、质量拒绝漏斗、完整 132 单元报告或 `blocked` 原因、对旧实验的配对差异表，以及一页结论。不得提交本机 `data/`、SQLite、模型密钥或许可受限原始响应。

遇到以下任一情况即停在该阶段：公司行动日期/比例/现金额冲突；原始价或 ATR 无法核验；同证券跨 ticker 映射不明；质量门漏计；右删失被算作盈利；实验目录或 Manifest 已存在；`raw_asof` 阻断尚未用真实行动会计替代。记录证券、日期、证据文件哈希和下一步修复动作，不用默认值继续。
