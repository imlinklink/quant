# 周线/日线买入策略测试与交接手册（2026-09-11）

## 1. 文档目的

本文供没有参与前期开发的人直接接手。执行者应按本文顺序完成代码冻结、数据审计、历史实验、LLM 标签验证、退出矩阵、报告审查和 shadow 联调。任何一步不满足验收条件，都应停止进入下一阶段并保留失败证据。

本文以项目目录为：

```text
/Users/wh1817w/Documents/quant/quant_us-main
```

本文是当前周线/日线方案的测试入口。`buy-strategy-validation-plan-2026-09-10.md` 中仍包含旧 15 分钟 A/B/C/D 定义；涉及主实验分组和退出方法时，以本文及 `weekly-daily-buy-strategy-technical-design-2026-09-11.md` 为准。

## 变更记录（2026-09-11 第二轮，优先于下文旧描述）

本轮修复了退出模拟与增量分析，下文相关段落的旧描述以本节为准：

| 项目 | 旧（首轮） | 现（本轮） | 影响章节 |
|---|---|---|---|
| 退出窗口 | `bars[date >= entry_time]` 把成交当日整根 K 线排除，退出从次日开始 | **含成交当日**：按交易日选取入场日及其后 40 个交易日 | §6 §12 |
| 退出时刻 | 交易日 00:00，可能早于入场时刻 | 统一为交易日 09:30 ET，保证 `exit_time >= entry_time` | §12 |
| 增量分析 | 同 setup 配对，嵌套分组下恒为 0 | 两组**聚合期望差**（独立组 bootstrap）+ 年份同向计数 | §13 |
| 报告完整性 | 强制 A/B/C/D 四组共 176 单元 | 支持 `--groups ABC`，ABC 为 3×11×4 = **132 单元** | §8.3 §13 |
| 集中度 | 按净收益总额（净额≈0 或为负时失真） | 按**毛利**（正贡献之和）比例 | §13 |
| 报告分层 | 无 | **资产类型切片**（普通股/ETF/杠杆 ETF）+ 集中度列，自动读 manifest 的 `security_master.csv` | §9 §13 |
| 脚本运行 | 需 `PYTHONPATH=.` | 已加 `sys.path` bootstrap，`python3 scripts/X.py` 直接可用 | §4 §10 §12 §13 |
| 全量测试 | 461 | 480 | §4.3 |

对应实现提交：`169aff2`（`--groups`）、`037d4b1`/`fb8a34b`（退出缺陷修复与提速）、`b9c7f67`（增量改聚合对比）、`7843a6c`/`68b517d`（资产切片与集中度）。首个 A/B/C 工程实验与登记见 `backtests/buy_v2/BUY-WD-ABC-EXP-001/` 和 `backtests/buy_v2/experiment-log.md`。

## 2. 当前系统状态

截至 2026-09-11，已经完成：

- 新买入主链路改为“周线环境门 + 日线状态机 + LLM 研究过滤 + T+1 开盘执行”；
- 新策略不依赖 15 分钟 K 线；
- 历史行情统一采用 Futu `QFQ` 前复权日线；
- Security Master、断点下载、标准化、交易日历、数据质量、T-1 流动性和历史时点 universe 已有脚本；
- 历史 setup、A/B/C/D 入场、E1-E11 退出矩阵和统计报告已有脚本；
- 最近一次全量测试结果为 `480 passed`（本轮修复后；首轮为 461）。

首轮交接时以下文件尚未提交；**本轮已全部提交**（当前实现提交见开头「变更记录」）：

```text
Makefile
docs/weekly-daily-strategy-operations-manual-2026-09-11.md
scripts/buy_strategy_experiment_runner.py
scripts/data/build_security_master_from_futu.py
scripts/data/download_market_history.py
scripts/data/generate_historical_setups.py
scripts/data/run_daily_pipeline.py
tests/unit/live/test_market_data_pipeline.py
```

当前 HEAD 在交接时为 `ad59e2f`。这只是修改前基线，不能作为本轮实验代码版本。

本地已有试点快照：

```text
data/market_history/runs/PILOT-QFQ-20260911-R2/
```

已核对结果：

| 项目 | 数值 |
|---|---:|
| Security Master 标的 | 13 |
| QFQ 年度有效分区 | 121 |
| 上市前不可用分区 | 35 |
| 标准化日线 | 28,672 |
| SPY 交易日 | 2,939 |
| 数据质量通过 | 13/13 |
| universe 记录 | 15,796 |
| eligible 记录 | 12,427 |
| 历史 setup | 2,297 |
| weekly gate 通过 | 1,802 |
| weekly gate 阻断 | 495 |
| LLM 标签 | 2,297 条全部为 `missing` |

这个快照只能用于管道、时序和代码验收，不能用于宣称策略有效，原因是：

1. 股票池只有 13 只，且主要来自当前观察池，存在严重幸存者偏差；
2. 没有完整退市证券和历史成分；
3. 没有历史时点 LLM 标签，D 组为空；
4. 尚未完成按市场阶段、资产类型、年份和成本的样本外检验。

## 3. 必须遵守的安全和研究边界

1. 本文所有历史命令都不需要交易权限。
2. 联调只允许 `--dry-run`、`shadow` 或 `SIMULATE`。
3. 不得解锁真实交易，不得使用 `--real`，不得确认真实订单。
4. 不得将 API 密钥复制进实验目录或 manifest。
5. 不得使用今天的新闻、期权数据、评级或公司认知标注过去的 setup。
6. 测试集结果打开后，不得修改同一实验的阈值；任何修改创建新实验编号。
7. 不得覆盖旧 run-id 或实验目录。
8. 不得降低 98% 数据覆盖门槛来掩盖数据问题。
9. 普通股票、ETF、杠杆 ETF 和次新股必须分开报告。
10. 任何收益结果都必须同时报告成本、回撤、MAE、年份稳定性和集中度。

## 4. 接手后的第一小时

### 4.1 检查环境

```bash
cd /Users/wh1817w/Documents/quant/quant_us-main
python3 --version
python3 -c "import pandas, numpy, yaml; print('python dependencies ok')"
git status --short
git diff --check
```

预期：Python 依赖可导入，`git diff --check` 无输出。当前工作区有上述未提交修改是已知状态；除此之外出现的新文件或改动必须先查明来源。

### 4.2 审查差异

```bash
git diff --stat
git diff -- scripts/data/download_market_history.py
git diff -- scripts/data/run_daily_pipeline.py
git diff -- scripts/data/generate_historical_setups.py
git diff -- scripts/buy_strategy_experiment_runner.py
```

重点确认：

- Futu 下载口径是 `AuType.QFQ`；
- 缓存路径包含 `raw/day/qfq/`；
- 检查点 key 包含 `code|kind|qfq|year`；
- 流水线只读取 QFQ 目录；
- 旧未复权失败记录不会导致 QFQ 任务失败；
- 已确认不可用的上市前年份不会反复下载；
- 历史 baseline 允许绕过 weekly gate，在线状态机仍要求 weekly gate；
- T+1 时间严格晚于 setup 时间。

### 4.3 运行测试

Futu SDK 导入时会写用户日志目录，因此在受限沙箱中全量测试可能出现：

```text
PermissionError: ~/.com.futunn.FutuOpenD/Log/...
```

这不是策略失败。应在正常本机终端运行：

```bash
python3 -m pytest -q
```

验收：

- 所有测试通过；
- 当前参考值为 480 项（首轮 461；本轮新增资产切片/§12 校验/退出缺陷回归等测试）；新增合理测试后数量可以增加；
- 不允许通过跳过失败测试来提交。

另外运行定向测试：

```bash
python3 -m pytest \
  tests/unit/live/test_market_data_pipeline.py \
  tests/unit/live/test_setup_state_machine.py \
  tests/unit/live/test_buy_strategy_experiment_runner.py \
  tests/unit/live/test_exit_matrix.py \
  tests/unit/live/test_buy_strategy_report.py \
  tests/unit/live/test_experiment_manifest.py -q
```

### 4.4 提交代码

测试通过后提交。提交中不得包含本地密钥、Futu 日志、原始大数据或个人配置。

```bash
git status --short
git diff --check
git add \
  Makefile \
  docs/weekly-daily-strategy-operations-manual-2026-09-11.md \
  docs/weekly-daily-strategy-test-handoff-manual-2026-09-11.md \
  scripts/buy_strategy_experiment_runner.py \
  scripts/data/build_security_master_from_futu.py \
  scripts/data/download_market_history.py \
  scripts/data/generate_historical_setups.py \
  scripts/data/run_daily_pipeline.py \
  tests/unit/live/test_market_data_pipeline.py
git commit -m "完善周线日线策略研究数据管道"
git status --short
```

验收：最后一条命令无输出。记录：

```bash
git rev-parse HEAD
```

后续 manifest 必须引用这个干净提交。

## 5. 第一阶段：重现试点数据管道

### 5.1 前置条件

- Futu OpenD 已启动；
- 地址为 `127.0.0.1:11111`；
- 已登录并具备美股历史日线权限；
- 不需要交易解锁。

### 5.2 检查 Security Master

已有本地文件：

```text
data/security_master_pilot.csv
```

需要重新生成时执行：

```bash
python3 scripts/data/build_security_master_from_futu.py \
  --config config.yaml \
  --include US.SPY \
  --output data/security_master_pilot.csv \
  --overwrite
```

Futu 会填充代码、名称、当前证券类型和可获得的上市日期。脚本还会识别常见杠杆 ETF。`delisting_date` 不能依靠当前 Futu 证券目录完整获得；正式无偏实验必须另接历史证券主数据。

检查：

```bash
python3 - <<'PY'
import pandas as pd
p='data/security_master_pilot.csv'
d=pd.read_csv(p)
required={'code','listing_date','delisting_date','asset_type'}
assert required.issubset(d.columns), required-set(d.columns)
assert d.code.is_unique
assert 'US.SPY' in set(d.code)
assert set(d.asset_type).issubset({'stock','etf','leveraged_etf'})
print(d.groupby('asset_type').size())
print(d[['code','listing_date','delisting_date','asset_type']].to_string(index=False))
PY
```

不得把 1970 占位日期理解为真实上市日期。流水线会用首根历史日线修正晚于下载起点的新上市证券；早于下载起点的证券只能判断为“此前已经上市”。

### 5.3 重跑流水线

每次使用新 run-id，例如：

```bash
python3 scripts/data/run_daily_pipeline.py \
  --master data/security_master_pilot.csv \
  --start 2015-01-01 \
  --end 2026-09-10 \
  --universe-start 2016-01-01 \
  --universe-end 2026-09-10 \
  --run-id PILOT-QFQ-HANDOFF-001
```

预期会复用：

```text
data/market_history/raw/day/qfq/year=YYYY/*.csv.gz
```

禁止从旧的 `raw/day/year=...` 或未复权目录组装正式数据。

### 5.4 检查 pipeline.json

```bash
python3 - <<'PY'
import json
p='data/market_history/runs/PILOT-QFQ-HANDOFF-001/pipeline.json'
d=json.load(open(p))
print(json.dumps(d,ensure_ascii=False,indent=2))
assert d['status']=='complete'
assert d['stages']['download']['adjustment']=='qfq'
assert d['stages']['quality']['failed']==0
assert d['stages']['calendar']['source']=='US.SPY daily'
PY
```

验收：所有 stage 完成，下载失败为零，质量失败为零。`unavailable` 可以大于零，但必须全部对应上市前区间，而不是权限、限流或网络失败。

### 5.5 数据质量审计

```bash
python3 - <<'PY'
import pandas as pd
p='data/market_history/runs/PILOT-QFQ-HANDOFF-001'
q=pd.read_csv(f'{p}/daily_quality.csv')
u=pd.read_csv(f'{p}/universe.csv')
d=pd.read_csv(f'{p}/daily.csv.gz')
print(q.to_string(index=False))
assert (q.quality=='good').all()
assert (q.coverage>=.98).all()
assert q.duplicate_bars.sum()==0
assert q.invalid_ohlc.sum()==0
assert q.invalid_volume.sum()==0
assert q.outside_listing_window.sum()==0
assert not d.duplicated(['stock','date']).any()
assert (pd.to_datetime(u.liquidity_as_of) < pd.to_datetime(u.universe_date)).all()
print(u.groupby(['asset_type','eligible','reason'],dropna=False).size())
PY
```

若失败：保留 run 目录，不得编辑结果文件。调查下载检查点、上市日期、权限和源数据后，用新 run-id 重跑。

## 6. 第二阶段：历史 setup 时序测试

```bash
python3 scripts/data/generate_historical_setups.py \
  --daily data/market_history/runs/PILOT-QFQ-HANDOFF-001/daily.csv.gz \
  --config docs/trading-upgrade-settings.yaml \
  --start 2016-01-01 \
  --end 2026-09-10 \
  --output data/market_history/runs/PILOT-QFQ-HANDOFF-001/setups.csv
```

检查：

```bash
python3 - <<'PY'
import pandas as pd
p='data/market_history/runs/PILOT-QFQ-HANDOFF-001/setups.csv'
d=pd.read_csv(p)
required={'setup_id','stock','setup_time','next_open_time','next_open_price',
          'initial_stop','signal_close','atr14','weekly_gate',
          'daily_confirmed','weekly_regime','llm_decision'}
assert required.issubset(d.columns), required-set(d.columns)
assert d.setup_id.is_unique
assert (pd.to_datetime(d.next_open_time,utc=True) >
        pd.to_datetime(d.setup_time,utc=True)).all()
assert (d.next_open_price>0).all()
assert (d.initial_stop>0).all()
assert set(d.weekly_regime).issubset({'trend','recovering','falling'})
assert not (d.weekly_gate & d.weekly_regime.eq('falling')).any()
print('rows',len(d))
print(d.weekly_regime.value_counts())
print(d.llm_decision.value_counts(dropna=False))
PY
```

必须人工抽查至少 30 条，覆盖：

- 10 条 `trend`；
- 5 条 `recovering`；
- 5 条 `falling`；
- 5 条 `daily_confirmed=True`；
- 5 条次新股或杠杆 ETF。

每条人工抽查只使用 setup 当日及以前的数据，确认周线由已完成日线聚合、日线状态没有读取未来 bar、成交日为下一交易日且成交价为下一交易日开盘。

## 7. 第三阶段：A/B/C 入场漏斗测试

当前分组定义固定为：

| 组 | 条件 |
|---|---|
| A | 日线 setup + T+1 可成交门 |
| B | A + weekly gate |
| C | B + daily confirmed |
| D | C + 历史时点 LLM 接受 |

用试点数据做管道统计：

```bash
python3 - <<'PY'
import pandas as pd
from scripts.buy_strategy_experiment_runner import build_abcd_entries,apply_universe
p='data/market_history/runs/PILOT-QFQ-HANDOFF-001'
s=pd.read_csv(f'{p}/setups.csv')
e=build_abcd_entries(s,max_gap_atr=.75)
a,r=apply_universe(e,pd.read_csv(f'{p}/universe.csv'))
print('before universe',e.experiment.value_counts().sort_index().to_dict())
print('accepted',a.experiment.value_counts().sort_index().to_dict())
print('rejected',r.experiment.value_counts().sort_index().to_dict())
for child,parent in [('D','C'),('C','B'),('B','A')]:
    cs=set(e.loc[e.experiment==child,'setup_id'])
    ps=set(e.loc[e.experiment==parent,'setup_id'])
    assert cs<=ps,(child,parent,len(cs-ps))
PY
```

预期 `D ⊆ C ⊆ B ⊆ A`。当前无历史 LLM 标签时 D=0 是正确结果，不得把 `missing` 当作接受，也不得用随机标签形成正式 D 组。

## 8. 第四阶段：补齐历史 LLM 标签

这是进入正式 A/B/C/D 实验前的阻断项。

### 8.1 标签文件契约

至少包含：

```text
setup_id
llm_decision
model
prompt_version
evidence_cutoff
generated_at
input_hash
output_hash
```

`llm_decision` 允许值应与实验代码保持一致：

```text
candidate
support
support_execute
observe
reject
insufficient_evidence
```

### 8.2 时间真实性

对 setup 时间为 T 的记录：

- 行情只能截止 T 收盘；
- 新闻、财报和公告必须满足 `published_at <= evidence_cutoff`；
- 期权数据只有具备历史快照时才允许输入；
- 当前基本面、当前分析师评级、当前 Max Pain 不得回填；
- 数据不足时必须输出 `insufficient_evidence`。

### 8.3 两种测试

管道契约测试可以复制少量 setup，使用明确标记为 `synthetic_contract_test` 的固定标签，验证 join、D 子集和报告代码。这些结果不得进入收益报告。

正式实验必须使用真实历史时点证据生成冻结标签。若无法建立可靠的历史证据库，正式结论限定为 A/B/C，LLM 增量判定为 `inconclusive`。

**已实现（本轮）**：`exit_matrix.py` / `buy_strategy_report.py` 均支持 `--groups ABC`（默认 `ABCD`，保持兼容；只接受 A/B/C/D 的有序子集，且必须与 `manifest.experiment_groups` 一致）；`experiment_manifest.py` 冻结 `experiment_groups` 与 `llm_evaluation`，缺 D 时强制 `status=inconclusive`。报告在无 D 时输出 `LLM_INCREMENT_STATUS = inconclusive` / `reason = HISTORICAL_LLM_LABELS_UNAVAILABLE`，不显示空表。因此 D 为空时可直接运行 A/B/C（§10–§13 命令加 `--groups ABC`），无需伪标签。实验编号用 `BUY-WD-ABC-EXP-001`；未来补齐真实标签后另建 `BUY-WD-ABCD-EXP-001`，二者不得互相覆盖。

标签冻结后必须重新生成一个新 setup 文件，不能覆盖无标签版本：

```bash
python3 scripts/data/generate_historical_setups.py \
  --daily data/market_history/runs/FORMAL-QFQ-001/daily.csv.gz \
  --config docs/trading-upgrade-settings.yaml \
  --llm-labels data/market_history/runs/FORMAL-QFQ-001/llm_labels.csv \
  --start 2016-01-01 \
  --end 2026-09-10 \
  --output data/market_history/runs/FORMAL-QFQ-001/setups-labeled.csv
```

核对 `setup_id` join 覆盖率、重复标签和未匹配标签。正式 manifest 应登记 `setups-labeled.csv`，后续命令中的 `setups.csv` 应由这个文件复制而来。

## 9. 第五阶段：扩大正式股票池

13 只试点股票不足以做策略结论。正式实验开始前应准备 point-in-time Security Master，最低要求：

- 20～50 只仅用于工程预实验；
- 最终统计实验建议更大且覆盖退市样本；
- 包含大中盘股票、小盘股票、次新股、行业 ETF；
- 杠杆 ETF 单独成层，不计入普通股票主结论；
- 每个代码有真实上市日期、退市日期、资产类型和 ticker 历史；
- 记录数据源、提取时间和版本。

没有退市数据时，可继续工程验证，但实验结论必须标为 `inconclusive`，不得写成策略有效。

## 10. 第六阶段：冻结正式实验

只有以下条件全部满足才执行：

- Git 工作区干净；
- 全量测试通过；
- 正式股票池和数据质量通过；
- setup 已生成；
- LLM 标签已经冻结，或已明确采用只评估 ABC 的新实验协议；
- 开发、验证、测试日期已经登记；
- 尚未查看测试期收益。

先创建唯一实验编号，例如：

```text
BUY-WD-EXP-001
```

创建 manifest：

```bash
python3 scripts/experiment_manifest.py \
  --experiment-id BUY-WD-EXP-001 \
  --config docs/trading-upgrade-settings.yaml \
  --universe data/market_history/runs/FORMAL-QFQ-001/universe.csv \
  --data \
    data/market_history/runs/FORMAL-QFQ-001/security_master.csv \
    data/market_history/runs/FORMAL-QFQ-001/daily.csv.gz \
    data/market_history/runs/FORMAL-QFQ-001/daily_liquidity.csv.gz \
    data/market_history/runs/FORMAL-QFQ-001/trading_calendar.csv \
    data/market_history/runs/FORMAL-QFQ-001/setups-labeled.csv \
    data/market_history/runs/FORMAL-QFQ-001/llm_labels.csv \
  --quality data/market_history/runs/FORMAL-QFQ-001/daily_quality.csv \
  --groups ABC \
  --output-dir backtests/buy_v2/BUY-WD-EXP-001 \
  --root .
```

`--groups` 写入 manifest 的 `experiment_groups` 并据此决定 `llm_evaluation`（缺 D 时强制 `status=inconclusive`，见 §8.3）。ABC 实验编号用 `BUY-WD-ABC-EXP-001`；补齐真实标签后另建 `BUY-WD-ABCD-EXP-001`，不得互相覆盖。

复制冻结 setup；目标不存在时才执行：

```bash
cp data/market_history/runs/FORMAL-QFQ-001/setups-labeled.csv \
  backtests/buy_v2/BUY-WD-EXP-001/setups.csv
```

验证：

```bash
python3 scripts/experiment_manifest.py \
  --validate backtests/buy_v2/BUY-WD-EXP-001/manifest.json \
  --root .
```

必须返回：

```json
{"valid": true, "errors": []}
```

如返回 `GIT_WORKTREE_DIRTY`、`HASH_MISMATCH` 或日期重叠，停止。不得手改 manifest 绕过。

## 11. 第七阶段：生成 A/B/C/D 冻结入场

```bash
python3 scripts/buy_strategy_experiment_runner.py \
  --manifest backtests/buy_v2/BUY-WD-EXP-001/manifest.json \
  --setups backtests/buy_v2/BUY-WD-EXP-001/setups.csv \
  --universe backtests/buy_v2/BUY-WD-EXP-001/universe.csv \
  --max-gap-atr 0.75 \
  --output-dir backtests/buy_v2/BUY-WD-EXP-001/entries
```

检查：

```bash
python3 - <<'PY'
import pandas as pd
p='backtests/buy_v2/BUY-WD-EXP-001/entries'
a=pd.read_csv(f'{p}/signals.csv')
r=pd.read_csv(f'{p}/rejected_signals.csv')
print(a.experiment.value_counts().sort_index())
print(r.portfolio_reject_reason.value_counts(dropna=False))
assert set(a.experiment)==set('ABCD')
sets={g:set(a.loc[a.experiment==g,'setup_id']) for g in 'ABCD'}
assert sets['D']<=sets['C']<=sets['B']<=sets['A']
assert (pd.to_datetime(a.entry_time,utc=True)>
        pd.to_datetime(a.setup_time,utc=True)).all()
assert (a.entry_price>a.initial_stop).all()
assert (a.entry_price<=a.signal_close+.75*a.atr14).all()
PY
```

拒绝信号必须保留，原因至少能区分质量失败、非历史 universe、流动性和成交 gap 门。

## 12. 第八阶段：运行 E1-E11 退出矩阵

退出定义：

- E1/E2/E3/E4：固定持有 5/10/20/40 个交易日；
- E5：固定止损与 20 日时间退出；
- E6-E9：1.5/2.0/2.5/3.0 ATR Chandelier；
- E10：higher-low 日线结构退出；
- E11：higher-low 与 2ATR 中更紧的保护线。

运行：

```bash
python3 scripts/exit_matrix.py \
  --manifest backtests/buy_v2/BUY-WD-EXP-001/manifest.json \
  --entries backtests/buy_v2/BUY-WD-EXP-001/entries/signals.csv \
  --daily data/market_history/runs/FORMAL-QFQ-001/daily.csv.gz \
  --groups ABC \
  --output backtests/buy_v2/BUY-WD-EXP-001/exit_matrix.csv
```

`--groups` 必须与 `manifest.experiment_groups` 一致（A/B/C 实验用 `ABC`；ABCD 实验用默认 `ABCD`）。退出引擎的两条口径（本轮修复）：

- 模拟窗口**含成交当日**：从入场交易日（`next_open_time` 所在日）起的 40 个交易日，不得跳过成交当日；
- 退出时刻统一为交易日的 09:30 ET，因此 `exit_time >= entry_time` 恒成立。

检查矩阵完整性：

```bash
python3 - <<'PY'
import pandas as pd
p='backtests/buy_v2/BUY-WD-EXP-001/exit_matrix.csv'
d=pd.read_csv(p)
GROUPS='ABC'  # 与 manifest.experiment_groups 一致；ABCD 实验用 'ABCD'
expected={(g,f'E{i}',c) for g in GROUPS for i in range(1,12)
          for c in (.001,.002,.005,.01)}
actual=set(zip(d.experiment,d.exit_method,d.cost_scenario.astype(float)))
assert expected<=actual, expected-actual
assert set(d.data_quality).issubset({'good','missing_future_bars'})
assert (d.exit_price.dropna()>0).all()
assert (pd.to_datetime(d.exit_time,utc=True,errors='coerce')>=
        pd.to_datetime(d.entry_time,utc=True)).dropna().all()
print(d.groupby(['experiment','exit_method','cost_scenario']).size())
print(d.exit_reason.value_counts(dropna=False))
PY
```

人工检查以下路径：

1. 入场日开盘直接低于止损，按开盘价产生 `GAP_STOP`；
2. 日内最低价穿止损，按止损线产生 `STOP`；
3. 当日收盘后计算的新保护线只能从下一交易日生效；
4. 多头保护线只能上移，不能下降；
5. 数据结束不足 40 日时明确标记，而不是静默删除；
6. 四个成本场景均为 0.1%、0.2%、0.5%、1.0%；
7. 每个 experiment/exit/cost 独立应用最多三仓约束。

上述 1–5、7 已有确定性单测（`tests/unit/live/test_exit_matrix.py`：成交当日 GAP_STOP、日内 STOP 按止损线成交、新保护线次日生效、数据不足标 `DATA_END`、三仓按 experiment/exit/cost 独立）；6 由 `COSTS` 常量与矩阵完整性检查覆盖。

## 13. 第九阶段：生成报告但不自动选参数

```bash
python3 scripts/buy_strategy_report.py \
  --matrix backtests/buy_v2/BUY-WD-EXP-001/exit_matrix.csv \
  --manifest backtests/buy_v2/BUY-WD-EXP-001/manifest.json \
  --groups ABC \
  --output-dir backtests/buy_v2/BUY-WD-EXP-001/report
```

`--groups` 必须与 manifest 一致。资产类型切片会自动读取 manifest 的 `security_master.csv`，也可用 `--security-master` 覆盖。

检查：

- 单元数与所选组一致：`--groups ABC` 为 3 组 × 11 种退出 × 4 种成本 = 132；`ABCD` 为 176；
- 报告包含 group bootstrap 95% 区间；
- 增量采用**两组聚合期望差**（独立组 bootstrap）并给出**年份同向**计数；不是同 setup 配对——嵌套分组下配对差恒为 0，无法回答增量；
- 多重比较使用 Holm 校正；
- 分年份报告最好和最差年份；
- 报告 top-2 股票、top-3 交易占**毛利**（正贡献之和）的比例，不是占净额（净额≈0 或为负时失真）；
- **按资产类型切片**：普通股 / ETF / 杠杆 ETF 分开；某类型无可交易样本时必须显式标注（本试点中普通 ETF 无成交）；
- 开发期、验证期、测试期分别报告。

不要用全期收益最高的单元直接宣布胜出。应依次判断：

1. B-A 是否在多个年份同方向，回答 weekly gate 是否有增量；
2. C-B 是否改变期望并跨资产类型同向，回答等待日线确认的价值（注意方向可能按资产类型相反：本试点普通股为正、杠杆 ETF 为负）；
3. D-C 是否有稳定的聚合增量，回答 LLM 是否创造价值（本实验 D 为空，标记 inconclusive）；
4. 退出家族是否跨年份、资产层和成本场景保持方向；
5. 结果是否由少数股票或交易贡献（看集中度列：占毛利比例过高即为集中风险）。

结论只能是：

```text
retain
reject
inconclusive
```

样本不足、置信区间跨零、D 标签不真实、退市数据缺失或集中度过高时，应选择 `inconclusive`。

## 14. 第十阶段：shadow 联调

历史工程链路通过后，启动完整 dry-run：

```bash
python3 run_all.py --dry-run
```

运行期间不得在确认台点击会进入真实账户的操作。收盘后运行：

```bash
python3 scripts/live_trading/run_daily_setups.py --json
python3 scripts/live_trading/reconcile_selection_decision.py --json
```

至少运行两个独立美股交易日并累计三个有效研究批次。逐批核对：

- selection 模式仍为 `shadow`；
- 每个 selection decision 有输入快照、模型、prompt 版本和输出；
- setup 引用正确的 selection decision id；
- LLM 接受、观察、拒绝和资料不足都能进入账本；
- 同一股票同一交易日不重复领取 setup；
- falling 周线状态不能进入可执行集合；
- T 日 setup 最早 T+1 执行；
- 过度高开和跌破初始止损会拒绝；
- 确认台只展示达到规则门槛的候选；
- shadow 不生成券商订单；
- outcome 按 1/3/5/10/20/40 日补算；
- 页面数量能与决策账本 funnel 对账。

如确认台为空，先检查 funnel 各层数量，不要直接降低阈值：

```text
原始股票池
→ 数据质量通过
→ LLM selection 接受
→ weekly gate
→ daily setup
→ daily confirmed
→ gap/stop 可成交
→ confirmation desk
```

## 15. 故障分类与处理

| 现象 | 处理 |
|---|---|
| OpenD 连接失败 | 确认进程、登录、地址、端口和行情权限 |
| Futu SDK 日志 PermissionError | 在有用户目录写权限的本机终端运行 |
| 下载出现限流 | 保留检查点，等待后用新 run-id 重跑；不要删除成功缓存 |
| `EMPTY_RESPONSE` | 判断是否为上市前年份；若非上市前，检查权限和代码 |
| 缓存哈希不一致 | 复制异常文件调查；确认后才重新下载，禁止直接改检查点哈希 |
| coverage < 98% | 查缺失交易日、上市日期、停牌和分页；禁止降低门槛 |
| manifest dirty | 提交或清理明确的代码变更；禁止伪造 clean 状态 |
| D 组为空 | 补真实历史 LLM 标签，或将正式结论限定为 ABC/inconclusive |
| 矩阵缺单元 | 查空组、未来 bar 不足和数据 join；禁止删除 expected 单元 |
| 收益异常巨大 | 首查复权、拆股、entry/exit 时区和杠杆 ETF 混入 |
| 确认台为空 | 查 funnel 拒绝原因，不能先改阈值 |
| shadow 出现订单 | 立即停服务，保存日志，检查模式和执行门 |

## 16. 必须保存的交接证据

每轮实验至少保留：

```text
git commit
git status
pytest 输出
pipeline.json
security_master.csv
daily_quality.csv
universe.csv
setups.csv
llm_labels.csv（如有）
manifest.json
signals.csv
rejected_signals.csv
exit_matrix.csv
metrics.json
report.md
人工抽查记录
实验结论与理由
```

在 `backtests/buy_v2/experiment-log.md` 登记实验编号、父实验、修改前假设、唯一主要变化、预期影响、允许查看的数据、结果和 `retain/reject/inconclusive` 决策。

## 17. 下一位执行者的优先任务

按优先级执行：

1. 审查、测试并提交当前未提交代码；
2. 用新 run-id 重现 QFQ 试点，验证缓存和质量门；
3. 完成 30 条 setup 人工无未来数据抽查；
4. 为 LLM 历史标签设计并实现时点证据快照；
5. 引入包含退市证券的 point-in-time Security Master；
6. 冻结首个正式实验；
7. 运行 A/B/C/D × E1-E11 × 四种成本；
8. 按预登记规则作出结论；
9. 连续运行至少两个交易日 shadow 并完成账本对账。

### 进度（2026-09-11 第二轮）

- **已完成**：1（审查/测试/提交）；3（setup 无未来数据抽查，28 条跨标的 0 失败，记录在 `backtests/buy_v2/setup-lookahead-audit-PILOT-QFQ-20260911-R2.csv`）；6/7/8 的 **A/B/C 部分**（实验 `BUY-WD-ABC-EXP-001`，132 单元，结论 `inconclusive`）。
- **需本机 Futu OpenD**：2（重跑 QFQ 试点）、9（shadow 联调）。
- **需外部数据源**：4（历史时点 LLM 标签）、5（含退市的 point-in-time Security Master），二者是 D 组的阻断项。
- 本轮同时修复了退出模拟成交当日缺失与增量配对两个缺陷（见开头「变更记录」）。

在第 4、5 项完成前，可以证明工程管道正确，但不能证明 LLM 或买入策略具备可交易优势。
