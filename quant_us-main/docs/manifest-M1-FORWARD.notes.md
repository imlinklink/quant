# 生产 M1 manifest 草案说明

草案：`manifest-M1-FORWARD.draft.json`。`validate` 已通过（`errors: []`），并已试冻结到临时目录
确认 freeze 门放行、冻结产物能通过运行时校验、改任一字段即被 `MANIFEST_CHANGED_SINCE_FREEZE` 拒绝。

## 冻结命令

```bash
cd quant_us-main
python3 -m scripts.portfolio_shadow.cli freeze \
  --manifest docs/manifest-M1-FORWARD.draft.json \
  --start-session 2026-08-16 \
  --output data/portfolio_shadow
# → data/portfolio_shadow/M1-FORWARD-20260917/{manifest.json,ledger.sqlite3}
```

**参数变更必须新建 `experiment_id`** —— 同一个 id 用不同配置再 freeze 会被
`EXPERIMENT_ALREADY_FROZEN` 拒绝（这正是 #16 修复的内容）。

## 字段取值理由

| 字段 | 取值 | 理由 |
|---|---|---|
| `parent_strategy_id/version/code_hash` | B3 / 1 / 当前 HEAD | 绑死跑的是哪个策略版本 |
| `initial_cash` | 100000 | 与已冻结基线 `RISK-RULE-20260916-001` 一致，绩效可比 |
| `single_position_risk_bp` | 100（1.0%） | 冻结基线的取值 |
| `max_weight_bp` / `max_positions` / `top_n` | 2000 / 5 / 5 | 同上 |
| `drawdown_ladder` | 显式写全 7 个键 | 不写则用代码默认值；显式写死才叫冻结 |
| `entry_rule` / `exit_policy_id` / `horizon` | b3 / H60 / 60 | 同上 |
| `universe_id` / `universe_hash` | survivor13-tech / `4f698a95…` | 由 13 只 TECH 的排序列表算出 |

## 三个需要你拍板的点

### 1. `evidence_mode`：草案填的是 `diagnostic`

- **strict** 要求证据的 `observed_at` 落在窗口内 —— 这要求**每天在窗口里导入当天事件**
  （`import-evidence` 默认 `--observed-at-policy ingest`，入库时刻即观测时刻）。做得到就选它，
  它才是「点对点可证明」的等级。
- **diagnostic** 只要求 `published_at ≤ 截止`。第三方历史档案（如 futu 盈利日历）本来就没有
  首次观测时间，只能以 `--observed-at-policy unknown` 导入，因此**只能配 diagnostic**。
- 草案选 diagnostic 是因为**当前唯一可用的证据源是历史档案**。若你打算先搭起每日导入，
  就该改成 strict —— 那才是想要的目标状态。
- 无论选哪个都请记住：**没有每日证据导入时，包是 `LLM_INSUFFICIENT`，模型根本不会被调用**
  （日运行器会打印 `no_opportunities` 与空证据提示）。这不是故障，是如实反映。

### 2. `knowledge_cutoff: "unknown"`

DeepSeek 的真实训练数据截止我们不知道。填 `unknown` 会让 `RealModel` **跳过**该项检查
（而不是通过它）。这是如实的默认；如果你能确认截止日期，填真实值能让
`MODEL_KNOWLEDGE_CUTOFF` 这道门真正生效。**不要为了让它「看起来有值」而随便填一个日期** ——
填错的日期比 unknown 更糟（会给出虚假的保护感）。

### 3. `start_session`（不在 JSON 里，由 `freeze --start-session` 给）

草案建议 **2026-08-16**。这不是随便定的：`start_session` **同时**是
① 账户起算日 与 ② 增量生成器的回放起点，而 ② 需要**约一个月的提前量**才有候选。

实测（同一份 manifest，只改 `start_session`）：

```
start_session=2026-08-01 → prepare(09-15) 找到 1 个候选
start_session=2026-09-10 → prepare(09-15) 0 个候选   ← 08-31 月末选股没被回放，pending 是空的
```

取太晚的后果很隐蔽：实验**永远没有候选**、日运行器每天报 `no_opportunities`，而日志里
看不出「是因为起始日定晚了」。建议留足一个月，之后再考虑给它加个显式守卫。

## 两个已知陷阱

### `data_hashes` 草案是空的，这是刻意的

`data_hashes` **在 `manifest_hash` 里**。前向运行的面板与公司行动**每天都在增长**，如果把它们
的哈希冻进 manifest，第二天运行时哈希就变了 → 被 `MANIFEST_CHANGED_SINCE_FREEZE` 拒绝，
实验一天都跑不下去。

所以 `data_hashes` 只应装**真正不可变**的定义性输入；增长型数据的身份由**每次决策**的
`market_snapshot_id` 与包的 `provenance` 记录。`universe_hash` 已经承担了宇宙定义那一部分。

### `calendar_version` 目前只是声明

`us-nyse-v1` 是个声明串，`Manifest.validate()` 只检查它非空。真正的交易日历来自
`scripts/data/trading_calendar.py` 的 NYSE 规则历 ∪ 实际日线，**与这个字段尚未挂钩**。
改历法不会让实验失效 —— 这是个待补的缺口，不是已生效的机制。
