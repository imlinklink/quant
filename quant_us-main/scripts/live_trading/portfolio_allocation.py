"""实盘层的 Portfolio 调用方（设计 §6.3）：在若干已合格候选间分配有限容量。

**为什么直接构造 `DecisionEngine` 而不走 `DecisionRuntime`**：与
`scripts/live_trading/protocol_review.py` 同一先例 —— 新增角色不扩 `DecisionRuntime.ROLES`
（那是 selection/entry/position 的路由门，动它只扩大风险面）。

**接通后实盘行为一字不变**：Portfolio 权限是 `shadow` ⇒ `effective_action` 恒为父策略
`keep_rule_allocation`（`mutifactor/llm/validators/action.py` 的 shadow 分支），只额外记
`shadow_difference='llm_would_<action>'`。本模块**不改变任何提案状态、不下单**。

**为什么在提案评审时触发**：在途提案对 `risk_quantity` **不可见**（`pending` 不是订单，
`execution.py` 只对 `book['orders']` 里 ACTIVE 的**订单**预留），所以并发的待审提案互相看不见、
各自独立通过定仓；真正的容量约束要到 `execution.py` 提交时以**拒绝**形式出现，而那里是
一次一条、凑不出批量。**提案评审时是唯一能看到竞争全貌的位置。**

**付费调用有界**：仅在确有容量冲突（`consult_required`）时才认领，且同账户**每天最多一次**
（复用 `ReviewScheduler.claim_daily_job`）。没有冲突时既不动模型也不消耗当天认领。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from scripts.live_trading.approval.proposal_store import ACTIVE_STATUSES

MICRO = 1_000_000
JOB_TYPE = 'portfolio_allocation'

# 候选口径的**披露文本**：§6.3 要求候选是"Selection 与 Entry 的有效输出"，而实盘层的
# Entry 评审是逐条异步的、提审时点未必都有结论。这里退一步用"已算出交易计划的在途买入提案"，
# 比 §6.3 宽 —— 必须写进包里让读的人看得见，不得当成"已通过 Entry"。
CANDIDATE_CRITERION = ('在途买入提案中已有交易计划（plan_id）者；'
                       '**未经** Entry 有效输出过滤（实盘 Entry 评审逐条异步，提审时点未必有结论）')


def active_buy_proposals(store) -> List[Dict[str, Any]]:
    """在途**买入**提案。

    **必须过滤 `side`**：卖单与买单共用同一个 `ProposalStore`
    （`chandelier_exit_manager` 的退出提案也在里面），而卖单**释放**容量、不是竞争者。
    直接用 `active_count()` 会把卖单算成候选 —— 那个方法不分方向。
    """
    return [p for p in store.get_all()
            if p.get('side') == 'buy' and p.get('status') in ACTIVE_STATUSES]


def candidates_from_proposals(proposals, *, code_groups: Dict[str, str],
                              per_trade_bp: int) -> List[Dict[str, Any]]:
    """在途买入提案 → Portfolio 候选。

    `rank` 取 `created_at` 升序 —— 这是**规则基线顺序**，不得表述为"规则排名"：
    实盘提案不携带 `portfolio_rank`（那只存在于冻结的 Selection 反事实里），
    所以 `select_ranked_subset` 模板在本层不会生成。那是**正确**的：没有模型排序就不该
    生成一个挂着那个名字的模板。
    """
    ordered = sorted(proposals,
                     key=lambda p: (p.get('created_at') or 0, str(p.get('id') or '')))
    out: List[Dict[str, Any]] = []
    for rank, p in enumerate(ordered, start=1):
        code = str(p.get('stock_code') or '')
        if not code:
            continue
        qty = float(p.get('quantity') or 0)
        price = float(p.get('price') or 0)
        out.append({'security_id': code,
                    'rank': rank,
                    'risk_bp': int(per_trade_bp),
                    'risk_group': str(code_groups.get(code) or ''),
                    'estimated_cost_micro': int(round(qty * price * MICRO))})
    return out


def evidence_from_proposals(proposals) -> List[Dict[str, Any]]:
    """把竞逐提案携带的证据并成包内证据（去重，按 `evidence_id`）。

    **不是可选的**：`validate_portfolio_v1` 要求**改变分配**的动作至少引用一条证据
    （§7.2"改变仓位须有证据"）。包内没有证据时，模型**只能**选 `keep_rule_allocation` ——
    任何别的选择都会被判 `failed`（不是 `validated`），于是该角色的 `output_validity`
    是 0 而不是 1。提案携带的证据是实盘层本来就有的东西（`workflow.news_evidence` 的产物）。
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for p in proposals:
        for item in p.get('evidence_items') or []:
            eid = item.get('evidence_id')
            if not eid or eid in seen:
                continue
            seen.add(eid)
            out.append(dict(item))
    return out


def occupied_slots(book: Dict[str, Any], *, code_groups: Dict[str, str],
                   per_trade_bp: int) -> Tuple[List[Dict[str, Any]], int]:
    """当前**占用**的仓位格：持仓 ∪ 在途买单，各占一格（与 `execution.py` 提交时的
    `occupied = 持仓 ∪ ACTIVE 买单` 同口径）。返回 `(slots, estimated)`。

    风险取值：`initial_risk` 优先；**取不到就用 `per_trade_bp` 估**，并把用了几格作为第二个
    返回值披露出来。

    为什么不用 0：实盘层自己在 `risk_preview` 里对缺 `initial_risk` 的持仓按 0 计，但那是
    *定仓* 的口径；这里是 *容量与预算* 的口径，**低估计**会让 `consult_required` 该真时假 ——
    漏掉一次该做的评审。宁可高估（多问一次模型）也不要漏，且把估计过的格数写明。
    在途买单更是永远没有 `initial_risk`，按 0 计等于假装它不占风险。
    """
    from scripts.live_trading.execution import ACTIVE      # 延迟导入：execution ↔ workflow 有环
    slots: List[Dict[str, Any]] = []
    estimated = 0

    def add(code: str, risk, group) -> None:
        nonlocal estimated
        if risk in (None, ''):
            estimated += 1
            slots.append({'security_id': code, 'risk_bp': int(per_trade_bp),
                          'risk_group': group, 'risk_estimated': True})
            return
        try:
            bp = max(0, int(risk))
        except (TypeError, ValueError):
            estimated += 1
            bp = int(per_trade_bp)
        slots.append({'security_id': code, 'risk_bp': bp, 'risk_group': group})

    for code, pos in (book.get('positions') or {}).items():
        add(str(code), pos.get('initial_risk'),
            str(pos.get('risk_group') or code_groups.get(str(code)) or ''))
    held = {s['security_id'] for s in slots}
    for order in (book.get('orders') or {}).values():
        if order.get('side') != 'buy' or order.get('status') not in ACTIVE:
            continue
        code = str(order.get('code') or '')
        if not code or code in held:
            continue          # 已按持仓计过，别重复占格
        add(code, None, str(code_groups.get(code) or ''))
    return slots, estimated


def limits_from_config(config: Dict[str, Any], *, cash: float) -> Tuple[dict, dict]:
    """`config.yaml` 的 `risk_budget` → §6.3 的限额口径。返回 `(limits, unavailable)`。

    **组上限传字典而不是标量**：实盘层是**按组**的（`risk_budget.group_limits`：semis / china /
    speculative / space 各不相同）。传一个标量等于拿某一组的限额去管所有组，会算错。
    """
    cfg = ((config or {}).get('risk_budget') or {})
    per_trade_bp = _bp_of(cfg.get('per_trade'))
    limits: Dict[str, Any] = {
        'max_positions': int(cfg.get('max_positions') or 0),
        'max_name_risk_bp': per_trade_bp,
        'max_total_risk_bp': _bp_of(cfg.get('total')),
        'cash_available_micro': int(round(float(cash or 0) * MICRO)),
    }
    group_limits = {str(g): _bp_of(v) for g, v in (cfg.get('group_limits') or {}).items()}
    unavailable: Dict[str, str] = {}
    if group_limits:
        limits['max_group_risk_bp'] = group_limits
    else:
        unavailable['max_group_risk_bp'] = ('risk_budget.group_limits 未声明；'
                                            '§6.3 的「风险组上限」在本配置下无法执行')
    return limits, unavailable


def _bp_of(fraction) -> int:
    """比例 → 基点（0.0025 → 25）。"""
    try:
        return int(round(float(fraction) * 10_000))
    except (TypeError, ValueError):
        return 0


def consult(registry, store, config: Dict[str, Any], *, equity: float, cash: float,
            advisor=None, call_model=None, as_of: str, session: str,
            market_session: str = 'regular') -> Dict[str, Any]:
    """建包 → (有冲突才) 认领 → 调模型。返回可审计的摘要。

    `registry` 既是账户作用域来源（`registry.namespace`），也是引擎要的那个注册表；
    `store` 是 `ProposalStore`（候选来源）。
    """
    from scripts.live_trading.decision_bridge import build_portfolio_packet
    from scripts.live_trading.decision_engine import DecisionEngine
    from scripts.live_trading.review_scheduler import ReviewScheduler

    cfg = ((config or {}).get('risk_budget') or {})
    code_groups = {str(k): str(v) for k, v in (cfg.get('code_groups') or {}).items()}
    limits, unavailable = limits_from_config(config, cash=cash)
    with registry.transaction() as book:
        positions, estimated = occupied_slots(book, code_groups=code_groups,
                                              per_trade_bp=limits['max_name_risk_bp'])
    proposals = active_buy_proposals(store)
    candidates = candidates_from_proposals(proposals, code_groups=code_groups,
                                           per_trade_bp=limits['max_name_risk_bp'])
    new_evidence = evidence_from_proposals(proposals)

    packet = build_portfolio_packet(
        candidates=candidates, positions=positions, limits=limits,
        new_evidence=new_evidence,
        account_scope=registry.namespace, subject_id=f'portfolio:{session}', as_of=as_of,
        market_session=market_session)
    packet['limits_unavailable'] = dict(unavailable)
    packet['candidate_criterion'] = CANDIDATE_CRITERION
    packet['risk_estimated_slots'] = int(estimated)
    packet['equity_usd'] = float(equity or 0)

    summary: Dict[str, Any] = {'session': session, 'candidates': len(candidates),
                               'occupied': len(positions),
                               'evidence': len(new_evidence),
                               'risk_estimated_slots': int(estimated),
                               'consult_required': bool(packet['consult_required']),
                               'limits_unavailable': sorted(unavailable),
                               'called': False}
    if not packet['consult_required']:
        # §6.3：没有容量冲突时不调用。**也不消耗当天认领** —— 冲突晚些才出现时还能再评。
        summary['skipped'] = 'NO_CAPACITY_CONFLICT'
        return summary

    if not ReviewScheduler(registry, config).claim_daily_job(JOB_TYPE, session):
        # 同账户每天最多一次付费调用。候选集变化会改变 packet_id ⇒ 引擎那边本就幂等，
        # 但"集合每天变几次就调几次"不是我们想要的形态，故在认领处收口。
        summary['skipped'] = 'ALREADY_CONSULTED_TODAY'
        return summary

    engine = DecisionEngine(registry, advisor=advisor, config=config, call_model=call_model)
    result = engine.decide_portfolio(packet)
    summary.update({
        'called': True,
        'decision_id': result.decision_id,
        'status': result.status,
        'model_action': result.model_action,
        'effective_action': result.effective_action,
        'permission_level': result.permission_level,
        'validation_errors': list(result.validation_errors or ()),
    })
    return summary
