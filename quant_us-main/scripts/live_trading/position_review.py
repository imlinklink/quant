"""Event-driven SHADOW position review. This component has no execution API.

Only writes input/review events; recommendations cannot revise approved plans,
move stops, submit orders, or authorize future actions.
"""
import queue
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from mutifactor.llm.trade_review import build_input, validate_review
from .decision_ledger.event_store import EventStore, insert_event, make_event, stable_id, utc
from .decision_ledger.thesis_ledger import ThesisLedger
from .decision_ledger.workflow import review_queue


class PositionReviewScheduler:
    def __init__(self, registry, advisor, config=None, decision_config=None):
        self.events = EventStore(registry)
        self.advisor = advisor
        self.config = config or {}
        self.decision_config = decision_config or self.config
        self.thesis = ThesisLedger(registry)
        self.last_check = {}

    def schedule(self, code, price, now=None, evidence_items=(), event_reason=None):
        if not self.config.get('enabled', False):
            return False
        now = time.time() if now is None else now
        if now-self.last_check.get(code, 0) < float(self.config.get('check_interval_seconds',60)) and not evidence_items and not event_reason:
            return False
        self.last_check[code] = now
        record = self.events.registry.get(code)
        if not record or not record.get('plan_id'):
            return False
        plan = self.events.get_snapshot('plan', record['plan_id'], record.get('plan_version',1))
        if not plan:
            return False
        et = datetime.fromtimestamp(now, ZoneInfo('America/New_York'))
        stop = (record.get('exit_state') or {}).get('stop_line') or record.get('initial_stop')
        target = record.get('target')
        trigger = event_reason
        if stop and abs(price-float(stop))/float(stop) <= float(self.config.get('near_stop_pct',.01)):
            trigger = 'near_risk_boundary'
        elif target and abs(price-float(target))/float(target) <= float(self.config.get('near_target_pct',.01)):
            trigger = 'near_target'
        elif evidence_items:
            trigger = 'new_evidence'
        elif et.hour >= 16 and et.weekday()<5:
            trigger = 'session_close'
        if not trigger:
            return False
        clusters = sorted(set(e['cluster_id'] for e in evidence_items))
        window = et.date().isoformat() if trigger=='session_close' else int(now/float(self.config.get('dedup_seconds',900)))
        rid = stable_id('position_review', self.events.scope, record['trade_id'], plan['plan_id'], plan['plan_version'], trigger, clusters, window)
        eid = stable_id('event',self.events.scope,'position_review_requested',rid)
        previous = next((e['payload'] for e in reversed(self.events.events())
                         if e['event_type']=='position_reviewed' and e.get('trade_id')==record['trade_id']),None)
        item = dict(price=price, quantity=record['qty'], reason=trigger, quote_observed_at=utc(now),
                    position_context={'position':record, 'previous_review':previous})
        snapshot = build_input(item, plan, evidence_items)
        links = dict(trade_id=record['trade_id'],plan_id=plan['plan_id'],plan_version=plan['plan_version'],review_id=rid)
        with self.events.transaction() as con:
            if con.execute('SELECT 1 FROM decision_events WHERE event_id=?',(eid,)).fetchone():
                return False
            self.events.snapshot(con,'input',snapshot['input_snapshot_id'],1,snapshot)
            insert_event(con,make_event(self.events.scope,'position_review_requested',rid,
                {'trigger':trigger,'input':snapshot,'shadow_only':True},**links))

        def run():
            raw = None
            packet = None
            try:
                from scripts.live_trading.decision_runtime import DecisionRuntime
                runtime = DecisionRuntime(self.events.registry, self.advisor,
                                          self.decision_config)
                if runtime.is_shadow('position'):
                    from scripts.live_trading.decision_bridge import (
                        build_position_packet, position_legacy_projection,
                    )
                    trade = {
                        'trade_id': record['trade_id'], 'code': record['code'],
                        'direction': 'long', 'remaining_qty': float(record['qty']),
                        'entry_price': float(record.get('entry_price') or 0),
                        'mark_price': float(price),
                        'sector': record.get('sector'),
                        'risk_group': record.get('risk_group'),
                    }
                    protection = {
                        'active_stop': float(stop or 0),
                        'initial_stop': record.get('initial_stop'),
                        'hard_exit_authoritative': True,
                    }
                    packet = build_position_packet(
                        trade=trade, protection=protection,
                        new_evidence=list(evidence_items),
                        account_scope=self.events.scope,
                        subject_id=record['trade_id'], as_of=utc(now),
                        thesis={'state': self.thesis.current(record['trade_id']) or 'FORMING'},
                        model={'provider': 'configured',
                               'model_id': getattr(self.advisor, 'model', ''),
                               'temperature': 0.0, 'timeout_seconds': 30})
                    result = runtime.engine().decide_position(packet)
                    review = position_legacy_projection(
                        result, trigger=trigger,
                        legacy_input_snapshot_id=snapshot['input_snapshot_id'])
                    raw = result.validated_output
                else:
                    raw = self.advisor.review_plan(snapshot,'sell') if self.advisor else None
                    review = validate_review(raw,snapshot,'sell',ttls=self.config.get('evidence_ttl_seconds'))
            except Exception as exc:
                review = {'status':'failed','error':type(exc).__name__}
            review.update(shadow_only=True, trigger=trigger, input_snapshot_id=snapshot['input_snapshot_id'],
                          raw_output=raw, plan_change_applied=False,
                          comparison='无新增独立证据，保持原批准计划' if not evidence_items else '建议需人工审阅并另行确认')
            if (packet is not None and review.get('status') == 'complete'
                    and review.get('decision_id')):
                try:
                    from .decision_ledger.position_counterfactual import PositionCounterfactualLedger
                    frozen = PositionCounterfactualLedger(self.events.registry).freeze(
                        packet, review, review_id=rid, trigger=trigger,
                        fee_rate=float(self.config.get('counterfactual_fee_rate', 0.0)))
                    review['counterfactual_id'] = frozen['counterfactual_id']
                except Exception:
                    # 影子实验失败不得影响持仓评审主链。
                    pass
            decision_link = ({'decision_id': review.get('decision_id')}
                             if review.get('decision_id') else {})
            self.events.record('position_reviewed',rid,review,**links, **decision_link)
            # thesis ledger：仅有效评审更新逻辑状态（delta 由程序计算；无新证据不改状态）
            if review.get('status') == 'complete' and review.get('thesis_state'):
                try:
                    self.thesis.record_review(
                        trade_id=links.get('trade_id'), code=record['code'],
                        plan_id=plan['plan_id'], plan_version=plan['plan_version'],
                        review_id=rid, review=review,
                        evidence_items=evidence_items, trigger=trigger)
                except Exception:
                    # thesis 记录失败不影响评审主流程
                    pass

        try:
            review_queue(self.config).put(0 if trigger=='near_risk_boundary' else 2,run)
        except queue.Full:
            self.events.record('position_reviewed',rid,{'status':'failed','reason':'queue_full','shadow_only':True},**links)
        return True
