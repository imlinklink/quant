# 买入策略优化实现说明（2026-09-10）

对应设计：`buy-strategy-optimization-technical-design-2026-09-10.md`。

## 已完成

- 修复 dip_buy 回测的 bar 内时序：先检查上一根已经生效的保护线，再根据当前收盘更新保护线。
- dip_buy 逐笔新增 MFE、MAE、保本阶段、Trailing 阶段和 ATR 阶段字段。
- 新增组合级交易筛选，按入场时间、组合排名和股票代码执行全局最多三仓约束。
- 新增日线 SetupFeatures，包含 MA、ATR、回撤、行业相对强度、确认 pivot 和数据质量门。
- 新增 `FALLING/CAPITULATION/STABILIZING/REVERSING/CONFIRMED` 状态机。
- 新增 `trend_pullback`、`reversal_confirmed` 候选生成。
- 新增 15 分钟已完成 bar 执行触发器。
- 新增 setup state/candidate 的 SQLite 快照和 append-only 事件。
- 新增 setup 的 1/3/5/10/20/40 日 outcome，未来数据不足写 pending。
- EntryPacket 支持携带 `setup_snapshot` 和 `selection_context`。
- 新增收盘后 Daily setup CLI，并接入完整 DRY-RUN 调度。
- `standalone_dip_buy=false` 已接入旧监控器：旧信号继续记录，但不生成确认台提案。
- 新增 A/B/C/D 实验汇总入口并应用组合持仓约束。

## 运行方式

收盘后手工生成一次 setup shadow：

```bash
python3 scripts/live_trading/run_daily_setups.py --config config.yaml --json
```

完整系统会在纽约时间 16:30 自动运行 Daily setup，并在 17:30 运行 Outcome：

```bash
python3 run_all.py --dry-run
```

A/B/C/D 四组逐笔数据完成后汇总：

```bash
python3 scripts/run_buy_strategy_experiments.py \
  --a backtests/group_a.csv \
  --b backtests/group_b.csv \
  --c backtests/group_c.csv \
  --d backtests/group_d.csv
```

## 当前安全状态

- Daily setup 为 `shadow`；
- Entry 和 Position 权限保持原状态；
- 新 setup 不创建确认台 proposal；
- 旧 dip_buy 不再独立创建 proposal；
- 调度任务只读取行情并写决策账本。

## 仍需数据运行才能完成的验收

代码已经具备 A/B/C/D 汇总和 outcome 能力，但具体收益结论必须在历史数据上运行后产生。还需要构建按历史时点变化的 universe，避免当前观察池的幸存者偏差。完成这些数据工作前，不应把新 setup 切到可下单模式。

## 实验基础设施

已按 `buy-strategy-validation-plan-2026-09-10.md` 补齐五个模块：

- `scripts/experiment_manifest.py`：冻结 Git commit、工作区状态、配置、universe、数据文件、区间和成本矩阵；正式冻结拒绝脏工作区及非空目录。
- `scripts/historical_universe.py`：按上市/退市日期和当日价格、成交额生成 point-in-time universe。
- `scripts/buy_strategy_experiment_runner.py`：从冻结信号生成 A/B/C/D，并执行历史时点 universe 门。
- `scripts/exit_matrix.py`：运行 E1–E12 和四档成本，每个矩阵单元按实际退出时间独立执行三仓限制。
- `scripts/buy_strategy_report.py`：输出 independence-group bootstrap、D-C 配对差异、Holm 校正、成本、回撤、集中度及最好/最差年份。

五个模块已通过合成数据端到端演练：4 个实验组 × 12 种退出 × 4 档成本生成 192 行矩阵，并生成 192 个指标单元和 48 个 D-C 配对单元。演练没有读取正式测试期数据。
