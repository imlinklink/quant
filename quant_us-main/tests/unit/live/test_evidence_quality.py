"""补证据再评估（任务 E）回归：输入质量统计、样本外划分、效果报告。"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.live_trading.approval.proposal_store import ProposalStore
from scripts.live_trading.decision_ledger.workflow import candidate
from scripts.live_trading.decision_ledger.input_quality import build_input_quality
from scripts.live_trading.decision_ledger.sample_split import split_signals
from scripts.live_trading.decision_ledger.llm_effectiveness import build_effectiveness
from scripts.live_trading.position_registry import PositionRegistry


class EvidenceQualityContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.registry = PositionRegistry(self.path / 'state.db', 'DRY-RUN')
        self.store = ProposalStore(log_dir=self.path, registry=self.registry)
        p = patch.object(ProposalStore, '_record_ledger')
        p.start()
        self.addCleanup(p.stop)
        self.config = {'risk_budget': {'code_groups': {'US.A': 'semis'},
                                       'group_limits': {'semis': .0075}}}
        self.owner = SimpleNamespace(config=self.config, approval_store=self.store,
                                     dry_run=True, pool=None)

    def _proposal_and_review(self, status='complete', suffix=''):
        decision = candidate(self.owner, 'US.A', 'dip_buy' + suffix,
                             datetime(2026, 9, 4, 15, tzinfo=timezone.utc), True,
                             {'score': 12})
        item = self.store.create(stock_code='US.A', side='buy', price=100, quantity=20,
                                 entry_mode='dip_buy', trade_plan={'initial_stop': 95, 'target': 110},
                                 reason='已收盘15分钟信号', **decision)
        req = self.store.begin_review(item['id'])
        snapshot = self.store.events.get_snapshot('input', req['input_snapshot_id'])
        e = snapshot['evidence'][0]
        raw = dict(status=status, recommendation='support_execute', proposed_action='buy',
                   thesis_state='unchanged',
                   facts=[{'text': e['summary'], 'evidence_ids': [e['evidence_id']]}],
                   inferences=[], counterevidence=[], missing_information=['尚未取得经营新闻'],
                   plan_change_requested=False, next_review_conditions=['触及程序保护线'])
        self.store.complete_review(req, raw, 'fake-model', {'cost_usd': .01})
        return item

    def test_input_quality_counts_missing_information(self):
        self._proposal_and_review()
        self._proposal_and_review(status='insufficient_information', suffix='x')
        report = build_input_quality(self.store.events.events())
        self.assertGreaterEqual(report['total_reviews'], 2)
        self.assertIn('尚未取得经营新闻', report['missing_information'])
        self.assertGreaterEqual(report['status_distribution'].get('insufficient_information', 0), 1)

    def test_split_signals_by_date_no_cross_split(self):
        events = [
            {'event_type': 'rule_candidate', 'signal_id': 's1',
             'payload': {'signal_bar_end': '2026-01-15T00:00:00+00:00'}},
            {'event_type': 'rule_candidate', 'signal_id': 's2',
             'payload': {'signal_bar_end': '2026-03-15T00:00:00+00:00'}},
            # 同一 signal 的后续事件（不应改变分期）
            {'event_type': 'fill_received', 'signal_id': 's1',
             'payload': {'signal_bar_end': None}},
        ]
        buckets = split_signals(events, '2026-02-01', '2026-04-01')
        self.assertEqual(buckets['train'], ['s1'])
        self.assertEqual(buckets['val'], ['s2'])
        self.assertEqual(buckets['test'], [])

    def test_split_signals_unassigned_on_bad_date(self):
        events = [
            {'event_type': 'rule_candidate', 'signal_id': 's1',
             'payload': {'signal_bar_end': 'not-a-date'}},
        ]
        buckets = split_signals(events, '2026-02-01', '2026-04-01')
        self.assertEqual(buckets['unassigned'], ['s1'])

    def test_effectiveness_report(self):
        self._proposal_and_review()
        report = build_effectiveness(self.store.events.events())
        self.assertGreaterEqual(report['sample']['candidates'], 1)
        self.assertIn('rates', report)
        self.assertIn('conclusion', report)
        # 样本不足 → 结论明确不声称有效
        self.assertIn('样本不足', report['conclusion'])


if __name__ == '__main__':
    unittest.main()
