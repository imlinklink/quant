# LLM 持仓角色（L1）：技术包与限定动作集 —— 机制已交付、前向证据仍未开始

日期：2026-09-21。上位规划 `docs/next-system-implementation-plan-2026-09-21.md` §6（第三批 L1）。
**这不是收益结果** —— 它是「让这个角色能存在并被验证」的那一步（§6.3 要求先用合成输入验证执行链）。

## 一、本批改了什么

| 项 | 内容 |
|---|---|
| 角色限定（§6.1） | 只开放 **`hold` / `exit`**（外加弃权）。`reduce` / `tighten_protection` / `post_exit_review` 既不提供模板，也被显式拒绝。硬止损不由模型决定，不可取消或放宽。 |
| 技术证据包（§6.2） | `scripts/strategy_research/technical_packet.py`：程序计算 **22 项**事实（市价/收盘/MA20/MA50/MA200/MA200 斜率/252 日高点/回撤/ATR/量比/持有 session/浮盈 USD 与 R/保护距离 USD·比例·ATR 倍数/风险状态/账户回撤/预算/现金占比/暴露），每项带**单位、算法版本、as_of、available_at、来源、缺失原因**。 |
| 充分性标准（§6.2） | `sufficiency()`：**必需项**（10 项）任一缺失 ⇒ `LLM_INSUFFICIENT`（不调用模型、零成本）；缺**可选项**不拦。它与新闻质量门是两件事：这里判「程序能不能把持仓状态说清楚」。 |
| 独立权限配置（§6.3） | 新 manifest 键 `llm_policy.technical_packet` + `open_actions`，**两者必须同时出现**（只开一个就 fail-closed），且要求 `position_overlay=position_action`。默认不设 ⇒ 生产默认 shadow 权限一字未动。 |

## 二、把角色救活的那条依赖（本批最重要的发现）

`validate_position_v2` 对 `reduce`/`exit` 有一条硬要求：**至少引用一条证据**
（`position_v2.py:276-280`）。而本项目刻意不接公司事件源、市场日报又是唯一证据供给 ⇒
**在没有技术事实可引用时，EXIT 这个动作在结构上不可能通过校验**，L 只能恒等于 R。

技术包把这件事解开了，而且是**构造性**的：技术事实按 `subject_code = 本持仓证券` 生成、
`published_at = available_at = 该值可见的时刻`（≤ 评审 as_of），渲染成可**逐字引用**的一行
（`atr14_frac=0.0417 fraction`）。于是：
- 模型的 `facts` 能引用本证券的技术事实 ⇒ 通过归属隔离与时间检查；
- 归属不再是「只有 MARKET」，`reduce`/`exit` 不再被结构挡住。

这一点由测试钉死：`test_exit_is_structurally_impossible_without_citable_evidence`
（去掉技术条目后同一个 exit 输出立刻被拒）。

## 三、已用合成输入验证的链（§6.3 第一步）

`tests/unit/strategy_research/test_position_chain.py`（10 条）：

1. **零新闻也开门**：技术包齐备时 `data_quality.level == 'OK'`、`news_events == 0`、
   `gate(packet) is None` —— 模型确实会被调用（此前必为 `LLM_INSUFFICIENT`）。
2. **只提供开放动作**：包内 `allowed_action_set == ['exit','hold']`，模板也只有这两类。
3. **技术证据驱动的 EXIT 变成改变路径的动作**：`decide_position_overlay` 返回 `POSITION_EXIT`。
4. **集合外动作降级**：`post_exit_review` ⇒ `INVALID_OUTPUT` + `POSITION_ABSTAIN`
   （它不要求选模板，「模板里没有」拦不住它，必须显式判）。
5. **不给技术包时逐字段不变**：门仍按新闻判、包内不多任何键、动作不收窄。
6. **前视有两道闸**：CLI 切片按 session 截断 **且** `build_facts` 内部再过滤一次。
   两道分别注入缺陷验过（去掉任一道，测试失败）。

另修一处潜伏缺陷：`FakePositionModel.call` 在 `evidence_from_packet=False` 时
`summary` 未绑定就使用（`UnboundLocalError`）—— 现有调用方总传 True 才没暴露。

## 四、**还没有的东西（必须说清）**

- **没有任何 L−R 贡献数字。** 规划 §6.3 的第二步是「用真实前向数据与真实模型验证贡献」，
  本批只做了第一步。当前 `path_changed` / `applications_written` / L−R 净值差**仍然是零**，
  因为从未在真实窗口上跑过一次带技术包的真实模型评审。
- 为什么不能拿历史补：历史 LLM 调用含**模型知识穿越**，§6.3 明写「不能作为正式收益证据」。
  资格判断只能来自「实时冻结证据 + 决策在执行前生成」的前向记录。
- 也**没有**做：promotion/权限变更（默认全 shadow 不动）、买入/加仓/自由仓位（§6.1 明确不同时开放）。

## 五、下一步（L1 的第二步）

1. 新建一个 study 身份的 experiment（`llm_policy` 一变就必须新建 id），
   `technical_packet: true` + `open_actions: [hold, exit]` + `use_real_model: true`；
2. 在**实时窗口**跑 `prepare-position-reviews` → `review-positions --model real` → `settle-session`；
3. 按 §6.3 验收：有效评审覆盖、可追踪完成率、实际应用、路径变化、未应用原因、模型费用、
   L−R 终值与风险差。**零个路径变化可以是自然结果，不能为了展示作用强迫交易**。

## 六、复现

```bash
cd /Users/wh1817w/quant-research/quant_us-main
/usr/bin/python3 -m pytest tests/unit/strategy_research/test_position_chain.py \
    tests/unit/strategy_research/test_technical_packet.py -q      # 29 条
```

全量测试 **1583 passed**。
