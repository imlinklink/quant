# 周线/日线买入策略落地操作手册（2026-09-11）

## 1. 当前阶段

系统已经完成从15分钟抄底转向“周线环境 + 日线Setup + LLM研究 + T+1执行”的代码改造。下一阶段的目标是用本地历史日线跑通第一轮可复现实验，再用每日shadow数据验证运行时链路。

执行顺序固定为：

```text
准备Security Master
→ 下载并缓存日线
→ 标准化与质量检查
→ 构建历史时点股票池
→ 生成历史Setup和LLM标签
→ 冻结实验Manifest
→ 运行A/B/C/D × E1-E11
→ 审查结果
→ 启动日常shadow
```

指标和阈值在第一轮实验完成前保持冻结。

## 2. 本轮交付边界

第一轮只验证数据和实验管道，不直接启用真实买入：

- 行情：历史日线；
- 股票数：20～50只；
- 周线：由已完成日线聚合；
- 执行：T日收盘形成Setup，T+1开盘价；
- LLM：历史实验先使用冻结标签，在线继续shadow；
- 账户：DRY-RUN；
- 正式下单权限：不提升；
- 15分钟数据：不下载、不进入新实验。

## 3. 环境准备

### 3.1 Python

在项目目录执行：

```bash
cd /Users/wh1817w/Documents/quant/quant_us-main
python3 --version
python3 -c "import pandas, numpy, yaml; print('ok')"
```

Futu SDK和OpenD需要正常可用。运行：

```bash
python3 scripts/live_trading/preflight_check.py
```

本阶段只需要行情权限，不需要真实交易解锁。

### 3.2 OpenD

1. 启动Futu OpenD；
2. 登录具有美股行情权限的账户；
3. 确认监听地址为`127.0.0.1:11111`；
4. 确认可以查询SPY和试验股票的历史日线；
5. 不使用`--real`启动交易系统。

## 4. 准备Security Master

### 4.1 第一轮股票池

建议选择20～50只证券，至少覆盖：

- 10只以上大中盘普通股；
- 5只半导体或AI基础设施股票；
- 3～5只小盘或高波动普通股；
- SPY和行业ETF；
- 2只以内杠杆ETF，单独报告；
- 当前观察池。

`US.SPY`必须存在，因为流水线用SPY实际日线确定交易session。

### 4.2 文件格式

建立：

```text
data/security_master_pilot.csv
```

格式：

```csv
code,listing_date,delisting_date,asset_type,name,source,as_of
US.SPY,1993-01-29,,etf,SPDR S&P 500 ETF,pilot_manual,2026-09-11
US.AAPL,1980-12-12,,stock,Apple,pilot_manual,2026-09-11
```

允许的资产类型：

```text
stock
etf
leveraged_etf
```

第一轮手工master只用于管道验收，必须在报告中注明幸存者偏差。正式实验需要包含退市证券和ticker历史的point-in-time数据源。

### 4.3 校验并标准化

如果master来自多个文件：

```bash
python3 scripts/data/build_security_master.py \
  --input data/master_part_1.csv data/master_part_2.csv \
  --output data/security_master_pilot.csv
```

脚本拒绝重复代码、上市日期冲突、非法资产类型以及退市早于上市。

## 5. 一键运行数据流水线

推荐先运行完整命令：

```bash
python3 scripts/data/run_daily_pipeline.py \
  --master data/security_master_pilot.csv \
  --start 2015-01-01 \
  --end 2026-09-10 \
  --universe-start 2016-01-01 \
  --universe-end 2026-09-10 \
  --run-id PILOT-20260911
```

也可以使用：

```bash
make data-pipeline \
  MASTER=data/security_master_pilot.csv \
  END=2026-09-10 \
  RUN_ID=PILOT-20260911
```

流水线执行：

1. 校验并复制Security Master；
2. 按股票和年份分页下载日线；
3. 把原始分区保存到`data/market_history/raw/day/`；
4. 写入下载检查点和SHA-256；
5. 标准化日线；
6. 用SPY的实际交易日生成日历；
7. 生成T-1流动性特征；
8. 生成逐股质量报告；
9. 构建point-in-time universe；
10. 写入本次流水线清单。

## 6. 本地缓存和重复运行

原始行情保存在：

```text
data/market_history/raw/day/year=YYYY/US_SYMBOL.csv.gz
```

下载状态保存在：

```text
data/market_history/checkpoints/download_state.json
```

相同股票、年份和覆盖区间已经下载且哈希一致时，下载器直接复用。结束日期向后延伸时，下载器合并新结果、按`code + time_key`去重并更新检查点。

每次流水线的标准化快照保存在：

```text
data/market_history/runs/<run-id>/
```

`run-id`不可覆盖。失败后检查该目录中的`pipeline.json`，修复原因后使用新的run-id重新执行。原始缓存仍会复用。

## 7. 流水线产物

成功后检查：

```text
data/market_history/runs/PILOT-20260911/
  pipeline.json
  security_master.csv
  daily.csv.gz
  trading_calendar.csv
  daily_liquidity.csv.gz
  daily_quality.csv
  universe.csv
```

查看状态：

```bash
python3 - <<'PY'
import json
path='data/market_history/runs/PILOT-20260911/pipeline.json'
print(json.dumps(json.load(open(path)),ensure_ascii=False,indent=2))
PY
```

`pipeline.json.status`必须为`complete`，各stage均应完成。

## 8. 数据质量验收

打开`daily_quality.csv`，重点检查：

```text
quality
coverage
duplicate_bars
invalid_ohlc
invalid_volume
outside_listing_window
reasons
```

进入正式实验的股票区间必须满足：

- 覆盖率不低于98%；
- 没有重复主键；
- 没有非法OHLC；
- 没有负成交量；
- 没有上市前或退市后数据；
- 分页没有失败；
- `liquidity_as_of < universe_date`。

快速检查：

```bash
python3 - <<'PY'
import pandas as pd
p='data/market_history/runs/PILOT-20260911'
q=pd.read_csv(f'{p}/daily_quality.csv')
print(q.groupby(['quality','reasons'],dropna=False).size())
u=pd.read_csv(f'{p}/universe.csv')
print(u.groupby(['asset_type','eligible','reason']).size())
PY
```

质量失败的股票保留在报告中，但不能产生实验交易。

## 9. 周线与日线Setup验收

在线单日扫描命令：

```bash
python3 scripts/live_trading/run_daily_setups.py --json
```

每只股票至少核对：

- `feature_version=weekly-daily-setup-v2`；
- `weekly_regime`属于`trend/recovering/falling`；
- `weekly_gate`与regime一致；
- `falling`时原因为`WEEKLY_REGIME_BLOCKED`；
- 日线状态按`FALLING → STABILIZING → REVERSING → CONFIRMED`推进；
- 候选包含`initial_stop`、`weekly_regime`和`daily_confirmed`；
- 扫描不生成券商订单。

使用冻结日线生成逐日Setup表：

```bash
python3 scripts/data/generate_historical_setups.py \
  --daily data/market_history/runs/PILOT-20260911/daily.csv.gz \
  --config docs/trading-upgrade-settings.yaml \
  --start 2016-01-01 \
  --end 2026-08-31 \
  --output backtests/buy_v2/input/setups.csv
```

没有历史LLM标签时，`llm_decision=missing`，D组暂时为空；之后可通过`--llm-labels`传入包含`setup_id,llm_decision`的冻结文件。Setup输入包含：

```text
setup_id
stock
setup_time
next_open_time
next_open_price
initial_stop
signal_close
atr14
weekly_gate
daily_confirmed
llm_decision
```

`next_open_time`必须晚于`setup_time`。高开超过`signal_close + 0.75 × ATR14`或低于初始止损时不成交。

## 10. LLM历史标签

第一轮有两种方式：

### 10.1 管道验收方式

对小样本Setup离线调用当前DeepSeek，保存结构化输出和输入快照。只用于确认字段、账本和D组过滤可以工作。

### 10.2 正式实验方式

LLM只能看到历史决策时点已经公开的数据，并且每条证据必须有`observed_at/published_at`。不能把今天的公司认知、当前期权数据或未来财报回填到过去。

如果无法取得可靠的历史事件时间数据，D组标记为`inconclusive`；A/B/C仍可继续运行。

## 11. 冻结实验

完成数据质量审查后，先提交代码并保持Git工作区干净。创建正式实验：

```bash
python3 scripts/experiment_manifest.py \
  --experiment-id BUY-WD-EXP-001 \
  --config docs/trading-upgrade-settings.yaml \
  --universe data/market_history/runs/PILOT-20260911/universe.csv \
  --data \
    data/market_history/runs/PILOT-20260911/security_master.csv \
    data/market_history/runs/PILOT-20260911/daily.csv.gz \
    data/market_history/runs/PILOT-20260911/daily_liquidity.csv.gz \
    data/market_history/runs/PILOT-20260911/trading_calendar.csv \
  --quality data/market_history/runs/PILOT-20260911/daily_quality.csv \
  --output-dir backtests/buy_v2/BUY-WD-EXP-001 \
  --root .
```

正式实验不使用`config.yaml`，避免冻结本地API密钥。应准备不含密钥的实验配置文件。

验证：

```bash
python3 scripts/experiment_manifest.py \
  --validate backtests/buy_v2/BUY-WD-EXP-001/manifest.json \
  --root .
```

## 12. 运行A/B/C/D

```bash
python3 scripts/buy_strategy_experiment_runner.py \
  --manifest backtests/buy_v2/BUY-WD-EXP-001/manifest.json \
  --setups backtests/buy_v2/BUY-WD-EXP-001/setups.csv \
  --universe backtests/buy_v2/BUY-WD-EXP-001/universe.csv \
  --max-gap-atr 0.75 \
  --output-dir backtests/buy_v2/BUY-WD-EXP-001/entries
```

四组含义：

| 组 | 条件 |
|---|---|
| A | 日线Setup + T+1开盘 |
| B | A + 周线环境门 |
| C | B + 日线CONFIRMED |
| D | C + LLM candidate |

必须检查`D ⊆ C ⊆ B ⊆ A`。

## 13. 运行E1-E11和报告

```bash
python3 scripts/exit_matrix.py \
  --manifest backtests/buy_v2/BUY-WD-EXP-001/manifest.json \
  --entries backtests/buy_v2/BUY-WD-EXP-001/entries/signals.csv \
  --daily data/market_history/runs/PILOT-20260911/daily.csv.gz \
  --output backtests/buy_v2/BUY-WD-EXP-001/exit_matrix.csv
```

```bash
python3 scripts/buy_strategy_report.py \
  --matrix backtests/buy_v2/BUY-WD-EXP-001/exit_matrix.csv \
  --manifest backtests/buy_v2/BUY-WD-EXP-001/manifest.json \
  --output-dir backtests/buy_v2/BUY-WD-EXP-001/report
```

重点比较：

- B-A：周线环境门；
- C-B：等待日线确认；
- D-C：LLM过滤。

同时查看期望收益、MAE、MFE、最大回撤、年份稳定性、个股集中度、成本敏感性和置信区间。

## 14. 日常Shadow运行

历史管道正确后：

```bash
python3 run_all.py --dry-run
```

服务会启动Web、日线Setup调度、唐奇安监控、止盈止损和Outcome任务，不再启动15分钟DipBuyMonitor。

收盘后检查：

```bash
python3 scripts/live_trading/run_daily_setups.py --json
python3 scripts/live_trading/reconcile_selection_decision.py --json
```

关注：

- Setup是否每日只领取一次；
- 决策账本是否关联selection decision；
- 是否存在数据质量失败；
- shadow期间是否误生成订单；
- Outcome是否按1/3/5/10/20/40日补算。

## 15. 故障处理

### OpenD连接失败

检查OpenD是否启动、端口是否为11111、账户是否登录、历史行情权限是否存在。

### 下载分区失败

查看：

```text
data/market_history/checkpoints/download_state.json
```

失败不会删除已完成分区。修复权限或网络后使用新run-id重跑。

### 已有文件哈希不一致

说明本地原始缓存被外部修改。不要强制覆盖；先复制异常文件并调查来源，再明确使用`--overwrite`重新下载。

### 质量覆盖率不足

确认上市日期、退市日期、下载起止时间和分页是否完整。不得直接降低98%门槛来让实验通过。

### Manifest拒绝dirty工作区

先检查差异、运行测试并提交代码。不要通过修改Manifest绕过。

## 16. 推进顺序

### 现在执行

1. 创建20～50只股票的`security_master_pilot.csv`；
2. 启动OpenD；
3. 运行`PILOT-20260911`数据流水线；
4. 检查质量报告；
5. 修复数据源和覆盖问题。

### 数据通过后开发

1. 增加历史事件时间切片和LLM离线标签生成器；
2. 冻结`BUY-WD-EXP-001`；
3. 跑A/B/C/D × E1-E11；
4. 根据预登记标准作出`retain/reject/inconclusive`判定。

### 实验通过后

1. 连续运行至少两个独立交易日shadow；
2. 累计至少三个有效批次；
3. 完成决策账本和Outcome对账；
4. 将合格Setup开放到DRY-RUN确认台；
5. 再决定是否进入SIMULATE。

## 17. 本阶段完成标准

- 一键流水线可重复执行并复用本地日线缓存；
- 每次run拥有不可覆盖目录和完整哈希清单；
- 日线质量和point-in-time universe可审计；
- 正式实验不依赖15分钟数据；
- A/B/C/D和E1-E11命令可运行；
- 全量测试通过；
- 未提升任何真实交易权限。
