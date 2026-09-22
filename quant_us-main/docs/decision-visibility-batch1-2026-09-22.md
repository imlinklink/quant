# 决策可视化第一批 + 第二批 交付记录

日期：2026-09-22。状态：**两批都已上到在跑服务 8890**（`quant-runtime-main` pin `574eb5a`）。
依据：`docs/decision-visibility-product-requirements-2026-09-22.md`（需求草案）。

> 文件名保留 `batch1`（第一批先落地），本文件同时记录第二批（见 §8）。

## 0. 一句话

**六个只读页面已上线，来源是四代账本的统一快照；能回答"为什么买/为什么没买""这笔
持仓为什么还拿着、什么条件卖""模型看没看、改没改动作""数据可不可信"。**
策略与 LLM 的**结论**没有变化 —— 这批交付的是「看得见 + 不骗自己」。

## 1. 架构：为什么必须是「导出器 + 纯快照」

需求 §6 写着「各自由各自固定运行版本提供只读导出，Web 消费统一快照」。落地时发现这
不是偏好而是**硬约束**：

- `ShadowStore.transaction()` 的版本守卫**对读也生效**（`LEDGER_SCHEMA_MISMATCH`）；
- 库里同时存在 **schema 9**（M1 影子实验、三臂前向）与 **schema 10**（L1 持仓实验）；
- ⇒ **没有任何一个 checkout 能用 ORM 打开全部账本**。

于是：

```
账本(schema 9/10) → [导出器：原始 SQL + mode=ro] → JSON 快照 → [Web：只读 JSON]
                     唯一一份指标算法在 report.py         web 零 sqlite、零 scripts.* import
```

**导出器为什么要自带词表**：实测 pin `cf2440d` 的 `llm_overlay.ABSTAIN_REASONS`
**缺 `MODEL_BUDGET_EXHAUSTED`**，而 L1 的账本由 dev 血统的 checkout 写。若借 pin 的词表
解释 L1 的账本，**预算耗尽会被判成「不是程序侧弃权」⇒ 计入「模型知情」** —— 那是静默的
错误分类，不是缺数据。所以 `ops/analytics_export/vocabulary.py` 放一份**并集**，并有测试
钉死「它是当前 checkout 词表的超集，且在交集上逐码分类一致」。

**指标只有一份定义**：`report.py` 的四个指标函数加了 `vocab` 形参（默认仍是本 checkout
的词表），算式一行没改。`report.py` 在 pin 与 dev **逐字节相同**、且**不在** `frozen_code`
里 ⇒ cherry-pick 干净、前向观察不受影响。

## 2. 只读是怎么保证的（不是靠「路由叫 GET」）

三道机制，每一道都有测试：

1. **数据只来自 `latest/` 下的快照**，路径由 `index.json` 的 `file` 字段给出 —— 不接受
   请求里的任意路径（顺手免掉目录穿越），也不列举目录猜。
2. **`web/analytics.py` 不 import 任何 `scripts.*`** ⇒ `ShadowStore` / `PositionRegistry` /
   `ProposalStore` / `ExecutionService` 在 web 进程里**根本调不到**。这不是洁癖：实测
   `PositionRegistry.transaction()` 退出时**无条件** `INSERT OR REPLACE INTO books` + `commit`
   ⇒ **`registry.all()` 其实是一次写操作**。
3. 全 GET、无 POST；筛选/排序只改返回顺序。

`tests/unit/live/test_analytics_readonly.py` 是**本项目第一条「GET 不写」的测试**（此前
零覆盖）：请求期间断言不存在非 `mode=ro` 的 sqlite 连接、账本与快照目录逐字节未变、
并 patch `_create_ctx` 证明无网络。**注入即失败已验证**（给一个"看起来是读"的路由加一次
可写 connect，当场红）。

导出器侧同口径：实测 123 次连接**全部 `mode=ro`**、0 次可写。

## 3. 六页与"不骗自己"的几处关键取值

| 页面 | 关键口径 |
|---|---|
| `/overview` | 每范围**独立成卡、永不合计**；没有待办时明确写「当前无需处理」 |
| `/positions` | 保护位逐字段：schema 9 没有的显示**「未采集」而不是 0**；回吐在股数变动后金额口径**不适用** |
| `/opportunities` | 容量分配阶段显示**「该阶段未采集」**，不从两套成交集合差异反推挤占 |
| `/llm-impact` | `portfolio`/`review` 两角色显示**「未采集 + 原因」而不是 0**；费用未知单列不并成 0 |
| `/experiments` | 判定与机读结果并列，**不给完成百分比**、不生成「策略提升」结论 |
| `/decisions/<id>` | 时间线按实际发生顺序；**关联缺失显示断点**，不按代码+近似时间拼轨迹 |

**L1 的初始化回放**（场景 7）在快照里被显式分开：`nav_replay_sessions=24`、
`nav_forward_sessions=0`、`model_valid_sample_count=0`。任何按 `daily_nav` 求和画线的实现
都会把 24 天初始化回放画成前向业绩 —— 这是本批最值钱的一条防线。

## 4. 验收（需求 §9 首批出口）

从 `/overview` → `/positions` → 一笔 → `/decisions/<id>` 走通（用 M1 的 `SEC-US-LITE`，
L1 当前无持仓）：

| 问题 | 页面给出的答案 |
|---|---|
| 为什么持有 | 规则 B3 入场，退出政策 H60，持有 2 个 session |
| 什么条件卖 | 时间退出 H60 + 初始止损 $783.17 |
| 模型是否改变了动作 | R：`INTENT_CREATED`（无模型决策）；L：`ABSTAIN/DECISION_DEADLINE_MISSED` —— **评审窗口已过、模型从未被咨询**，页面把它与「模型自己弃权」区分开 |
| 数据可不可信 | `data_status=OK`、`as_of=2026-09-18`、账本 schema 9、逐字段「未采集」说明 |

## 5. 上线与核验（运行版本隔离合同）

| 项 | 结果 |
|---|---|
| `main` | `1c9256b`（导出层）+ `7d4cd4f`（web）+ `c76bbaa`（测试） |
| `quant-runtime-main` pin | `cf2440d` → **`31c59ff`**（选择性 checkout，**刻意不带 `ops/install_launchd.py`**） |
| 12 个冻结文件哈希 | **12/12 一致** ✓（页面与导出层都不在冻结集里） |
| 从 pin 跑导出器 | 成功，26 个文件、10 个范围全 OK |
| 新测试在 pin 上 | 24 passed |
| `install_launchd --check` / `install_cron --check` / `check_config_record` | 三项全绿 ✓ |
| 服务 health 与九个路由 | 全 **200** ✓（服务已 `kickstart -k` 重启加载新路由） |
| 看护 | ✅ 全部通过 |

**新增排程** `com.quant.web-snapshots`（北京周二~周六 21:30 / 22:30）：排在所有写账本的
作业之后（影子日到 19:40、L1 到 20:40、三臂到 21:10）。

**`ops/install_launchd.py` 刻意不进运行 checkout**：它只服务开发 checkout 的
`--install`/`--check`（看护从开发 checkout 跑），而 pin 上的那一份是运行版本隔离之前的
旧版；带过去只会制造无谓冲突。

## 6. 上一版就存在的两处限制（本批未修，如实记）

1. **快照目录里的 `analytics_scopes.json` 在 pin 与 dev 各有一份**。新实验登记只在 main 上
   更新，pin 的副本要等下一次运行版本移动才同步 ⇒ 页面会**看不见**新范围（不是显示错，
   是根本不列）。这是显式的、可见的，但需要在下一次 move pin 时记得。
2. **实盘范围（`live:SIMULATE`）没有净值与保护线**：`execution.sqlite3` 只保存 `books`
   两个 JSON blob，净值在监控器内存里、保护线在 chandelier 内存里，账本里没有落盘副本。
   页面如实显示「未采集」。

## 7. 复现

```bash
cd /Users/wh1817w/quant
python3 ops/build_web_snapshots.py --print        # 只看 index
python3 ops/build_web_snapshots.py                # 写一代快照
python3 -m pytest quant_us-main/tests/unit/ops/test_analytics_export.py \
                  quant_us-main/tests/unit/live/test_analytics_readonly.py -q
curl -s http://127.0.0.1:8890/api/analytics/scopes
```

## 8. 第二批（同日，`caa478f` / pin `574eb5a`）

需求 §8 第二批是「看清策略和 LLM 是否有效」。**纯新增，零删除** —— 第一批的 section 结构
接得住（6 文件 +316 行）。

**核心是四份研究归一成同一张对比表。** 它们产物形状各不相同（诊断研究是
`comparison`+`statistics`，策略研究是 `step1`/`entry_arms`/`sleeve`），
`normalize_study` 显式按形状取值：

| 实验 | 类型 | 判定 | 扣费后收益 | 胜率 | MDD | 尾部 |
|---|---|---|---|---|---|---|
| SD-P0P1-20260921-012 | 冻结基线 | `INSUFFICIENT_EVIDENCE` | 266.15% | 52.79% | −13.87% | 未采集 |
| S1 机械利润保护 | 卖出·同机会对照 | `NO_IMPROVEMENT` | 未采集（只给 R 汇总） | 只报计数 | — | A 1.376 → B 1.338 |
| B1 抄底替换 | 买入·替换 | `RISK_TRADEOFF` | 未采集（只给差值） | 未采集 | 未采集 | Δ+0.454R |
| 抄底 sleeve | 买入·补充 | `RISK_REJECTED` | 231.91% | 49.31% | −14.55% | −1.865R |

**几条刻意的口径**：
- **取不到就不填**：差额类产物（抄底替换只给差值）的绝对值标「未采集」，**不在此处与基线
  相加合成** —— 合成出来的数没人复核，而页面并列显示基线行与差值行已经足够。
- **胜率只给计数时只报计数**：S1 的产物给的是 `n_profitable/n_closed`，比值口径（分母是
  197 还是 201）要看报告，**不在这里相除**。
- **权衡并列、不给结论**：S1 就是实例 —— 盈利笔数 104→113（升）而净 R 161.9→90.8（降），
  两件事同时可见，页面不写成「策略提升」（需求 §5.5、场景 8）。
- 每行带**产物 sha256**，结论可溯源到文件。

**顺带**：`/opportunities` 加「应用动作的原因分布」（区分模型自己弃权 vs 程序侧弃权 ——
后者意味着模型从未被咨询）；`/llm-impact` 加「最有帮助/最有损害」与「提前退出的收益与
损害」两块。两项当前都**没有成熟样本**，如实显示「未采集 + 原因」，不在噪声上排名次。

**一个我犯的错（已补测试）**：第一次实现把基线胜率映射到 `statistics.exits.realized_win_rate`，
而它其实在 **`comparison.exits`** 里 ⇒ 页面静默显示「未采集」。新增的
`StrategyComparisonTests` 钉死字段路径与四种形状的识别，形状不认识就红。
