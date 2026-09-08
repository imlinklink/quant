"""Synthetic end-to-end decision contracts. No market/model/broker network calls."""
import copy
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mutifactor.llm.trade_review import approval_binding, evidence, legacy_adapter, validate_review
from scripts.live_trading.approval.proposal_store import ProposalStore
from scripts.live_trading.decision_ledger.event_store import EventStore, stable_id, utc
from scripts.live_trading.decision_ledger.funnel_report import build_funnel
from scripts.live_trading.decision_ledger.workflow import candidate
from scripts.live_trading.execution import ExecutionService
from scripts.live_trading.position_registry import PositionRegistry


class DecisionContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.registry = PositionRegistry(self.path/'state.db', 'DRY-RUN')
        self.store = ProposalStore(log_dir=self.path, registry=self.registry)
        p = patch.object(ProposalStore, '_record_ledger')
        p.start(); self.addCleanup(p.stop)
        self.config = {'risk_budget': {'code_groups': {'US.A':'semis'}, 'group_limits': {'semis':.0075}}}
        self.owner = SimpleNamespace(config=self.config, approval_store=self.store, dry_run=True, pool=None)

    def proposal(self, side='buy', suffix='', **fields):
        decision = candidate(self.owner, 'US.A', 'dip_buy'+suffix,
                             datetime(2026, 9, 4, 15, tzinfo=timezone.utc), True,
                             {'score':12, 'initial_stop':95})
        return self.store.create(stock_code='US.A', side=side, price=100, quantity=20,
            entry_mode='dip_buy', trade_plan={'initial_stop':95, 'target':110},
            reason='已收盘15分钟信号', **decision, **fields)

    def raw(self, request, recommendation='support_execute'):
        snapshot = self.store.events.get_snapshot('input', request['input_snapshot_id'])
        e = snapshot['evidence'][0]
        return dict(status='complete', recommendation=recommendation,
                    proposed_action=('exit' if request['side']=='sell' else 'buy') if recommendation=='support_execute' else 'hold',
                    thesis_state='unchanged', facts=[{'text':e['summary'], 'evidence_ids':[e['evidence_id']]}],
                    inferences=[], counterevidence=[], missing_information=['尚未取得经营新闻'],
                    plan_change_requested=False, next_review_conditions=['触及程序保护线'])

    def review(self, item, recommendation='support_execute'):
        req = self.store.begin_review(item['id'])
        self.store.complete_review(req, self.raw(req, recommendation), 'fake-model', {'cost_usd':.01})
        return self.store.get(item['id'])

    def approve(self, item, note=''):
        self.assertTrue(self.store.approve(item['id'], note, approval_binding(item)))
        self.assertTrue(self.store.mark(item['id'], 'executing'))
        return self.store.get(item['id'])

    def test_restart_duplicate_scan_and_duplicate_callback(self):
        p = self.proposal()
        req = self.store.begin_review(p['id'])
        raw = self.raw(req)
        self.assertTrue(self.store.complete_review(req, raw))
        self.assertFalse(self.store.complete_review(req, raw))
        store = ProposalStore(log_dir=self.path, registry=PositionRegistry(self.registry.path, 'DRY-RUN'))
        self.assertEqual(store.get(p['id'])['plan_id'], p['plan_id'])
        self.assertEqual(self.proposal()['id'], p['id'])
        report = build_funnel(store.events.events())
        self.assertEqual(report['candidates'], 1)
        self.assertEqual(report['stages']['with_llm_result'], 1)

    def test_failed_llm_and_unhandled_candidates_retained(self):
        p = self.proposal()
        req = self.store.begin_review(p['id'])
        self.store.complete_review(req, None)
        self.assertFalse(self.store.approve(p['id'], binding=approval_binding(self.store.get(p['id']))))
        self.proposal(suffix='other')
        report = build_funnel(self.store.events.events())
        self.assertEqual(report['candidates'], 2)
        self.assertEqual(report['stages']['llm_failed_or_stale'], 1)
        self.assertEqual(report['stages']['unhandled'], 2)

    def test_override_requires_reason(self):
        item = self.review(self.proposal(), 'oppose_execute')
        self.assertFalse(self.store.approve(item['id'], binding=approval_binding(item)))
        self.assertTrue(self.store.approve(item['id'], '已核对事件，接受原定风险', approval_binding(item)))
        self.assertTrue(self.store.get(item['id'])['override_reason'])

    def test_old_page_binding_cannot_approve_revision(self):
        item = self.review(self.proposal())
        old = approval_binding(item)
        self.store.approve(item['id'], binding=old)
        revised = self.store.revise_plan(item['id'], {'trade_plan':{'initial_stop':94, 'target':110}}, '调整结构边界')
        self.assertEqual(revised['status'], 'pending')
        self.assertIsNone(revised['approved_binding'])
        self.assertFalse(self.store.approve(item['id'], binding=old))
        self.assertEqual(self.store.events.get_snapshot('plan', item['plan_id'], 1)['exit_policy']['initial_stop'],95)

    def test_late_callback_cannot_modify_revised_plan(self):
        item = self.proposal()
        request = self.store.begin_review(item['id'])
        self.store.revise_plan(item['id'], {}, '出现新证据')
        self.assertFalse(self.store.complete_review(request, self.raw(request)))
        self.assertIsNone(self.store.get(item['id'])['llm'])
        self.assertIsNotNone(self.store.events.get_snapshot('review', request['review_id']))

    def test_stale_and_insufficient_results_block(self):
        item = self.proposal()
        request = self.store.begin_review(item['id'])
        raw = self.raw(request); raw['status']='insufficient_information'
        self.store.complete_review(request, raw)
        self.assertFalse(self.store.llm_ready(self.store.get(item['id'])))
        other = self.proposal(suffix='late', expires_at=time.time()-1)
        req = self.store.begin_review(other['id'])
        self.store.complete_review(req, self.raw(req))
        self.assertEqual(self.store.get(other['id'])['llm']['status'], 'stale')

    def test_bad_citations_and_fake_facts_cannot_be_valid(self):
        request = self.store.begin_review(self.proposal()['id'])
        snapshot = self.store.events.get_snapshot('input', request['input_snapshot_id'])
        for invalid in ('missing_source','invented_fact','tool_command','wrong_side'):
            raw = self.raw(request)
            if invalid=='missing_source':raw['facts'][0]['evidence_ids']=['unknown']
            if invalid=='invented_fact':raw['facts'][0]['text']='公司业绩已经改善'
            if invalid=='tool_command':raw['execute_tool']='place_order'
            if invalid=='wrong_side':raw['proposed_action']='exit'
            with self.assertRaises(Exception):validate_review(raw, snapshot, 'buy')

    def test_future_evidence_and_naive_timestamp_rejected(self):
        e = evidence('新闻', 'https://example.test/news', time.time()+3600, time.time()+3600, kind='news')
        with self.assertRaises(ValueError):self.proposal(evidence_items=[e])
        with self.assertRaises(ValueError):utc('2026-09-04T10:00:00')

    def test_dst_and_timeframe_identity(self):
        self.assertEqual(utc('2026-03-09T09:30:00-04:00'), '2026-03-09T13:30:00+00:00')
        self.assertEqual(utc('2026-03-06T09:30:00-05:00'), '2026-03-06T14:30:00+00:00')
        args = (self.owner,'US.A','dip_buy',datetime(2026,9,4,15,tzinfo=timezone.utc),True,{})
        a = candidate(*args, timeframe='15m')
        b = candidate(*args, timeframe='5m')
        self.assertNotEqual(a['signal_id'], b['signal_id'])

    def test_legacy_sell_block_means_hold(self):
        self.assertEqual(legacy_adapter({'verdict':'block'},'sell')['proposed_action'], 'hold')
        self.assertEqual(legacy_adapter({'verdict':'delay'},'sell')['proposed_action'], 'reduce')
        self.assertEqual(legacy_adapter({'verdict':'delay'},'buy')['proposed_action'], 'hold')

    def test_critical_persistence_failure_cannot_approve(self):
        item = self.review(self.proposal())
        with patch.object(self.store.events, 'save_proposal', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.approve(item['id'], binding=approval_binding(item))
        self.assertEqual(self.store.get(item['id'])['status'], 'pending')

    def test_outbox_failure_does_not_reexecute_and_retry_is_replayable(self):
        item = self.approve(self.review(self.proposal()))
        service = ExecutionService(None,self.config,self.store,True,self.registry)
        service.submit(item,100)
        blocked = self.path/'directory'; blocked.mkdir()
        self.assertGreater(self.store.events.export(blocked),0)
        export = self.path/'events.jsonl'
        self.assertEqual(self.store.events.export(export),0)
        events = [json.loads(line) for line in export.read_text().splitlines()]
        earlier = self.registry.path.parent/'decision_ledger'/'events-v1.jsonl'
        if earlier.exists():
            events += [json.loads(line) for line in earlier.read_text().splitlines()]
        self.assertEqual(build_funnel(events),build_funnel(events+events))
        self.assertEqual(self.registry.get('US.A')['qty'],20)
        report = build_funnel(events)
        self.assertEqual(report['stages']['with_fill'],1)
        self.assertEqual(report['linkage']['rate'],1)

    def test_execution_rejects_mutated_caller_payload(self):
        item = self.approve(self.review(self.proposal()))
        item['quantity']=200
        service = ExecutionService(None,self.config,self.store,True,self.registry)
        with self.assertRaises(ValueError):service.submit(item,100)
        self.assertEqual(self.registry.count(),0)

    def test_approved_price_tolerance_cannot_be_relaxed_by_config(self):
        item=self.approve(self.review(self.proposal()))
        self.config['trading']={'live_trading':{'human_approval':{'max_price_drift_pct':.1}}}
        service=ExecutionService(None,self.config,self.store,True,self.registry)
        with self.assertRaises(ValueError):service.submit(item,104)
        self.assertEqual(self.registry.count(),0)

    def test_durable_approval_checked_inside_order_transaction(self):
        item = self.approve(self.review(self.proposal()))
        with self.store.events.transaction() as con:
            con.execute('DELETE FROM decision_proposals')
        service = ExecutionService(None,self.config,self.store,True,self.registry)
        with self.assertRaises(ValueError):service.submit(item,100)
        self.assertEqual(self.registry.count(),0)

    def test_registry_outbox_transaction_rolls_back_together(self):
        self.proposal()
        with patch('scripts.live_trading.decision_ledger.event_store.insert_event', side_effect=sqlite3.OperationalError('full')):
            with self.assertRaises(sqlite3.OperationalError):
                from scripts.live_trading.decision_ledger.event_store import enqueue
                with self.registry.transaction() as book:
                    book['positions']['US.A']={'qty':5}
                    enqueue(book,'DRY-RUN','fill_received','fill-test',{})
        self.assertEqual(self.registry.count(),0)

    def test_database_upgrade_backs_up_old_book(self):
        path = self.path/'legacy.db'
        with sqlite3.connect(path) as con:
            con.execute('CREATE TABLE books(namespace TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            con.execute('INSERT INTO books VALUES (?,?)',('DRY-RUN',json.dumps({'positions':{},'orders':{}})))
        registry=PositionRegistry(path,'DRY-RUN')
        self.assertEqual(registry.count(),0)
        self.assertTrue(Path(str(path)+'.pre-decision-v1.bak').exists())

    def test_material_event_blocks_order_after_confirmation(self):
        item = self.approve(self.review(self.proposal()))
        news = evidence('公司发布新的业绩预警','https://example.test/filing',published_at=time.time(),kind='filing')
        self.store.register_material_event('US.A',news)
        service=ExecutionService(None,self.config,self.store,True,self.registry)
        with self.assertRaises(ValueError):service.submit(item,100)
        self.assertEqual(self.registry.count(),0)

    def test_other_process_reads_approved_proposal(self):
        item = self.review(self.proposal())
        second=ProposalStore(log_dir=self.path,registry=PositionRegistry(self.registry.path,'DRY-RUN'))
        second.get_all()
        self.store.approve(item['id'],binding=approval_binding(item))
        self.assertEqual(second.approved_items()[0]['id'],item['id'])

    def test_confirmation_api_binds_version_and_displays_snapshot(self):
        import importlib
        with patch('yaml.safe_load',return_value={}), patch(
                'scripts.live_trading.approval.proposal_store.ProposalStore',return_value=self.store):
            web=importlib.import_module('web.app')
        item=self.review(self.proposal(),'oppose_execute')
        with patch.object(web,'approval_store',self.store), patch.object(web,'_create_ctx',side_effect=AssertionError('no network')):
            client=web.app.test_client()
            rows=client.get('/api/approvals').get_json()['items']
            self.assertTrue(rows[0]['llm_ready'])
            binding=rows[0]['approval_binding']
            self.assertEqual(client.post('/api/approvals/'+item['id']+'/approve',json={'binding':binding}).status_code,409)
            self.assertEqual(client.post('/api/approvals/'+item['id']+'/approve',json={'binding':binding,'note':'接受既定风险'}).status_code,200)
            snapshot=client.get('/api/approvals/'+item['id']+'/input').get_json()['snapshot']
            self.assertEqual(snapshot['input_snapshot_id'],item['input_snapshot_id'])
            self.assertEqual(client.get('/api/decision-report').status_code,200)


class FillEconomics(unittest.TestCase):
    setUp = DecisionContracts.setUp
    def order(self, pid, side, qty):
        with self.registry.transaction() as book:
            book['orders'][pid] = dict(id=pid,code='US.A',side=side,qty=qty,price=100,status='submitted',
                filled_qty=0,risk=250,cost_per_share=.05,
                metadata={'initial_stop':95,'entry_mode':'dip_buy','risk_group':'semis'})

    def test_incremental_amount_r0_and_fee_adjustments(self):
        self.order('buy','buy',50)
        service=ExecutionService(None,self.config,None,True,self.registry)
        service.apply_report('buy',dict(order_id='broker-buy',order_status='FILLED_PART',dealt_qty=20,dealt_avg_price=101,cumulative_fee=1))
        with self.registry.transaction() as book:
            trade=list(book['trades'].values())[0]
            self.assertIsNone(trade['initial_r0'])
        service.apply_report('buy',dict(order_id='broker-buy',order_status='CANCELLED_PART',dealt_qty=40,dealt_avg_price=102,cumulative_fee=2))
        with self.registry.transaction() as book:
            fill=book['fills'][-1]
            self.assertEqual(fill['price'],103)  # (40*102 - 20*101)/20
            self.assertEqual(fill['amount'],2060)
            tid=book['orders']['buy']['trade_id']
            self.assertEqual(book['trades'][tid]['initial_r0'],280)
        self.registry.update('US.A',exit_state={'stop_line':105})
        self.order('sell1','sell',20)
        report=dict(order_id='broker-sell1',order_status='FILLED_ALL',dealt_qty=20,dealt_avg_price=110,cumulative_fee=1)
        service.apply_report('sell1',report); service.apply_report('sell1',report)
        self.order('sell2','sell',20)
        service.apply_report('sell2',dict(order_id='broker-sell2',order_status='FILLED_ALL',dealt_qty=20,dealt_avg_price=108,cumulative_fee=1))
        with self.registry.transaction() as book:
            trade=book['trades'][tid]
            self.assertEqual(trade['initial_r0'],280)
            self.assertEqual(trade['net_realized_pnl'],276)
            self.assertEqual(trade['remaining_qty'],0)
            self.assertEqual(len(book['fills']),4)
        service.apply_report('sell1',dict(report,cumulative_fee=1.5))
        with self.registry.transaction() as book:
            self.assertEqual(book['trades'][tid]['net_realized_pnl'],275.5)
            self.assertEqual(book['trades'][tid]['initial_r0'],280)
        service.apply_report('sell1',report)  # a later fee correction may return to an earlier value
        with self.registry.transaction() as book:
            self.assertEqual(book['trades'][tid]['net_realized_pnl'],276)
            self.assertEqual(book['orders']['sell1']['fee_revision'],3)

    def test_missing_fees_and_legacy_r_not_invented(self):
        self.registry.open('US.A','manual',10,100)
        self.order('sell','sell',10)
        service=ExecutionService(None,self.config,None,True,self.registry)
        service.apply_report('sell',dict(order_status='FILLED_ALL',dealt_qty=10,dealt_avg_price=110))
        with self.registry.transaction() as book:
            trade=list(book['trades'].values())[0]
            self.assertIsNone(trade['initial_r0'])
            self.assertIsNone(trade['net_realized_pnl'])
            self.assertEqual(trade['gross_realized_pnl'],100)
