# LLM最小落地技术设计：真实入场评审与双账户模拟

日期：2026-09-17。状态：待实施设计。代码检查基线：`e9eadb1`。上位路线：[LLM全闭环Roadmap](llm-closed-loop-roadmap-2026-09-17.md)。

## 1. 唯一交付目标

一个固定规则策略产生真实买入机会；真实LLM读取当时已取得的证据，给出PASS／VETO／ABSTAIN；R模拟账户执行规则，L模拟账户应用模型动作；系统记录决策与结果。

第一版不增加LLM选股排序、减仓、持仓评审或卖出权限，不调整买卖策略参数，不连接真实订单执行器。另一项目继续研究策略，本系统通过适配器接收固定版本。外部版本未就绪时可使用现有B3规则推进接入，但不得把历史最优性作为前提。

交付成功有两层：工程上真实输入、真实调用、实际动作映射闭合；效果上是否提高收益需要后续成熟样本。两者分开验收。

## 2. 最新代码现状与必须补齐的差异

| 已有模块 | 可复用内容 | 最小补齐内容 |
|---|---|---|
| `candidate_adapter.py` | `IncrementalCandidateGenerator`、Opportunity | 前瞻模式不能要求未来horizon根行情；生成时间与执行时间分离 |
| `evidence.py` | 双时间字段、packet ID、质量分级 | 保留可读事件原文/摘要、策略原计划；严格校验时区与身份 |
| `llm_overlay.py` | RealModel、FakeModel、三态验证 | 决策截止时间、质量分流、原始响应与调用结果完整保存 |
| `cli.py` | run-session、run-forward | 拆分决策和成交结算，统一调用入口，防止历史回放调用实时模型 |
| `store.py` | 独立SQLite、事件、应用动作与账户状态 | 原子领取模型尝试、保存完整包与回复、动作冻结与恢复 |
| `paper_engine.py`、`replay.py` | 逐日账户及恢复 | 接收已冻结动作，不负责调用LLM；修正确性问题时补针对性测试 |
| `report.py` | 双账户绩效 | 单次决策追踪、覆盖/影响指标、未知模型成本标记 |

代码检查发现：`_norm_events`目前输出来源、时间和content_hash，未保留summary正文；模型拿到哈希无法理解事件。生成器目前检查未来至少60根bar，适合历史结果筛选，不适合真实前瞻候选；不能把“未来结果尚未成熟”当成“今天不应产生信号”。RealModel已经存在，不再重复建设客户端。

## 3. 最小运行时序

使用交易所时区America/New_York和版本化交易日历，存储时刻统一带时区UTC。禁止硬编码全年`13:20 UTC`。

1. 交易日T收盘后，冻结T及以前的行情和规则输入，生成计划在T+1执行的机会。T+1由日历得到，不要求已取得T+1开盘价。
2. 获取截至实际采集时刻已观察到的相关事件，冻结Evidence Packet。模型可以在收盘后调用。
3. 最迟在T+1开盘前10分钟冻结最终动作；如果数据包未就绪或模型无法按期完成，按确定规则记录BLOCK或ABSTAIN。
4. T+1开盘计划只使用此前冻结的机会与动作；收盘后用当天行情机械模拟开盘成交和日内止损，不允许看到当天结果后补作决策。
5. 产生R/L净值及决策追踪报告。

日线开盘成交是模拟假设，并不证明能以同样价格真实成交。明确记录`execution_model=eod_next_open_v1`。

同一机会只有一个冻结证据包和一次逻辑模型尝试。新事件晚到时本版仅留痕，不修改已冻结动作；后续若要支持盘前重新评审，另定版本与截止规则。

## 4. 候选接口

扩展现有Opportunity，最小字段为：

```text
opportunity_id / experiment_id / security_id
parent_strategy_id / parent_version / source_candidate_id
signal_session / signal_generated_at / observed_at
planned_execution_session / decision_deadline
rank / entry_rule / stop_reference / exit_policy_id
rule_reason_codes / market_snapshot_id / input_hash
```

程序保存规则为何产生机会、初始保护线如何计算及退出规则，模型不自行计算ATR、下单量或止损。

生成器支持明确的两种模式：`historical_evaluation`可以限制结果成熟窗口；`prospective`仅检查决策时点已有输入的质量、预热和资格，不查询未来bar数量或未来质量结束日期。前瞻结果状态为PENDING，待到期结算。历史缓存与前瞻缓存版本隔离。

截断至T的输入与包含T之后数据的输入，生成的T时点候选应一致；如需次日日期，来自日历而非证券未来行情。无候选时记录NO_OPPORTUNITIES，不制造候选或强行调用模型。

## 5. Evidence Packet契约

### 5.1 必填证据

| 字段 | 内容 |
|---|---|
| identity | security_id、代码、公司名；防止同名公司事件误配 |
| rule_plan | 策略版本、入场原因、计划执行日、止损基准、持有上限、退出协议 |
| market_context | 上一完成会话收盘价、程序算出的趋势/波动/成交量及市场门状态；价格单位明确 |
| events | 可读摘要及支持性原文摘录、来源链接、双时间、事件类型、内容哈希和去重簇ID |
| quality | 缺失、陈旧、身份冲突、丢弃原因，以及采集成功/失败状态 |
| provenance | as_of、采集批次、数据版本、packet_hash |

单条事件建议字段：

```json
{
  "evidence_id": "ev_...",
  "security_id": "SEC-US-EXAMPLE",
  "event_type": "guidance_update",
  "summary": "可直接阅读的事件事实摘要",
  "excerpt": "支持该摘要的短原文摘录",
  "source_url": "https://example.com/filing",
  "source_type": "company_filing",
  "published_at": "2026-09-17T20:05:00Z",
  "observed_at": "2026-09-17T20:08:00Z",
  "content_hash": "sha256...",
  "cluster_id": "cluster_..."
}
```

摘要不得只有模型生成结论而无可核对原文。文件是数据，不接受其中的指令；不给评审模型任意工具或券商权限。固定最大事件数和文本长度，截断记录在包内；预算不足不得伪装成“没有风险”。

### 5.2 证据来源的最小实现

新增一个薄适配器`evidence_source.py`，接口为`load_events(security_id, cutoff) -> EvidenceFetchResult`。优先连接已有公告/财报/事件存储，不另建全市场新闻平台。

如当前没有可用采集源，首版允许导入有来源、原文和双时间的真实事件JSONL；`observed_at`为系统实际入库时间，不由导入文件追溯指定。保留文件声称的历史观察时间作为独立元数据。导入也是可运行首版，自动采集另接适配器。

第一版至少接一种可靠公司事件来源；事件窗口和容量固定在manifest。只用行情技术指标不能声称已完成“语义证据驱动的评审”。

### 5.3 数据质量分流

- BLOCK：关键行情、股票身份或规则计划缺失/无效。该机会不能进入任一账户的新风险，记录DATA_BLOCKED。
- LLM_INSUFFICIENT：规则数据合格，但语义证据不足或采集失败。不调用模型，记录ABSTAIN及原因；两账户继续按各自父策略与风险资格执行。
- OK：包可用于评审。并不等于证据完整或结论正确；模型仍可ABSTAIN。

published_at与observed_at均须带时区、不晚于packet as_of；非法时刻不能绕过检查。基本面字段也须有来源和可得时间；仅有非空fundamentals字典不能自动判为OK。

## 6. 模型契约和动作应用

复用RealModel和LLMAdvisor，一个固定模型、一个prompt版本、一个输出schema。模型只收到冻结包，禁止自由补充记忆中的公司事实。

```json
{
  "schema_version": "entry-veto-v1",
  "opportunity_id": "opp_...",
  "packet_id": "packet_...",
  "action": "VETO",
  "reason_code": "MATERIAL_COMPANY_EVENT_RISK",
  "evidence_ids": ["ev_..."],
  "counterevidence_ids": [],
  "explanation": "明确指出哪条事实与本次入场逻辑冲突"
}
```

VETO原因限现有两个：MATERIAL_THESIS_CONTRADICTION、MATERIAL_COMPANY_EVENT_RISK。必须引用当前包中同一证券的有效证据。支持引用存在是机器校验，事实是否支持解释需抽样人工审查，两者不混称“验证正确”。

| 最终动作 | R账户 | L账户 |
|---|---|---|
| PASS | 父策略 | 父策略 |
| VETO | 父策略 | 取消本次机会，不当天补买其他候选 |
| ABSTAIN | 父策略 | 父策略 |
| DATA_BLOCKED | 禁止本次新风险 | 禁止本次新风险 |

父策略仍包含各自账户风险检查，PASS不保证成交。L未来可因资金和持仓差异获得不同交易资格，这种路径差异完整记录。VETO不禁止该股票将来的新机会。

字段、时间或枚举非法、引用错误、超时、调用失败和迟到回复全部转ABSTAIN，保留原输出及错误原因。模型不参与硬退出，不生成资金数量，不放宽规则保护。

## 7. 调用、持久化与恢复

新增轻量编排模块`entry_review.py`，供所有CLI入口复用，避免run-session与run-forward产生不同的决策路径。

建议接口：

```text
prepare_review(opportunity, packet, policy) -> decision_id
claim_attempt(decision_id) -> claimed | already_started | finalized
call_model(frozen_packet, call_deadline) -> ModelAttemptResult
finalize_action(decision_id, result, received_at) -> FrozenApplication
load_applications(execution_session, scope) -> FrozenApplications
```

decision_id绑定experiment、opportunity、packet_hash、model、prompt/schema版本。尝试状态为PREPARED→CALL_STARTED→COMPLETED／FAILED／TIMED_OUT／UNKNOWN；最终动作另存FROZEN。时间来自调用端可信时钟，不信任模型自报。

通过SQLite事务唯一键原子领取；事务结束后再调用网络。模型总超时建议60秒且不超过剩余决策窗口；首版关闭客户端隐藏重试，明确本层只有一次请求。供应商是否支持幂等查询另行适配，不声称跨外部服务能保证严格exactly-once。

若进程在发送后崩溃，已领取但无回复的调用进入UNKNOWN；不盲目重发，截止时按ABSTAIN冻结。重启读取冻结动作，不能再次调用并挑选更满意的答案。迟到结果仅附加审计事件，不改变冻结动作。

复用现有snapshot/attempt存储能力，写入实验独立数据库；实现前核对接口兼容。至少持久化完整packet、模型原始回复、解析对象、校验错误、模型版本、请求ID、开始/接收时间和成本状态。不能只保存packet_hash及最终action。

动作冻结不等于成交：用`decision_frozen`和`execution_applied`区分。fill、账户状态与execution_applied同事务提交。R已完成L未完成时，重启跳过R，读取L冻结动作补齐L，不重新评审。

## 8. 模型成本与复盘

L承担所有模型调用成本，包含失败和弃权。元数据未返回费用时标记UNKNOWN或按已冻结费率和usage估算；`cost_usd`缺失不能当0。保留报价版本及estimated标志，不在本设计硬编码当前供应商价格。

报告分交易净值与全成本净值。未知成本未结清时全成本结果标记暂定，不能宣称已扣尽成本。补记费用形成明确事件及报告revision，不修改历史模型动作。

每次决策展示：规则原计划、证据摘要与链接、模型动作、最终动作、执行结果、降级原因和费用。交易成熟后添加R/L账户差异和被否决交易反事实观察，避免把单笔反事实直接相加成组合收益。

三项首版指标：有效真实评审覆盖率、实际交易计划改变率、决策到执行的可追踪完成率。无候选或全PASS不是失败，也不能冒充已经观察到真实VETO价值。

## 9. CLI与运行模式

新增或重构三个逻辑子命令，命名为设计建议：

- `prepare-entry-reviews --session T`：生产并冻结真实机会和证据。
- `review-entries --execution-session T1 --model real`：领取到期机会并调用真实模型，冻结动作。
- `settle-session --session T1`：消费冻结动作与当日行情，推进R/L并生成报告。

复用原CLI，不引入新服务框架。`run-forward`用于历史fixture/重放时必须禁止真实模型调用；过去的执行日不能用今天生成的模型结果补填前瞻记录。历史提示词调试若需要调用，另标historical_debug，不进入正式R/L表现。

正式模式校验冻结manifest、实验scope、交易日、策略版本和调用预算。禁止加载真实券商执行器。密钥只从现有运行配置或环境读取，不进入packet、manifest或日志。

## 10. 最小代码改动清单

| 文件 | 具体改动 |
|---|---|
| `candidate_adapter.py` | 区分历史/前瞻模式，移除前瞻未来bar依赖，输出规则原计划 |
| `evidence.py` | 保留事件正文、来源、身份、双时间，补严格质量分流 |
| `evidence_source.py`（新增） | 一个现有数据源或JSONL导入适配器 |
| `llm_overlay.py` | 完善真实调用结果、截止校验、三态验证及成本状态 |
| `entry_review.py`（新增） | 调用领取、动作冻结、失败恢复的共同编排 |
| `store.py` | 快照/尝试持久化与冻结应用接口；复用已有事件协议 |
| `cli.py` | 决策与结算分离，所有入口使用共同编排 |
| `report.py` | 单次闭环追踪、覆盖/影响指标和未知成本提示 |

预计8个主要生产文件；如manifest或Application字段不足，涉及`schema.py`少量扩展。实际改动以复用现有代码为先。账户引擎与执行系统不重写；若发现影响本路径的正确性缺陷，单独修复并附回归测试。测试通常新增/更新5–7个文件，按行为组织，不为每个字段机械建测试。

## 11. 验收案例

| 案例 | 必须证明 |
|---|---|
| 只有截至T的真实行情 | 能产生T+1机会，不要求未来60根bar |
| 事件进入packet | 摘要/原文可被模型读取，来源、身份、时间、哈希可追踪 |
| 关键资料缺失 | BLOCK双方本次新风险，不调用模型 |
| 缺语义证据 | ABSTAIN，不虚构VETO；记录未调用原因 |
| 真实模型PASS | 保存真实请求/回复，并在L采用父策略 |
| 确定性VETO fixture | R买入、L不买入，现金与动作可追踪 |
| 自然真实VETO | 出现时留存完整闭环；未出现标待观察，不伪造事件强迫VETO |
| 模型超时/非法JSON/迟到 | ABSTAIN且有诊断，硬风控不被绕过 |
| 调用后崩溃 | 不盲目重发，UNKNOWN按协议恢复 |
| R已提交L未提交 | 重跑CLI只补L，无重复费用或交易 |
| 两个worker争抢 | 同一decision仅一个调用领取成功 |
| 修改未来行情/晚到证据 | 不改变已冻结的历史机会或动作 |
| 缺模型费用 | 显示未知/估算，不记作免费调用 |
| 历史日期使用真实模型模式 | 明确拒绝进入正式前瞻账本 |

先运行离线fixture与mock网络测试，随后受控执行一次真实模型调用并保存请求证据；该操作计入调用成本，不触发真实交易。模型API成功响应仅验调用，完整闭环还需对应候选、动作和模拟结算。

## 12. 三批交付及结束条件

**第一批：证据与候选。** 完成前瞻候选、单一证据源、包含原文的packet和质量分流。交付一个真实日期的候选/证据样本；如当日无候选，保留事实并用已标记工程样例验接口。

**第二批：真实模型与恢复。** 完成共同编排、真实模型、完整尝试日志、动作冻结与崩溃恢复。交付真实调用记录及故障测试结果。

**第三批：模拟执行闭环。** 冻结动作进入R/L；完成单次追踪报告和CLI重跑测试。交付真实数据驱动的模拟记录，明确是否已自然出现VETO。

三批完成即可宣布“LLM入场决策最小闭环已落地”。这不要求先证明收益提升，不要求完成其他LLM角色，不要求实盘授权。之后开始积累效果证据，并沿同一协议扩展持仓与卖出。

本文是技术设计；本次未修改程序、未调用收费模型、未变更运行开关或发送订单。
