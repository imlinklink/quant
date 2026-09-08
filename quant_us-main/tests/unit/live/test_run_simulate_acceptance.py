"""模拟账户验收脚本（任务 C）回归：脱敏、闭环时间线、数据库缺失不冒充零持仓。"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from scripts import run_simulate_acceptance as rsa
from scripts.live_trading.approval.proposal_store import ProposalStore
from scripts.live_trading.decision_ledger.workflow import candidate
from scripts.live_trading.position_registry import PositionRegistry


class RunSimulateAcceptanceContracts(unittest.TestCase):
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

    def _proposal_and_review(self):
        decision = candidate(self.owner, 'US.A', 'dip_buy',
                             datetime(2026, 9, 4, 15, tzinfo=timezone.utc), True,
                             {'score': 12})
        item = self.store.create(stock_code='US.A', side='buy', price=100, quantity=20,
                                 entry_mode='dip_buy', trade_plan={'initial_stop': 95, 'target': 110},
                                 reason='已收盘15分钟信号', **decision)
        req = self.store.begin_review(item['id'])
        snapshot = self.store.events.get_snapshot('input', req['input_snapshot_id'])
        e = snapshot['evidence'][0]
        raw = dict(status='complete', recommendation='support_execute', proposed_action='buy',
                   thesis_state='unchanged',
                   facts=[{'text': e['summary'], 'evidence_ids': [e['evidence_id']]}],
                   inferences=[], counterevidence=[], missing_information=['尚未取得经营新闻'],
                   plan_change_requested=False, next_review_conditions=['触及程序保护线'])
        self.store.complete_review(req, raw, 'fake-model', {'cost_usd': .01})
        return item

    def test_sanitize_config_strips_secrets(self):
        cfg = {'live_manager': {'trd_env': 'SIMULATE'},
               'llm': {'enabled': True, 'api_key': 'sk-secret',
                       'base_url': 'https://x', 'model': 'deepseek-chat'},
               'trading': {'live_trading': {'human_approval': {'enabled': True}}},
               'dip_buy': {'watch_list': ['US.A']}}
        s = rsa._sanitize_config(cfg)
        self.assertEqual(s['trd_env'], 'SIMULATE')
        self.assertTrue(s['llm_enabled'])
        blob = json.dumps(s)
        self.assertNotIn('api_key', blob)
        self.assertNotIn('sk-secret', blob)

    def test_trace_loop_extracts_stages(self):
        self._proposal_and_review()
        loops = rsa.trace_loop(self.store.events.events())
        self.assertEqual(len(loops), 1)
        stages = loops[0]['stages']
        for key in ('signal', 'plan', 'proposal', 'review'):
            self.assertIn(key, stages)
        self.assertEqual(stages['signal']['passed'], True)

    def test_readonly_report_missing_db_not_zero_positions(self):
        cfg_path = self.path / 'config.yaml'
        cfg_path.write_text(yaml.safe_dump({
            'live_manager': {'trd_env': 'SIMULATE'},
            'llm': {}, 'trading': {'live_trading': {'human_approval': {}}},
        }), encoding='utf-8')
        with patch('urllib.request.urlopen', side_effect=OSError('no web')):
            report = rsa.readonly_report(config_path=cfg_path, db_path=self.path / 'nope.db')
        db_check = [c for c in report['checks'] if c['name'] == 'database'][0]
        self.assertEqual(db_check['status'], 'warn')
        self.assertIn('不存在', db_check['detail'])

    def test_readonly_report_reads_real_db(self):
        self._proposal_and_review()
        cfg_path = self.path / 'config.yaml'
        cfg_path.write_text(yaml.safe_dump({
            'live_manager': {'trd_env': 'SIMULATE'},
            'llm': {'enabled': True, 'model': 'deepseek-chat'},
            'trading': {'live_trading': {'human_approval': {'enabled': True}}},
        }), encoding='utf-8')
        with patch('urllib.request.urlopen', side_effect=OSError('no web')):
            report = rsa.readonly_report(config_path=cfg_path, db_path=self.registry.path)
        db_check = [c for c in report['checks'] if c['name'] == 'database'][0]
        self.assertEqual(db_check['status'], 'ok')
        self.assertEqual(report['llm']['status'], 'success')
        self.assertEqual(report['trace']['event_types'].get('proposal_created'), 1)
        # 脱敏：报告不含 api_key
        self.assertNotIn('api_key', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
