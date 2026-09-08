"""只读决策健康摘要（Decision observability, task A）。

纯函数，不触发 LLM 请求、不批准、不下单；只从事件账本和提案状态推导。
- 区分 LLM「配置启用 / 尚未调用 / 成功 / 失败 / 资料不足」；
- 给出不可批准原因的稳定原因码；
- 汇总规则通过/拒绝、提案、评估中与到期时间。

扫描时间必须来自实际扫描完成的心跳（scan_heartbeat 事件），
重复信号去重后的事件时间不能冒充最新扫描时间。
"""
import time
from datetime import datetime, timezone

# 不可批准原因码 → 中文说明（前端直接映射）。
REASON_LABELS = {
    'review_pending': '等待大模型评估',
    'review_failed': '评估失败',
    'insufficient_information': '资料不足',
    'review_expired': '评估已过期',
    'plan_changed': '计划已修订，需重新评估',
    'proposal_expired': '提案已过期',
    'llm_disabled': 'LLM 未启用',
}


def _iso(value):
    """把 event observed_at（ISO 字符串）归一化为可比较的 UTC ISO 字符串。"""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    s = str(value).replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return s


def _latest(events, predicate):
    best = None
    for e in events:
        if not predicate(e):
            continue
        if best is None or _iso(e.get('observed_at')) > _iso(best.get('observed_at')):
            best = e
    return best


def unapprovable_reason(item, now=None):
    """返回 (reason_code, 中文说明)；可批准时返回 (None, '')。

    与 ProposalStore.llm_ready 语义对齐：llm_ready=True 时返回 (None, '')。
    不修改 item，不写事件。
    """
    now = now if now is not None else time.time()

    # 提案已过期：最优先，且与状态机「pending/approved 超时即 expired」一致。
    expires_at = item.get('expires_at')
    if expires_at and float(expires_at) and now >= float(expires_at):
        return 'proposal_expired', REASON_LABELS['proposal_expired']

    review = item.get('llm') or {}

    # 旧版提案（无结构化计划）：只有「尚未评估 / 评估失败」两类阻塞。
    if not item.get('plan_id'):
        if not review:
            return 'review_pending', REASON_LABELS['review_pending']
        if review.get('error'):
            return 'review_failed', REASON_LABELS['review_failed'] + f"（{review.get('error')}）"
        return None, ''

    # 结构化计划流程（trade-review-v1）。
    if not review:
        return 'review_pending', REASON_LABELS['review_pending']

    status = review.get('status')
    if status == 'failed':
        suffix = f"（{review.get('error')}）" if review.get('error') else ''
        return 'review_failed', REASON_LABELS['review_failed'] + suffix
    if status == 'insufficient_information':
        gaps = review.get('missing_information') or []
        suffix = '：' + '；'.join(str(g) for g in gaps) if gaps else ''
        return 'insufficient_information', REASON_LABELS['insufficient_information'] + suffix
    if status == 'stale' or (review.get('expires_at') and now >= float(review['expires_at'])):
        return 'review_expired', REASON_LABELS['review_expired']
    if review.get('plan_change_requested'):
        return 'plan_changed', REASON_LABELS['plan_changed']
    # 计划修订后旧评估回调：review 绑定版本与当前计划版本不一致 → 旧评估不得恢复批准资格。
    if (review.get('review_id') != item.get('review_id')
            or review.get('plan_version') != item.get('plan_version')
            or review.get('plan_id') != item.get('plan_id')
            or review.get('input_snapshot_id') != item.get('input_snapshot_id')):
        return 'plan_changed', REASON_LABELS['plan_changed']

    return None, ''


def llm_state(events, llm_enabled):
    """从事件账本推导 LLM 状态。区分「尚未调用 / 等待完成 / 成功 / 失败 / 资料不足」。"""
    requested = [e for e in events if e['event_type'] == 'llm_requested']
    completed = [e for e in events if e['event_type'] == 'llm_completed']
    failed = [e for e in events if e['event_type'] == 'llm_failed']

    last_request = _latest(requested, lambda e: True)
    last_success = _latest(completed, lambda e: e.get('payload', {}).get('status') == 'complete')
    last_insufficient = _latest(completed, lambda e: e.get('payload', {}).get('status') == 'insufficient_information')
    last_failure = _latest(failed, lambda e: True)

    status = 'never_called'
    if last_request is not None:
        status = 'pending'
        candidates = [e for e in (last_success, last_insufficient, last_failure) if e is not None]
        if candidates:
            latest = max(candidates, key=lambda e: _iso(e.get('observed_at')))
            status = ('failed' if latest is last_failure
                      else 'insufficient_information' if latest is last_insufficient
                      else 'success')

    def _at(e):
        return _iso(e.get('observed_at')) if e is not None else None

    failure_reason = None
    if last_failure is not None:
        p = last_failure.get('payload') or {}
        failure_reason = p.get('error') or p.get('status') or '未知错误'

    return {
        'enabled': bool(llm_enabled),
        'status': status,
        'last_request_at': _at(last_request),
        'last_success_at': _at(last_success),
        'last_failure_at': _at(last_failure),
        'last_failure_reason': failure_reason,
        'request_count': len(requested),
        'success_count': sum(1 for e in completed if e.get('payload', {}).get('status') == 'complete'),
        'insufficient_count': sum(1 for e in completed if e.get('payload', {}).get('status') == 'insufficient_information'),
        'failure_count': len(failed),
    }


def build_health(events, llm_enabled, proposals=None, scope=None, now=None):
    """汇总只读健康摘要。events 须已按账户作用域过滤（调用方负责）。"""
    now = now if now is not None else time.time()
    candidates = [e for e in events if e['event_type'] in ('rule_candidate', 'rule_rejected')]
    passed = [e for e in candidates if e['event_type'] == 'rule_candidate']
    rejected = [e for e in candidates if e['event_type'] == 'rule_rejected']

    last_signal = _latest(candidates, lambda e: True)
    heartbeat = _latest(events, lambda e: e['event_type'] == 'scan_heartbeat')

    props = proposals or []
    pending = [p for p in props if p.get('status') in ('pending', 'approved')]
    in_review = [p for p in pending
                 if p.get('review_requested_at') and not (p.get('llm') or {})]
    expiries = [float(p['expires_at']) for p in pending if p.get('expires_at')]

    scope = scope or next((e.get('account_scope') for e in events if e.get('account_scope')), None)

    return {
        'account_scope': scope,
        'llm': llm_state(events, llm_enabled),
        'scan': {
            'last_heartbeat_at': _iso(heartbeat.get('observed_at')) if heartbeat else None,
            'last_signal_at': _iso(last_signal.get('observed_at')) if last_signal else None,
            'has_signal': bool(candidates),
            'rule_passed': len(passed),
            'rule_rejected': len(rejected),
        },
        'proposals': {
            'total': len(props),
            'pending': len(pending),
            'in_review': len(in_review),
            'next_expiry': min(expiries) if expiries else None,
        },
        'now': now,
    }


def record_scan_heartbeat(store, scope=None):
    """记录一次「实际扫描完成」心跳。幂等（同秒内去重），不触发 LLM/批准/下单。

    供监控器在每轮扫描结束后调用；health 面板据此区分「最新扫描时间」
    与「去重后信号事件时间」。
    """
    from scripts.live_trading.decision_ledger.event_store import make_event, insert_event
    scope = scope or store.scope
    key = int(time.time())
    event = make_event(scope, 'scan_heartbeat', key, {})
    with store.transaction() as con:
        insert_event(con, event)
    return event
