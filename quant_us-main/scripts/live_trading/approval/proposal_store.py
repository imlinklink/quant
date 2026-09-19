"""
人工确认下单 - 提案存储（线程安全）

状态机:
    pending → approved → executing → executed
           ↘ rejected        ↘ failed / skipped / expired
           ↘ expired（超时未操作）
"""
import json
import logging
import os
import threading
import time
import uuid
import copy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


ACTIVE_STATUSES = {'pending', 'approved', 'executing', 'submitted', 'partially_filled', 'unknown'}
TERMINAL_STATUSES = {'rejected', 'expired', 'executed', 'failed', 'skipped'}

_ALLOWED_TRANSITIONS = {
    # `pending`/`approved → skipped`：**容量未分配**（不是"过期"）。
    # 两者是不同的事，混用 `expired` 会让界面撒谎（用户会以为是自己没及时处理）。
    # 由容量对账在确有超容量时标记，note 里带规则序位次与上限。
    'pending': {'approved', 'rejected', 'expired', 'skipped'},
    'approved': {'executing', 'rejected', 'expired', 'skipped'},
    'executing': {'executed', 'failed', 'skipped', 'expired', 'submitted', 'partially_filled', 'unknown'},
    'submitted': {'partially_filled', 'executed', 'failed', 'unknown'},
    'partially_filled': {'executed', 'failed', 'unknown'},
    'unknown': {'submitted', 'partially_filled', 'executed', 'failed'},
}


class ProposalStore:
    """提案存储：线程安全，进程内保存 + 决策记录落盘。"""

    def __init__(self, ttl_seconds: float = 180, log_dir: Optional[str] = None, registry=None):
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.RLock()
        self._items: Dict[str, Dict[str, Any]] = {}
        from scripts.live_trading.position_registry import REGISTRY, PositionRegistry
        from scripts.live_trading.decision_ledger.event_store import EventStore
        self.registry = registry or (PositionRegistry(Path(log_dir) / 'execution.sqlite3', 'test') if log_dir else REGISTRY)
        self.events = EventStore(self.registry)
        self._restored_scope = None

        if log_dir is None:
            # approval/ 在 scripts/live_trading/approval/ 下，向上 4 级到项目根
            log_dir = Path(__file__).resolve().parent.parent.parent.parent / 'data' / 'approvals'
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._decision_log = self.log_dir / 'decisions.jsonl'

    # ==================== 写操作 ====================

    def create(self, **kwargs) -> Dict[str, Any]:
        """创建一条待确认提案。kwargs 里放展示/执行所需业务字段。"""
        now = time.time()
        proposal_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._restore()
            item: Dict[str, Any] = {
                'id': proposal_id,
                'status': 'pending',
                'created_at': now,
                'expires_at': now + self.ttl_seconds,
                'updated_at': now,
                'note': '',
            }
            item.update(kwargs)
            if item.get('signal_id'):
                from mutifactor.llm.trade_review import build_plan, build_input
                from scripts.live_trading.decision_ledger.event_store import digest, stable_id
                item.setdefault('side', 'buy')
                item.setdefault('max_price_drift_pct', .03)
                item['account_scope'] = self.events.scope
                # Stable proposal per signal/side: restart does not create another approval.
                proposal_id = stable_id('proposal', self.events.scope, item['signal_id'], item['side'])
                if proposal_id in self._items:
                    return copy.deepcopy(self._items[proposal_id])
                item['id'] = proposal_id
                original_plan = item.pop('approved_plan', None)
                plan = copy.deepcopy(original_plan) if original_plan else build_plan(item, self.events.scope)
                if original_plan:
                    if item['side'] != 'sell' or plan['account_scope'] != self.events.scope or plan['stock_code'] != item['stock_code']:
                        raise ValueError('退出计划与账户/持仓不匹配')
                    item['trade_plan'] = copy.deepcopy(plan['exit_policy'])
                snapshot = build_input(item, plan, item.pop('evidence_items', ()))
                version = plan['plan_version']
                item.update(plan_id=plan['plan_id'], plan_version=version, plan_hash=digest(plan),
                            plan=plan, input_snapshot_id=snapshot['input_snapshot_id'], llm=None,
                            review_id=stable_id('review', proposal_id, version, snapshot['input_snapshot_id']))
                self.events.save_proposal(item, 'proposal_created', snapshots=(
                    ('plan', item['plan_id'], version, plan), ('input', snapshot['input_snapshot_id'], 1, snapshot)))
            self._items[proposal_id] = item
        self._log('created', proposal_id, item.get('stock_code', ''), '')
        self._record_ledger('proposal_created', item)
        return copy.deepcopy(item)

    def approve(self, proposal_id: str, note: str = '', binding=None) -> bool:
        """用户点击「下单」。"""
        with self._lock:
            self._restore()
            item = self._items.get(proposal_id)
            if item and item.get('plan_id'):
                from mutifactor.llm.trade_review import approval_binding
                if binding != approval_binding(item):
                    return False
                if (item.get('llm') or {}).get('recommendation') != 'support_execute' and not note.strip():
                    return False
            ok = self._transition(proposal_id, 'approved', note or '用户点击下单', binding=binding)
        if ok:
            self._log('approved', proposal_id, self._code(proposal_id), '用户点击下单')
            self._record_ledger('human_decision', self.get(proposal_id), {'action': 'approve'})
        return ok

    def reject(self, proposal_id: str, note: str = '') -> bool:
        """用户点击「拒绝」。"""
        reason = note or '用户点击拒绝'
        ok = self._transition(proposal_id, 'rejected', reason)
        if ok:
            self._log('rejected', proposal_id, self._code(proposal_id), reason)
            self._record_ledger('human_decision', self.get(proposal_id), {'action': 'reject', 'note': note})
        return ok

    def mark(self, proposal_id: str, status: str, note: str = '') -> bool:
        """内部状态推进（executing / executed / failed / skipped / expired 等）。"""
        ok = self._transition(proposal_id, status, note)
        if ok:
            self._log(f'mark:{status}', proposal_id, self._code(proposal_id), note)
            if status in ('executing', 'executed', 'failed', 'skipped', 'expired'):
                self._record_ledger('execution_status', self.get(proposal_id), {'status': status, 'note': note})
        return ok

    def update_fields(self, proposal_id: str, **fields) -> bool:
        """线程安全地补写展示字段（如异步 LLM 判定回填）。终态/不存在返回 False。"""
        with self._lock:
            item = self._items.get(proposal_id)
            if not item or item['status'] not in ('pending', 'approved'):
                return False
            if item.get('plan_id'):
                # Material edits use revise_plan; reviews use complete_review.
                if set(fields) - {'note'}:
                    return False
                updated = copy.deepcopy(item)
                updated.update(fields, updated_at=time.time())
                self.events.save_proposal(updated, 'proposal_note', key=f"{proposal_id}:{updated['updated_at']}")
                self._items[proposal_id] = updated
                return True
            item.update(fields)
            item['updated_at'] = time.time()
        return True

    def expire_old(self, now: Optional[float] = None) -> int:
        """所有未执行提案超时过期，不自动批准或下单。"""
        now = time.time() if now is None else now
        expired = 0
        with self._lock:
            self._restore()
            pids = list(self._items.keys())
        for pid in pids:
            item = self.get(pid)
            if not item:
                continue
            if item['status'] in ('pending', 'approved') and now > item.get('expires_at', now):
                if self._transition(pid, 'expired', '超时未确认，自动过期'):
                    expired += 1
        # 顺手清理超过保留期的终态提案，避免进程长期运行内存无限增长
        self.purge_terminal(now=now, keep_seconds=86400)
        return expired

    def purge_terminal(self, now: Optional[float] = None,
                       keep_seconds: float = 86400) -> int:
        """删除超过保留期的终态提案（rejected/expired/executed/failed/skipped）。

        历史记录由 decisions.jsonl 与评估账本长期保存，这里只回收进程内内存；
        活跃提案与保留期内的终态提案不受影响。
        """
        now = time.time() if now is None else now
        removed = 0
        with self._lock:
            pids = list(self._items.keys())
            for pid in pids:
                item = self._items.get(pid)
                if not item:
                    continue
                if item['status'] in TERMINAL_STATUSES:
                    updated = float(item.get('updated_at') or item.get('created_at') or 0)
                    if now - updated > keep_seconds:
                        del self._items[pid]
                        removed += 1
        if removed:
            logger.debug(f"已清理 {removed} 条超过保留期的终态提案")
        return removed

    # ==================== 读操作 ====================

    def get(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._restore()
            item = self._items.get(proposal_id)
            return copy.deepcopy(item) if item else None

    def get_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._restore()
            items = copy.deepcopy(list(self._items.values()))
        items.sort(key=lambda v: v.get('created_at', 0), reverse=True)
        return items

    def has_active(self) -> bool:
        with self._lock:
            self._restore()
            return any(v['status'] in ACTIVE_STATUSES for v in self._items.values())

    def has_active_for_code(self, stock_code: str) -> bool:
        with self._lock:
            self._restore()
            return any(
                v.get('stock_code') == stock_code and v['status'] in ACTIVE_STATUSES
                for v in self._items.values()
            )

    def approved_items(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._restore()
            return copy.deepcopy([v for v in self._items.values() if v['status'] == 'approved'])

    def rejected_items(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._restore()
            return copy.deepcopy([v for v in self._items.values() if v['status'] == 'rejected'])

    def active_count(self) -> int:
        with self._lock:
            self._restore()
            return sum(1 for v in self._items.values() if v['status'] in ACTIVE_STATUSES)

    def active_buys(self) -> List[Dict[str, Any]]:
        """在途**买入**提案（按 `created_at` 升序 = 规则基线顺序）。

        **必须按 `side` 过滤**：卖单与买单共用同一个 store（`chandelier_exit_manager` 的
        退出提案也在里面），而卖单**释放**容量、不是竞争者。`active_count()` 不分方向，
        所以它不能当作"在途买入数"用。

        返回升序（与 `get_all()` 的降序相反）是刻意的：容量分配的口径是**先到先得**，
        顺序本身是语义的一部分，不该让调用方各自再排一次。
        """
        with self._lock:
            self._restore()
            items = [copy.deepcopy(v) for v in self._items.values()
                     if v.get('side') == 'buy' and v['status'] in ACTIVE_STATUSES]
        items.sort(key=lambda v: (v.get('created_at') or 0, str(v.get('id') or '')))
        return items

    # ==================== 内部 ====================

    def _transition(self, proposal_id: str, target: str, note: str, binding=None) -> bool:
        with self._lock:
            self._restore()
            item = self._items.get(proposal_id)
            if not item:
                return False
            item = copy.deepcopy(item)
            if target == 'approved' and item.get('plan_id'):
                from mutifactor.llm.trade_review import approval_binding
                if binding != approval_binding(item):
                    return False
            if target in ('approved', 'executing'):
                if time.time() >= float(item.get('expires_at', 0)):
                    self._transition(proposal_id, 'expired', '确认已过期')
                    return False
                if not self.llm_ready(item):
                    return False
                if target == 'executing' and item.get('plan_id'):
                    from mutifactor.llm.trade_review import approval_binding
                    if item.get('approved_binding') != approval_binding(item):
                        return False
            current = item['status']
            allowed = _ALLOWED_TRANSITIONS.get(current, set())
            # 幂等：同状态重复标记不允许（避免重复下单）
            if target == current or target not in allowed:
                return False
            item['status'] = target
            item['updated_at'] = time.time()
            if note:
                item['note'] = note
            if item.get('plan_id'):
                from mutifactor.llm.trade_review import approval_binding
                if target == 'approved':
                    item['approved_binding'] = approval_binding(item)
                    item['approved_at'] = time.time()
                    item['override_reason'] = note if item['llm']['recommendation'] != 'support_execute' else ''
                event_type = ('human_decision' if target in ('approved','rejected') else
                              'proposal_expired' if target == 'expired' else 'execution_status')
                self.events.save_proposal(item, event_type, {'status': target, 'note': note,
                    'binding': item.get('approved_binding'), 'override_reason': item.get('override_reason')},
                    key=f"{proposal_id}:{item['plan_version']}:{target}")
            self._items[proposal_id] = item
            return True

    def recover_order(self, order, status, env):
        """重启恢复执行结果供页面查看，绝不恢复为可再次执行的批准状态。"""
        with self._lock:
            self._restore()
            previous = self._items.get(order['id'])
            item = copy.deepcopy(previous) if previous else dict(order.get('proposal') or {}, id=order['id'],
                    stock_code=order['code'], side=order['side'], env=env,
                    price=order['price'], quantity=order['qty'], status=status,
                    created_at=order.get('created_at', time.time()))
            projection = dict(status=status, filled_quantity=order.get('filled_qty',0),
                broker_order_id=order.get('order_id'), submitted_quantity=order['qty'],
                note=f"订单 {order.get('order_id', '')}：{order['status']}，已成交 {order.get('filled_qty', 0)}/{order['qty']}")
            if previous and all(previous.get(k)==v for k,v in projection.items()):
                return
            item.update(projection, updated_at=time.time())
            if item.get('plan_id'):
                self.events.save_proposal(item, 'order_projection', projection,
                    key=[item['id'],item.get('_revision',0)+1])
            self._items[order['id']] = item
            self._record_ledger('execution_status', item, projection)

    @staticmethod
    def llm_ready(item) -> bool:
        review = item.get('llm') or {}
        if item.get('plan_id'):
            from scripts.live_trading.decision_ledger.event_store import digest
            return (isinstance(review, dict) and review.get('status') == 'complete'
                    and not review.get('plan_change_requested')
                    and review.get('review_id') == item.get('review_id')
                    and review.get('plan_version') == item.get('plan_version')
                    and review.get('plan_id') == item.get('plan_id')
                    and review.get('input_snapshot_id') == item.get('input_snapshot_id')
                    and item.get('plan_hash') == digest(item.get('plan'))
                    and item.get('trade_plan', {}) == (item.get('plan') or {}).get('exit_policy')
                    and time.time() < float(review.get('expires_at', 0)))
        return (isinstance(review, dict)
                and review.get('verdict') in ('allow', 'delay', 'block', 'pass', 'watch', 'veto', 'hold', 'sell')
                and bool(str(review.get('reason') or '').strip())
                and not review.get('error'))

    def _restore(self):
        if self._restored_scope != self.events.scope:
            # Never carry approvals from one account into another.
            self._items = {}
            self._restored_scope = self.events.scope
        persisted = self.events.proposals()
        self._items.update({v['id']: v for v in persisted
                            if v['status'] not in TERMINAL_STATUSES or time.time()-v.get('updated_at',0)<86400})

    def begin_review(self, proposal_id):
        with self._lock:
            item = self.get(proposal_id)
            if not item or not item.get('plan_id') or item['status'] != 'pending' or item.get('review_requested_at'):
                return None
            item['review_requested_at'] = time.time()
            self.events.save_proposal(item, 'llm_requested', {'requested_at': item['review_requested_at']},
                                      key=item['review_id'])
            self._items[proposal_id] = item
            return copy.deepcopy(item)

    def complete_review(self, request, raw, model='', metadata=None, ttl=180, ttls=None):
        from mutifactor.llm.trade_review import validate_review, SCHEMA_VERSION, PROMPT_VERSION
        from scripts.live_trading.decision_ledger.event_store import utc
        snapshot = self.events.get_snapshot('input', request['input_snapshot_id'])
        now = time.time()
        try:
            review = validate_review(raw, snapshot, request.get('side', 'buy'), now, ttls)
        except Exception as exc:
            review = dict(status='failed', recommendation='defer', proposed_action='hold',
                          error=type(exc).__name__, missing_information=['模型无结果、格式/引用无效或资料已过期'])
        if now >= request['expires_at']:
            review['status'] = 'stale'
        review.update(review_id=request['review_id'], plan_id=request['plan_id'], plan_version=request['plan_version'],
                      input_snapshot_id=request['input_snapshot_id'], model=model, schema_version=SCHEMA_VERSION,
                      prompt_version=PROMPT_VERSION, requested_at=utc(request['review_requested_at']), responded_at=utc(now),
                      latency_seconds=now-request['review_requested_at'], expires_at=min(request['expires_at'], now+ttl),
                      usage=(metadata or {}).get('usage'), cost_usd=(metadata or {}).get('cost_usd'),
                      raw_output=raw)
        with self._lock:
            if self.events.get_snapshot('review', request['review_id']):
                return False
            item = self.get(request['id'])
            applicable = bool(item and item['status'] == 'pending' and item['review_id'] == request['review_id'])
            kind = 'llm_completed' if review['status'] in ('complete','insufficient_information') else 'llm_failed'
            if applicable:
                item['llm'] = review
                if request.get('counterfactual_id'):
                    item['counterfactual_id'] = request['counterfactual_id']
                self.events.save_proposal(item, kind, review, key=request['review_id'],
                    snapshots=(('review', request['review_id'], 1, review),))
                self._items[item['id']] = item
            else:
                # A late callback is auditable but cannot mutate approved/submitted orders or a revised plan.
                with self.events.transaction() as con:
                    from scripts.live_trading.decision_ledger.event_store import insert_event, make_event
                    self.events.snapshot(con, 'review', request['review_id'], 1, review)
                    insert_event(con, make_event(self.events.scope, kind, request['review_id'], review,
                        proposal_id=request['id'], signal_id=request['signal_id'], plan_id=request['plan_id'],
                        plan_version=request['plan_version'], review_id=request['review_id']))
            return applicable

    def complete_v2_review(self, request, result, ttl=180):
        """保存 DecisionEngine 结果并投影为现有审批结构。

        v2 输出已由 DecisionEngine 按对应契约校验，不再经过 legacy validator。
        revision fencing 与 complete_review 相同，晚到回调只能落审计快照。
        """
        from scripts.live_trading.decision_bridge import entry_legacy_projection
        review = entry_legacy_projection(result, request=request, ttl=float(ttl))
        with self._lock:
            if self.events.get_snapshot('review', request['review_id']):
                return False
            item = self.get(request['id'])
            applicable = bool(
                item and item['status'] == 'pending'
                and item.get('review_id') == request.get('review_id')
                and item.get('plan_id') == request.get('plan_id')
                and item.get('plan_version') == request.get('plan_version'))
            kind = 'llm_completed' if review['status'] == 'complete' else 'llm_failed'
            if applicable:
                item['llm'] = review
                for field in ('decision_id', 'decision_status', 'decision_engine_version',
                              'model_action', 'effective_action', 'permission_level'):
                    item[field] = review.get(field)
                self.events.save_proposal(
                    item, kind, review, key=request['review_id'],
                    snapshots=(('review', request['review_id'], 1, review),))
                self._items[item['id']] = item
            else:
                from scripts.live_trading.decision_ledger.event_store import insert_event, make_event
                with self.events.transaction() as con:
                    self.events.snapshot(con, 'review', request['review_id'], 1, review)
                    insert_event(con, make_event(
                        self.events.scope, kind, request['review_id'], review,
                        proposal_id=request['id'], signal_id=request.get('signal_id'),
                        plan_id=request.get('plan_id'), plan_version=request.get('plan_version'),
                        review_id=request['review_id'], decision_id=result.decision_id))
            return applicable

    def revise_plan(self, proposal_id, changes, reason):
        from mutifactor.llm.trade_review import build_plan, build_input
        from scripts.live_trading.decision_ledger.event_store import digest, stable_id
        if not reason.strip():
            raise ValueError('修订必须说明理由')
        if set(changes) - {'trade_plan','price','quantity','expires_at','context','evidence_items'}:
            raise ValueError('不允许修改账户、股票或方向')
        with self._lock:
            item = self.get(proposal_id)
            if not item or item['status'] not in ('pending', 'approved') or not item.get('plan_id'):
                raise ValueError('已提交或终态计划不能修订')
            before = copy.deepcopy(item['plan'])
            item.update(copy.deepcopy(changes))
            version = item['plan_version'] + 1
            plan = build_plan(item, self.events.scope, version, item['plan_version'])
            snapshot = build_input(item, plan, item.pop('evidence_items', ()))
            item.update(plan=plan, plan_version=version, plan_hash=digest(plan), status='pending', llm=None,
                        approved_binding=None, review_requested_at=None, note=reason,
                        input_snapshot_id=snapshot['input_snapshot_id'],
                        review_id=stable_id('review', proposal_id, version, snapshot['input_snapshot_id']))
            self.events.save_proposal(item, 'plan_revised', {'before': before, 'after': plan, 'reason': reason},
                key=f'{proposal_id}:{version}', snapshots=(('plan', item['plan_id'], version, plan),
                                                         ('input', snapshot['input_snapshot_id'], 1, snapshot)))
            self._items[proposal_id] = item
            return copy.deepcopy(item)

    def register_material_event(self, code, evidence):
        """Explicit provider hook; shadow reviews never classify/submit these on their own."""
        from scripts.live_trading.decision_ledger.event_store import utc
        if (not evidence.get('source') or not evidence.get('evidence_id') or
                utc(evidence['observed_at']) > utc() or
                evidence.get('published_at') and utc(evidence['published_at']) > utc()):
            raise ValueError('重大事件来源或时间无效')
        event = self.events.record('material_evidence', evidence['evidence_id'],
                                  {'stock_code':code,'evidence':evidence})
        for item in self.get_all():
            if (item.get('plan_id') and item.get('stock_code')==code and item['status'] in ('pending','approved') and
                    item['plan']['created_at'] < event['observed_at']):
                self.revise_plan(item['id'], {'evidence_items':[evidence]}, '新增重大事件，旧评估与批准失效')
        return event

    def _code(self, proposal_id: str) -> str:
        with self._lock:
            item = self._items.get(proposal_id)
            return item.get('stock_code', '') if item else ''

    @staticmethod
    def _record_ledger(event_type: str, item: Optional[Dict[str, Any]], extra: Optional[Dict[str, Any]] = None):
        """把事件写入共享评估账本（写失败不影响交易）。"""
        if not item:
            return
        try:
            from scripts.live_trading.decision_ledger import ledger
            fields = {
                'proposal_id': item.get('id'),
                'stock_code': item.get('stock_code'),
                'market_type': item.get('market_type'),
                'side': item.get('side', 'buy'),
                'env': item.get('env'),
                'price': item.get('price'),
                'quantity': item.get('quantity'),
                'estimated_cost': item.get('estimated_cost'),
                'entry_mode': item.get('entry_mode'),
                'kline_score': item.get('kline_score'),
                'reason': item.get('reason'),
                'context': item.get('context'),
                'llm': item.get('llm'),
            }
            if extra:
                fields.update(extra)
            ledger.record(event_type, **fields)
        except Exception:
            pass

    def _log(self, action: str, proposal_id: str, stock_code: str, note: str):
        try:
            with open(self._decision_log, 'a', encoding='utf-8') as f:
                line = {
                    'ts': datetime.now().isoformat(timespec='seconds'),
                    'action': action,
                    'id': proposal_id,
                    'stock_code': stock_code,
                    'note': note,
                }
                f.write(json.dumps(line, ensure_ascii=False) + '\n')
        except Exception:
            pass
