"""版本化 Thesis Ledger（阶段 I2）。

持仓逻辑状态机：established → strengthened / unchanged / weakened → invalidated → closed。
每次状态变化必须引用新增证据，并和上一版做 delta（新增/撤销什么、哪个失效条件被触发）；
没有新证据时，同一输入不应仅因模型措辞变化改 thesis_state。

纯影子：只记录，不改持仓、不动止损、不产生订单。
回放：thesis 演进以 append-only 事件（thesis_updated）存入 decision_events，可完整重建。
"""
import time

from .event_store import EventStore, stable_id

# 状态序（用于报告展示顺序）
THESIS_STATES = ('established', 'strengthened', 'unchanged', 'weakened', 'invalidated', 'closed')

# 模型可输出、且会被采纳的状态
_MODEL_ADOPTABLE = {'strengthened', 'weakened', 'invalidated', 'unchanged'}


def cited_evidence_ids(review):
    """提取一次评审实际引用的证据 id 集（facts/inferences/counterevidence）。"""
    ids = set()
    for section in ('facts', 'inferences', 'counterevidence'):
        for claim in (review or {}).get(section) or []:
            for eid in claim.get('evidence_ids') or []:
                if eid:
                    ids.add(str(eid))
    return ids


def build_delta(prev_cited, new_cited):
    """相对上一版评审，本次新增/撤销了哪些证据引用。"""
    prev_cited = set(prev_cited or ())
    new_cited = set(new_cited or ())
    return {
        'added': sorted(new_cited - prev_cited),
        'removed': sorted(prev_cited - new_cited),
    }


def apply_transition(prev_state, model_state, has_new_evidence, near_risk):
    """决定是否采纳模型建议的状态（无新证据不改 thesis_state）。

    Returns:
        (adopted_state, note)
    """
    if model_state not in _MODEL_ADOPTABLE:
        # unknown/failed/缺失 → 不改
        return (prev_state or 'established'), f'模型状态 {model_state!r} 不可采纳，保持'
    if prev_state is None:
        # 首次评审：建立逻辑
        return 'established', '持仓逻辑建立（首次评审）'
    if prev_state == 'invalidated':
        # 已失效，不往回改（closed 由实际平仓驱动）
        return 'invalidated', '已失效，保持失效'
    if model_state == 'unchanged' or model_state == prev_state:
        return prev_state, '状态不变'
    # 状态要变化（strengthened/weakened/invalidated）：必须有新证据，或价格已到保护线
    if not has_new_evidence and not near_risk:
        return prev_state, (
            f'模型建议 {model_state} 但无新增证据且未触及保护线，'
            '按“无新证据不改 thesis_state”保持'
        )
    return model_state, f'模型建议 {model_state}（有新证据或触及保护线），采纳'


class ThesisLedger:
    """thesis 演进账本：append-only 事件，可按 trade 回放。"""

    def __init__(self, registry):
        self.events = EventStore(registry)

    @property
    def scope(self):
        return self.events.scope

    def load_updates(self, trade_id):
        """按时间回放某 trade 的全部 thesis_updated 事件。"""
        evs = [e for e in self.events.events()
               if e.get('event_type') == 'thesis_updated'
               and e.get('trade_id') == trade_id]
        evs.sort(key=lambda e: (e.get('observed_at'), e.get('event_id')))
        return [e.get('payload', {}) for e in evs]

    def current(self, trade_id):
        """该 trade 最近一次采纳的 thesis 状态。"""
        updates = self.load_updates(trade_id)
        return updates[-1].get('state') if updates else None

    def record_review(self, *, trade_id, code, plan_id, plan_version, review_id,
                      review, evidence_items, trigger, now=None):
        """把一次持仓评审写入 thesis 账本。

        P1-4 收紧：只有「模型实际引用了本轮新增证据」才允许状态变化——
        has_new_evidence = (delta.added ∩ 本轮输入证据 id) 非空；
        并校验模型所有 cited_ids 都属于冻结输入（本轮 evidence_items）。
        状态不变不写版本；追加 thesis_updated 事件（幂等）。
        """
        prev = self.load_updates(trade_id)
        prev_state = prev[-1].get('state') if prev else None
        prev_cited = prev[-1].get('cited_ids', []) if prev else []

        new_cited = sorted(cited_evidence_ids(review))
        delta = build_delta(prev_cited, new_cited)

        # 冻结输入 = 本轮 evidence_items 的证据 id（可能是 dict 或 id 字符串）
        input_ids = set()
        for e in evidence_items or []:
            if isinstance(e, dict) and e.get('evidence_id'):
                input_ids.add(str(e['evidence_id']))
            elif isinstance(e, str) and e:
                input_ids.add(e)
        # 校验：模型引用必须都在冻结输入内（不在则不计入“新增”，且记录越界引用）
        cited_outside_input = sorted(set(new_cited) - input_ids) if input_ids else []
        added_in_input = sorted(set(delta.get('added', [])) & input_ids) if input_ids else []

        near_risk = trigger == 'near_risk_boundary'
        # 状态变化条件：新增证据且被模型实际引用（delta.added ∩ 输入非空），或价格触保护线
        has_new_evidence = bool(added_in_input)
        adopted, note = apply_transition(prev_state,
                                         (review or {}).get('thesis_state'),
                                         has_new_evidence, near_risk)

        version = (prev[-1].get('version', 0) + 1) if prev else 1
        # 只在状态真正变化时写新版本（首次建立 / 状态改变）；无变化不产生噪音版本。
        if prev_state is not None and adopted == prev_state:
            return None
        entry = {
            'version': version,
            'state': adopted,
            'model_state': (review or {}).get('thesis_state'),
            'trigger': trigger,
            'plan_version': plan_version,
            'review_id': review_id,
            'cited_ids': new_cited,
            'delta': delta,
            'cited_outside_input': cited_outside_input,
            'note': note,
            'shadow_only': True,
        }
        self.events.record('thesis_updated', [trade_id, version],
                           dict(entry, code=code, plan_id=plan_id, trade_id=trade_id),
                           trade_id=trade_id, plan_id=plan_id,
                           plan_version=plan_version, review_id=review_id)
        return dict(entry, code=code, plan_id=plan_id, trade_id=trade_id)

    def mark_closed(self, *, trade_id, reason='position_closed', now=None):
        """实际平仓时把状态置为 closed（由持仓移除/退出事件驱动）。"""
        updates = self.load_updates(trade_id)
        version = (updates[-1].get('version', 0) + 1) if updates else 1
        entry = {
            'version': version, 'state': 'closed', 'model_state': None,
            'trigger': reason, 'delta': {'added': [], 'removed': []},
            'note': '持仓已平仓，thesis 关闭', 'shadow_only': True,
            'ts': None,
        }
        return self.events.record('thesis_updated', [trade_id, version], entry,
                                  trade_id=trade_id)


def chain_report(updates):
    """把 thesis 演进链压成紧凑报告（含每步状态/触发/delta 概要）。"""
    lines = []
    for u in updates:
        d = u.get('delta') or {}
        delta_txt = []
        if d.get('added'):
            delta_txt.append(f"+{len(d['added'])}证据")
        if d.get('removed'):
            delta_txt.append(f"-{len(d['removed'])}证据")
        state = u.get('state')
        mark = '🔴' if state == 'invalidated' else '🟢' if state in ('strengthened',) else \
               '🟡' if state == 'weakened' else '⚪'
        lines.append(
            f"  v{u.get('version')} {mark} {state} "
            f"[{u.get('trigger')}] {u.get('note', '')}"
            + (f" ({'; '.join(delta_txt)})" if delta_txt else '')
        )
    return '\n'.join(lines)


def compare_actual_exit(events, trade_id):
    """把 thesis 演进与程序实际退出对比：LLM 何时判失效 vs 程序何时实际退出。

    从 decision_events 里找该 trade 的 thesis_updated 与平仓事件。
    返回 dict：{chain, actual_exit, invalidated_at, timeline}。
    """
    thesis = sorted(
        [e for e in events
         if e.get('event_type') == 'thesis_updated' and e.get('trade_id') == trade_id],
        key=lambda e: (e.get('observed_at'), e.get('event_id')))

    exits = [e for e in events
             if e.get('event_type') == 'trade_closed' and e.get('trade_id') == trade_id]

    invalidated = [e for e in thesis
                   if (e.get('payload') or {}).get('state') == 'invalidated']

    return {
        'trade_id': trade_id,
        'invalidated_at': invalidated[0].get('observed_at') if invalidated else None,
        'invalidated_count': len(invalidated),
        'actual_exit': {
            'at': exits[-1].get('observed_at') if exits else None,
            'status': (exits[-1].get('payload') or {}).get('status') if exits else None,
        } if exits else None,
        'chain': chain_report([e.get('payload', {}) for e in thesis]),
        'timeline': [
            {'event': e.get('event_type'), 'at': e.get('observed_at'),
             'state': (e.get('payload') or {}).get('state')}
            for e in sorted(thesis + exits, key=lambda e: e.get('observed_at'))
        ],
    }
