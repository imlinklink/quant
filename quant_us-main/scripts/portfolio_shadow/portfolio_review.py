"""Portfolio 的调用方：影子实验里的容量分配评审（设计 §6.3）。

**为什么不在 `settle-session` 里调用模型**：那条命令的性质是"没有模型"，§9 靠它保证
"看到当天结果后补作决策在结构上不可能"。因此 Portfolio 走与入场相同的三段式：

    prepare-portfolio-review --session T     T 收盘冻结候选、持仓、限额与全部合法模板
    review-portfolio --execution-session T1  唯一调模型处（T+1 开盘前）
    settle-session --session T1              消费冻结的分配，据此筛选 L 侧 intents

**默认关闭**（`llm_decision.portfolio_review.enabled: false`）。即便开启，Portfolio 的
权限等级是 `shadow` ⇒ 引擎的 `effective_action` 是父策略 `keep_rule_allocation` ⇒
应用于 L 的分配就是规则分配本身，**结果与不开启逐字节相同**（由测试钉死）。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# 限额里 manifest **未声明**、因此不参与分配控制的项。写在包里让它可见，
# 而不是用一个编出来的数把检查"看起来做过了"。
UNAVAILABLE_LIMIT_REASONS = {
    'max_group_risk_bp': 'manifest.risk_policy 未声明风险组上限；'
                         '§6.3 要求的「风险组上限」在本实验里无法执行（不是没触发，是没声明）',
}


def limits_from_manifest(manifest, *, state) -> Tuple[dict, List[str]]:
    """从冻结 manifest + 账户状态导出限额。

    **只声明了两项**：`max_positions` 与 `single_position_risk_bp`（单票上限）。
    总风险预算**未声明**，这里用「每仓都用满单笔预算」作为上界 —— 这是**推导**不是声明，
    必须与真正的声明区分开，所以一并返回不可用清单。
    """
    policy = manifest.risk_policy or {}
    base_bp = int(policy['single_position_risk_bp'])
    max_positions = int(policy['max_positions'])
    limits = {
        'max_positions': max_positions,
        'max_name_risk_bp': base_bp,
        'max_total_risk_bp': max_positions * base_bp,
        'cash_available_micro': int(state.cash_available + state.unsettled_cash),
    }
    return limits, sorted(UNAVAILABLE_LIMIT_REASONS)


def portfolio_ranks(store, session: str) -> Dict[str, int]:
    """取该 session 冻结的 Selection 模型排序（`portfolio_rank`）。

    从 `selection_counterfactual_frozen` 事件读 —— **不重算**。重算会得到与当时不同的
    值（信号、复权、参数都可能已变），那等于用今天的信息改写当时的依据。
    找不到就返回空字典：此时不生成模型排序模板（宁可少一个模板，也不生成一个挂着
    "模型排序"之名、实际按代码排序的模板）。
    """
    rows = store.raw_events('selection_counterfactual_frozen')
    best: Dict[str, int] = {}
    for payload in rows:
        if str(payload.get('as_of') or '')[:10] != session:
            continue
        for item in payload.get('ranked') or []:
            rank = item.get('portfolio_rank')
            code = item.get('code')
            if code and isinstance(rank, int) and rank > 0:
                best[str(code)] = rank
    return best


def candidates_for(store, manifest, *, execution_session: str, bars: dict,
                   state) -> List[dict]:
    """把 T+1 到期的机会转成 Portfolio 的候选。

    `risk_bp` 取单票风险预算（引擎的实际定仓口径）；`estimated_cost_micro` 用
    **T 收盘价**估算 —— 这是计划期的近似，真实成交价是 T+1 开盘，引擎届时会重新定仓。
    """
    from .paper_engine import initial_stop_micro, risk_sized_shares_micro
    # `store.opportunities()` 返回 dict（`_asdict` 过的），不是 Opportunity 对象
    ranks: Dict[str, int] = {}
    for session in {o.get('planned_execution_session')
                    for o in store.opportunities()} - {None}:
        ranks.update(portfolio_ranks(store, session))
    policy = manifest.risk_policy or {}
    base_bp = int(policy['single_position_risk_bp'])
    max_weight_bp = int(policy['max_weight_bp'])
    # 缺行情时用持仓成本价兜底：`AccountState.equity()` 对缺失标的会直接 KeyError，
    # 而候选生成不该因为某个持仓当日无行情就整体失败（引擎会在阶段 6 记 PROVISIONAL）。
    marks = {sid: bars[sid]['close'] for sid in state.positions if sid in bars}
    for sid, pos in state.positions.items():
        marks.setdefault(sid, pos.entry_price_micro)
    nav = state.equity(marks)
    out = []
    for opp in store.opportunities():
        if opp.get('planned_execution_session') != execution_session:
            continue
        code = str(opp.get('security_id') or '')
        bar = bars.get(code)
        if bar is None:
            continue
        stop_ref = opp.get('stop_reference') or {}
        stop = int(stop_ref.get('initial_stop_micro')
                   or initial_stop_micro(bar['close'], int(stop_ref.get('atr14_micro') or 0)))
        shares = risk_sized_shares_micro(bar['close'], stop, nav, state.cash_available,
                                         risk_bp=base_bp, max_weight_bp=max_weight_bp,
                                         fee_bp=10)
        candidate = {
            'security_id': code,
            'rank': int(opp.get('rank') or 0),
            'risk_bp': base_bp,
            'risk_group': str(opp.get('risk_group') or ''),
            'estimated_cost_micro': shares * bar['close'],
        }
        if code in ranks:
            candidate['portfolio_rank'] = ranks[code]
        out.append(candidate)
    return sorted(out, key=lambda c: (c['rank'], c['security_id']))


def position_exposure(state, bars: dict) -> List[dict]:
    """按当前价格至保护线的损失计算持仓风险，向上取整到基点。"""
    marks = {sid: (bars.get(sid) or {}).get('close', pos.entry_price_micro)
             for sid, pos in state.positions.items()}
    nav = state.equity(marks)
    if nav <= 0:
        raise ValueError('PORTFOLIO_NONPOSITIVE_EQUITY')
    return [{'security_id': sid,
             'risk_bp': (max(0, marks[sid] - pos.stop_micro) * pos.shares * 10000
                         + nav - 1) // nav,
             'risk_group': ''} for sid, pos in sorted(state.positions.items())]


def subject_key(execution_session: str) -> str:
    return f'portfolio@{execution_session}'


def prepare(store, manifest, *, session: str, execution_session: str, bars: dict,
            state) -> dict:
    """冻结 T 的 Portfolio 包（首次写入即冻结，重跑原样复用）。"""
    from .evidence import market_close
    from scripts.live_trading.decision_bridge import build_portfolio_packet
    key = subject_key(execution_session)
    packet = store.packet_for_opportunity(key)
    if packet is not None:
        return packet
    limits, unavailable = limits_from_manifest(manifest, state=state)
    candidates = candidates_for(store, manifest, execution_session=execution_session,
                                bars=bars, state=state)
    packet = build_portfolio_packet(
        candidates=candidates, positions=position_exposure(state, bars), limits=limits,
        new_evidence=[], account_scope=manifest.account_scopes[0],
        subject_id=f'portfolio:{session}', as_of=market_close(session).isoformat(),
        identity={'risk_group': ''}, subject_code='',
        versions={'packet_schema': 'portfolio-v1', 'prompt': 'portfolio-v1',
                  'output_schema': 'portfolio-v1', 'feature': 'feature-v2',
                  'rule': 'rule-v2', 'permission': 'permission-v2'})
    packet['limits_unavailable'] = {name: UNAVAILABLE_LIMIT_REASONS[name]
                                    for name in unavailable}
    packet['limits_derived'] = {
        'max_total_risk_bp': '推导值 = max_positions × single_position_risk_bp'
                             '（manifest 未声明总风险预算）'}
    packet['subject_key'] = key
    store.put_packet(key, packet)
    return packet


def frozen_allocation(store, scope: str, execution_session: str) -> Optional[set]:
    """该执行日已冻结的分配（证券集合）。没有冻结动作时返回 None。

    `Application` 没有 `action_template_id` 字段，而 Portfolio 的模板 id 与动作名
    **按构造同名**（`{'template_id': 'keep_rule_allocation', 'action': 'keep_rule_allocation'}`），
    故用 `action` 查模板。若将来两者分家，这里必须改成显式记录模板 id。
    """
    packet = store.packet_for_opportunity(subject_key(execution_session))
    if packet is None:
        return None
    application = store.application(scope, subject_key(execution_session))
    if not application:
        return None
    from mutifactor.llm.contracts.portfolio_v1 import apply_portfolio_choice
    plan = apply_portfolio_choice({'chosen_template_id': application.get('action')}, packet)
    return {a['security_id'] for a in plan['allocations']}


def filter_intents(store, scope: str, execution_session: str,
                   intents: list) -> Tuple[list, list]:
    """按已冻结的分配筛选 L 侧 intents；返回 (保留, 被剔除)。

    **没有冻结分配时原样放行**：Portfolio 没被咨询（未启用、无容量冲突、或窗口错过）
    不该悄悄改变 L 的行为 —— 那会把"没跑"表现成"模型选择少买"。
    """
    allocation = frozen_allocation(store, scope, execution_session)
    if allocation is None:
        return list(intents), []
    kept = [o for o in intents if o.security_id in allocation]
    dropped = [o for o in intents if o.security_id not in allocation]
    return kept, dropped
