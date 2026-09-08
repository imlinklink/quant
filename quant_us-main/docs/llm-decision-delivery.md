# LLM 决策优化：首批交付与验证

日期：2026-09-08。对应 [实施方案](llm-decision-implementation.md) 第十一节首批清单。
改动前代码已提交为 `928c2e9`。本轮未连接实盘、未发送订单、未修改本地风险参数，未启动或重启交易进程。

## 已实现

| 交付 | 实现及约束 |
| --- | --- |
| P1 统一事件账本 | 复用 execution.sqlite3；账户作用域、稳定 signal_id、版本化计划/输入/评估、独立 order_intent_id/trade_id、成交关联；唯一事件键和内容冲突检测 |
| 扫描到成交链路 | 抄底、突破、回调/突破回踩在 LLM 前记录规则结果；账户持仓和人工选择不再提前擦除已经满足规则的样本；没有提案、模型失败、未操作仍可统计 |
| 必需凭据持久化 | 计划/提案/批准落盘失败不能授权新订单；提交时在执行数据库写锁内核对批准凭据。跨进程读取批准，旧页面版本不匹配会拒绝 |
| outbox | 订单/成交账本与待导出事件在同一事务提交；导出只写 JSONL，不调用交易入口；失败记录 attempts/last_error 并重试。导出采用至少一次投递，读取端按 event_id 去重 |
| 成交经济数据 | 增量成交金额 = 新累计金额 − 旧累计金额；独立交易周期；入场订单终结后冻结 R0；部分卖出不覆盖 R0；费用修订追加事件 |
| P2 交易计划及决策卡 | 原始输入快照、来源引用、事实/推断/反对证据、资料缺口；买卖动作语义独立；显示计划版本、风险和完整输入；修改计划后重新评估和确认 |
| 审批绑定 | 账户、标的、方向、数量上限、报价、价格容忍度、有效期、计划版本/哈希、评估 ID 与输入 ID；反对或暂缓建议需要覆盖理由。已批准/提交后模型回调仅记录，不改订单 |
| 重大事件钩子 | register_material_event 接收明确标记且带来源的重大事件，作废待确认/已批准计划的评估；执行事务还检查批准后事件，覆盖“正在执行但尚未提交”的竞争窗口 |
| P3 影子复核基础 | 风险/目标附近、收盘、显式事件与证据簇去重，读取原计划和上次评估；默认关闭；只追加复核事件，无下单、改单、移动止损权限 |
| P4 首版报告/重放 | 候选→评估→人工→订单→成交数量、阶段耗时、关联完整率、交叉组；A/B 共用预设时钟，C 使用真实确认时间，三个模拟层共享现金、风险组、持仓上限与规则退出；D 使用对应真实成交记录 |

## 数据口径

- signal_id 基于账户、代码、策略配置/实现版本、K 线结束时间和信号类型。15 分钟、日线、事件型样本分开；历史缺失周期/版本仍标 legacy_unknown，不反推。
- 候选集合为具有可评分/可执行规则输入的扫描结果。缺行情或先验策略资格不满足的观察池股票不属于该集合。回调缺板块输入时不伪造规则结果。相同 K 线首次不通过、随后通过，可有两种规则事件，但仍只有一个 signal_id。
- 新闻保留来源 URL、发布时间、取得时间和摘要；缺来源或完整时间的旧新闻只能作为未核实上下文。新事实要求引用输入摘要原文；模型释义放在推断中。此检查证明引用与输入一致，不代表外部报道必然真实。
- 所有新账本时间明确转换为 UTC；日线信号以美东信号日 16:00 标识，程序仍只使用已完成日 K。回放拒绝无时区时间和重复/无效 OHLC。
- 计划 R 使用报价与计划数量的初始止损距离；风险预算另含每股成本预算。R0 使用最终入场数量 × 实际均价与初始止损的绝对距离，在入场订单终结后冻结。部分实现盈亏先扣已取得的累计费用；剩余风险按剩余数量比例计算，跟踪止损不重写整个交易的 R0。
- 成交适配器可接受 cumulative_fee。当前券商订单查询未承诺提供该字段：缺费用时仅显示毛金额盈亏，净金额/R 不计算。历史无初始风险时不以默认 5% 造 R；未知开仓成本不造盈亏。
- LLM 的 token/单价/成本分别保留；供应商没有返回 usage 或未配置单价时标未知。旧模型的 block 在卖出侧转换为“建议持有”，不能直接当买入侧否决汇总。模型自报信心不标成胜率。

## 配置、迁移和查看

配置片段见 [llm-decision-settings.yaml](llm-decision-settings.yaml)。配置样例不包含账户凭证，也不替换现有交易参数。`jsonschema` 是结构化评估的校验依赖，缺少时评估失败并阻止批准，不静默跳过验证。

首次访问旧执行库时，通过 SQLite backup API 生成 `execution.sqlite3.pre-decision-v1.bak` 并进行完整性校验，再创建版本化表。原 books 表内容保留；不补造历史批准与模型结果。迁移和恢复应在停止相关写入进程后部署，不能运行中用旧备份覆盖数据库。离线测试只使用临时库，没有对当前运行库执行迁移。

确认台 `/approvals` 显示计划/卡片；`/api/approvals/<id>/input` 返回冻结输入；`/api/decision-report` 返回首版漏斗报告。持仓影子结果保存为 position_review_requested / position_reviewed 事件。

执行对账和模型完成后会尝试导出 `data/decision_ledger/events-v1.jsonl`。这是增量导出流；切换导出目标不会自动重新导出此前已标记完成的事件。完整重算可以读取 SQLite decision_events 或保留整个导出文件。

```sh
# 项目根目录下执行，均为离线读/重放入口
python3 -m pytest tests/unit -q
python3 scripts/live_trading/decision_ledger/weekly_report.py --events-v1 data/decision_ledger/events-v1.jsonl
python3 scripts/live_trading/decision_ledger/funnel_report.py --events data/decision_ledger/events-v1.jsonl --output funnel.json
python3 scripts/live_trading/decision_ledger/comparison.py --events data/decision_ledger/events-v1.jsonl --bars bars.csv --experiment experiment.json --risk-config risk.json --output comparison.json
```

`bars.csv` 使用带时区的 15 分钟开始时间和 code,date,open,high,low,close。`risk.json` 为现有 risk_budget 节点的脱敏 JSON。实验配置示例（数值只定义离线模拟，不变更实盘参数）：

```json
{
  "experiment_id": "entry-selection-v1",
  "account_scope": "DRY-RUN",
  "decision_delay_seconds": 120,
  "entry_ttl_seconds": 1800,
  "exit_approval_delay_bars": 1,
  "defer_policy": "observe"
}
```

重放只能读取公共决策时钟之前已返回的模型结果；缺失/失败/暂缓组保留。C 中修订过退出规则的计划单列不可比组，不能混入固定退出实验。买入只在下一可用开盘满足限价时尝试一次，退出按指定审批延迟在后续开盘模拟，不假定盘中最佳成交。单信号 1/3/5 个会话窗口收益、MFE/MAE 与账户结果分开，不相加充当组合收益。

## 验证与后续边界

新增合成数据测试覆盖重复扫描和模型回调、数据库恢复、覆盖理由、计划修订、过期/资料不足、错误来源与伪造事实、未来资料、DST、原子回滚、outbox 失败重试、增量成交均价、部分撤单/卖出、R0 冻结、费用更正、重大事件竞争、确认页 API、持仓影子去重和 A/B 公共时钟/持仓上限。测试不初始化真实交易连接。

完整单元测试 76 项通过；Python 语法、确认台 JavaScript 解析、卡片渲染与不可信文本转义检查通过。部署审查与隔离演练见 [deployment-acceptance.md](deployment-acceptance.md)。
[合成闭环报告](decision-ledger-example.json) 展示一笔模拟开平仓：关联完整率 100%，20 股同价进出，费用 2 美元，净盈亏 −2 美元、−0.02R。它仅验证计算链路。

当前交付是首批闭环与后续实验基础，不是整个研究系统的全部验收：

1. 自动公告/财报事件订阅尚未接入 register_material_event。该钩子接收程序或人工已明确标记的重大事件，影子模型不能自行把新闻升级为交易权限。
2. 费用查询的券商专用补齐、迟到实际成交资料与完整账户资金流需要实测适配；缺失项保持不可计算。
3. 首版重放检验入场筛选；独立退出建议实验、完整挂单排队/滑点模型、日期分块置信区间及训练/验证/锁定样本外工作流仍是后续 P4 工作。原 run_strategy_validation.py 的时间切分入口保留；首版对照结果不自称样本外结论。
4. 没有真实的新事件样本与配套历史行情，本轮不声称 LLM 提高胜率、收益或降低回撤。示例报告使用合成行情和模拟评估，不能作为投资表现。
