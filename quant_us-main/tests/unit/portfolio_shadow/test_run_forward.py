"""前向运行关键路径：#1 成交时序（计划日入队、到期认领）、#3 证据质量门、#4 未知成本。

这些是 review 指出的「75 项测试通过但没覆盖的关键路径」——CLI 之前完全没有测试。
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import json

import pandas as pd

from scripts.live_trading.decision_ledger.event_store import stable_id
from scripts.portfolio_shadow.candidate_adapter import intents_for_session
from scripts.portfolio_shadow.cli import (_ensure_reviewed, _forward_calendar, _next_session,
                                          _settle_cost,
                                          _settle_intents, _settle_marks,
                                          apply_entry_reviews, drop_from_schedule,
                                          entry_packet_for, freeze_entry_reviews,
                                          manifest_from_dict, record_data_blocked)
from scripts.portfolio_shadow.evidence import (entry_market_cutoff,
                                               entry_response_deadline)
from scripts.portfolio_shadow.entry_review import decision_id_for
from scripts.portfolio_shadow.evidence_source import (JsonlEvidenceSource,
                                                     import_evidence_jsonl)
from scripts.portfolio_shadow.llm_overlay import FakeModel, SCHEMA_VERSION
from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.schema import Application, Manifest, Opportunity, to_micro
from scripts.portfolio_shadow.store import ShadowStore

SIGNAL = pd.Timestamp('2026-01-05')
EXEC = pd.Timestamp('2026-01-06')


def manifest():
    return Manifest(
        experiment_id='exp1', status='DRAFT', parent_strategy_id='B3', parent_version='1',
        parent_code_hash='abc', universe_id='u', universe_hash='uh',
        account_scopes=('SHADOW:exp1:R', 'SHADOW:exp1:L'), initial_cash=to_micro(100000),
        risk_policy={'single_position_risk_bp': 100, 'max_weight_bp': 2000, 'max_positions': 5},
        execution_policy={'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': 3},
        llm_policy={'overlay': 'entry_veto', 'evidence_mode': 'strict',
                                 'evidence_window_days': 30, 'evidence_max_events': 50}, calendar_version='v1',
        evaluation_protocol={'main_metric': 'L_minus_R_return', 'enrollment_window': '3-6 months',
                             'review_date': '2026-12-31', 'cost_allocation': 'L_pays_model_cost'}
    ).freeze('2026-01-02')


def opp(sid='SEC-A', planned='2026-01-06'):
    return Opportunity(experiment_id='exp1', security_id=sid, source_candidate_id=sid,
                       parent_version='1', signal_session='2026-01-05',
                       observed_at='2026-01-05T21:00:00+00:00',
                       planned_execution_session=planned, rank=1, entry_rule='b3',
                       stop_reference={'atr14_micro': to_micro(2.0)}, exit_policy_id='H60',
                       input_hash='h', terminal='READY')


def event_row(sid='SEC-A', *, published='2026-01-04T12:00:00Z', **extra):
    """一条真实事件（设计 §5.1 的导入格式）。"""
    return {'security_id': sid, 'event_type': 'filing', 'summary': '公司下调全年指引',
            'excerpt': '指引下调原文摘录', 'source_url': 'file://x', 'source_type': 'filing',
            'published_at': published, 'quality_status': 'verified', **extra}


def make_source(tmp, rows=None, *, ingested_at='2026-01-04T13:00:00+00:00',
                window_days=30, max_events=50):
    """走**真实导入路径**：JSONL → import_evidence_jsonl → JsonlEvidenceSource。

    `observed_at` 由导入那一步写死（= ingested_at），不由 JSONL 追溯指定。
    """
    src = Path(tmp) / 'events.jsonl'
    src.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in (rows or [event_row()])),
                   encoding='utf-8')
    store = Path(tmp) / 'evidence.csv'
    import_evidence_jsonl(src, store, ingested_at=ingested_at)
    return JsonlEvidenceSource(store, window_days=window_days, max_events=max_events)


class OverlayForwardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.store.save_experiment(manifest())
        self.scope = 'SHADOW:exp1:L'
        self.quotes = {'SEC-A': {'price': to_micro(100), 'observed_at': '2026-01-04T21:00:00Z'}}

    def _run(self, model, source=None, notes=None, quotes=None, force_recall=False,
             scopes=None, collected_at=None):
        """走生产同一条两阶段路径：先冻结证据包+质量分流，再对非 BLOCK 的做评审。"""
        notes = notes or [opp()]
        quotes = self.quotes if quotes is None else quotes
        market_cutoff = entry_market_cutoff(SIGNAL)
        deadline = entry_response_deadline(EXEC)
        reviews, blocked = freeze_entry_reviews(
            self.store, notes, quotes=quotes, source=source,
            market_cutoff=market_cutoff, deadline=deadline, evidence_mode='strict',
            collected_at=collected_at or market_cutoff)
        for o, packet in blocked:
            record_data_blocked(self.store, scopes or (self.scope,), o, packet,
                                session='2026-01-05')
        return apply_entry_reviews(
            self.store, self.scope, reviews, deadline=deadline, real_model=model,
            force_recall=force_recall)

    def test_no_evidence_abstains_free_and_keeps_intent(self):
        model = Mock()
        kept, cost, uncertain = self._run(model)
        self.assertEqual([o.opportunity_id() for o in kept], [opp().opportunity_id()])
        self.assertEqual((cost, uncertain), (0, []))
        model.call.assert_not_called()          # 无证据不发起调用 ⇒ 无费用
        app = self.store.application(self.scope, opp().opportunity_id())
        self.assertEqual(app['action'], 'ABSTAIN')
        self.assertEqual(app['reason_code'], 'INSUFFICIENT_EVIDENCE')

    def test_missing_quote_blocks_both_accounts(self):
        """设计 §6：DATA_BLOCKED 同时禁止 R 与 L 的新风险，不是在某一侧剔除。"""
        model = Mock()
        kept, cost, _ = self._run(model, quotes={},
                                  scopes=('SHADOW:exp1:R', 'SHADOW:exp1:L'))
        self.assertEqual(kept, [])
        self.assertEqual(cost, 0)
        model.call.assert_not_called()
        for scope in ('SHADOW:exp1:R', 'SHADOW:exp1:L'):
            app = self.store.application(scope, opp().opportunity_id())
            self.assertEqual(app['action'], 'DATA_BLOCKED')
            self.assertIn('quote.price', app['reason_code'])
        self.assertEqual(
            self.store.opportunity_terminals()[opp().opportunity_id()], 'DATA_BLOCKED')

    def test_evidence_as_of_is_collection_time_not_session_close(self):
        """设计 §3.2：证据 as_of = 实际采集时刻，不是信号日收盘。"""
        collected = '2026-01-05T23:00:00+00:00'  # 收盘(21:00Z)之后、开盘前截止之前
        self._run(Mock(), collected_at=collected)
        app = self.store.application(self.scope, opp().opportunity_id())
        self.assertEqual(app['as_of'], collected)
        self.assertGreater(app['as_of'], entry_market_cutoff(SIGNAL))
        self.assertLess(app['as_of'], entry_response_deadline(EXEC))

    def test_post_close_event_is_visible_to_the_decision(self):
        """盘后发布的事件必须能进包 —— 那正是财报最常发布的时段。

        回归：把证据 as_of 当成信号日收盘，会把 published_at 晚于收盘的事件系统性
        排除在决策之外。
        """
        source = make_source(self.tmp, [event_row(published='2026-01-05T21:30:00Z')],
                             ingested_at='2026-01-05T22:00:00+00:00')
        self._run(FakeModel(action='PASS', cost_micro=0), source=source,
                  collected_at='2026-01-05T23:00:00+00:00')
        packet = self.store.packet_for_opportunity(opp().opportunity_id())
        self.assertEqual(len(packet['events']), 1)
        self.assertEqual(packet['events'][0]['summary'], '公司下调全年指引')

    def test_veto_with_evidence_drops_from_l(self):
        packet_probe = {}
        model = FakeModel(action='VETO', reason_code='MATERIAL_COMPANY_EVENT_RISK',
                          evidence_ids=[], cost_micro=250)

        def _passthrough(packet, deadline):
            packet_probe.update(packet)
            return model.call(packet, deadline)

        stub = Mock()
        stub.call.side_effect = _passthrough
        source = make_source(self.tmp)
        fetch = source.load_events('SEC-A', entry_market_cutoff(SIGNAL))
        model.evidence_ids = [fetch.events[0]['evidence_id']]
        kept, cost, _ = self._run(stub, source=source)
        self.assertEqual(kept, [])              # VETO ⇒ L 不建仓
        self.assertEqual(cost, 250)
        app = self.store.application(self.scope, opp().opportunity_id())
        self.assertEqual(app['action'], 'VETO')
        self.assertEqual(app['reason_code'], 'MATERIAL_COMPANY_EVENT_RISK')
        # 包内确实带上了可读正文，VETO 才有依据
        frozen = self.store.packet_for_opportunity(opp().opportunity_id())
        self.assertEqual(frozen['events'][0]['summary'], '公司下调全年指引')
        self.assertEqual(frozen['events'][0]['excerpt'], '指引下调原文摘录')

    def test_rerun_reuses_recorded_decision_without_second_call(self):
        model = FakeModel(action='PASS', cost_micro=500)
        stub = Mock()
        stub.call.side_effect = lambda packet, deadline: model.call(packet, deadline)
        self._run(stub, source=make_source(self.tmp))
        first = stub.call.call_count
        self.assertEqual(first, 1)
        # 重跑同一 session：必须复用已落库决定，不得重复付费
        self._run(stub, source=make_source(self.tmp))
        self.assertEqual(stub.call.call_count, first)

    def test_unknown_cost_is_surfaced_not_swallowed(self):
        model = FakeModel(action='PASS', cost_micro=None, cost_uncertain=True)
        stub = Mock()
        stub.call.side_effect = lambda packet, deadline: model.call(packet, deadline)
        kept, cost, uncertain = self._run(stub, source=make_source(self.tmp))
        self.assertEqual(cost, 0)               # 尚未计入，不是免费
        self.assertEqual(len(uncertain), 1)
        app = self.store.application(self.scope, opp().opportunity_id())
        self.assertTrue(app['cost_uncertain'])
        self.assertEqual(app['attempt_id'], uncertain[0])


class CrashWindowTests(unittest.TestCase):
    """已领取但无结果的调用不得静默重发（设计 §7）——「不盲目重发」是硬要求。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.store.save_experiment(manifest())
        self.scope = 'SHADOW:exp1:L'
        self.quotes = {'SEC-A': {'price': to_micro(100), 'observed_at': '2026-01-04T21:00:00Z'}}
        self.oid = opp().opportunity_id()
        self.decision_id = self._decision_id()

    def _decision_id(self):
        fetch = make_source(self.tmp).load_events('SEC-A', entry_market_cutoff(SIGNAL))
        packet = entry_packet_for(opp(), self.quotes['SEC-A'], fetch,
                                  entry_market_cutoff(SIGNAL))
        return decision_id_for(self.store.experiment_id, self.scope, self.oid,
                               packet['packet_id'], '')

    def _run(self, model, force_recall=False):
        market_cutoff = entry_market_cutoff(SIGNAL)
        deadline = entry_response_deadline(EXEC)
        reviews, blocked = freeze_entry_reviews(
            self.store, [opp()], quotes=self.quotes, source=make_source(self.tmp),
            market_cutoff=market_cutoff, deadline=deadline, evidence_mode='strict',
            collected_at=market_cutoff)
        self.assertEqual(blocked, [])
        return apply_entry_reviews(
            self.store, self.scope, reviews, deadline=deadline, real_model=model,
            force_recall=force_recall)

    def test_abandoned_attempt_is_never_recalled_and_cost_is_flagged(self):
        """租约过期 = 进程在发送后崩溃。钱可能已花且金额不可知 → 不重发，挂账待补记。"""
        self.store.put_job_run(self.decision_id, 1, 'CALL_STARTED', {'packet_id': 'p'},
                               fencing_token='2020-01-01T00:00:00+00:00')
        model = Mock()
        kept, cost, uncertain = self._run(model)
        model.call.assert_not_called()          # 绝不重试付费
        self.assertEqual(cost, 0)               # 金额不可知 → 挂账
        self.assertEqual(uncertain, [self.decision_id])
        app = self.store.application(self.scope, self.oid)
        self.assertEqual(app['action'], 'ABSTAIN')       # 采用父策略
        self.assertEqual(app['reason_code'], 'RECALL_ABANDONED')
        self.assertTrue(app['cost_uncertain'])
        self.assertTrue(app['decision_frozen'])
        self.assertFalse(app['execution_applied'])       # 冻结 ≠ 成交

    def test_force_recall_does_not_bypass_the_abandoned_guard(self):
        self.store.put_job_run(self.decision_id, 1, 'CALL_STARTED', {'packet_id': 'p'},
                               fencing_token='2020-01-01T00:00:00+00:00')
        model = Mock()
        self._run(model, force_recall=True)
        model.call.assert_not_called()

    def test_in_flight_attempt_is_not_duplicated_nor_frozen(self):
        """租约仍有效：另一个 worker 正在调用 → 不重复调用，也不替它冻结。"""
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.store.put_job_run(self.decision_id, 1, 'CALL_STARTED', {'packet_id': 'p'},
                               fencing_token=future)
        model = Mock()
        kept, cost, _ = self._run(model)
        model.call.assert_not_called()
        self.assertEqual(cost, 0)
        self.assertIsNone(self.store.application(self.scope, self.oid))
        self.assertEqual([o.opportunity_id() for o in kept], [self.oid])  # 仍按父策略走

    def test_completed_attempt_is_recovered_without_second_call(self):
        self.store.put_job_run(self.decision_id, 1, 'COMPLETED', {
            'opportunity_id': self.oid, 'packet_id': 'p', 'action': 'VETO',
            'reason_code': 'MATERIAL_COMPANY_EVENT_RISK', 'model_cost': 777,
            'cost_uncertain': False, 'raw_action': 'VETO', 'late_response_observed': False})
        model = Mock()
        kept, cost, _ = self._run(model)
        model.call.assert_not_called()          # 从尝试记录恢复，不重调
        self.assertEqual(cost, 777)
        self.assertEqual(kept, [])              # 恢复的是 VETO
        self.assertEqual(self.store.application(self.scope, self.oid)['action'], 'VETO')

    def test_different_packet_on_rerun_raises_instead_of_reusing(self):
        """packet 变了就是另一个问题：不复用（答非所问），也不重算（改写依据）。"""
        self.store.put_application(Application(
            scope=self.scope, opportunity_id=self.oid, action='VETO',
            reason_code='MATERIAL_COMPANY_EVENT_RISK', decision_id='decision_stale',
            as_of='x', decision_frozen=True))
        with self.assertRaises(ValueError) as ctx:
            self._run(Mock())
        self.assertIn('PACKET_CHANGED_ON_RERUN', str(ctx.exception))

    def test_no_attempt_is_recorded_when_the_gate_short_circuits(self):
        # 无证据 ⇒ 质量门短路，不应留下任何调用尝试
        market_cutoff = entry_market_cutoff(SIGNAL)
        deadline = entry_response_deadline(EXEC)
        reviews, _ = freeze_entry_reviews(
            self.store, [opp()], quotes=self.quotes, source=None,
            market_cutoff=market_cutoff, deadline=deadline, evidence_mode='strict',
            collected_at=market_cutoff)
        apply_entry_reviews(self.store, self.scope, reviews, deadline=deadline,
                            real_model=Mock())
        self.assertIsNone(self.store.job_run(self.decision_id))


class ManifestLoadingTests(unittest.TestCase):
    """manifest.json 顶层键严格校验：拼错的键不能让安全门无声失效。"""

    def _base(self):
        return {'experiment_id': 'exp1', 'parent_strategy_id': 'B3', 'parent_version': '1',
                'parent_code_hash': 'abc', 'universe_id': 'u', 'universe_hash': 'uh',
                'account_scopes': ['SHADOW:exp1:R', 'SHADOW:exp1:L'], 'initial_cash': 100000,
                'risk_policy': {'single_position_risk_bp': 100, 'max_weight_bp': 2000,
                                'max_positions': 5},
                'execution_policy': {'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': 60},
                'llm_policy': {'overlay': 'fixed_pass'}, 'calendar_version': 'v1',
                'evaluation_protocol': {'main_metric': 'L_minus_R_return',
                                        'enrollment_window': '3-6 months',
                                        'review_date': '2026-12-31',
                                        'cost_allocation': 'L_pays_model_cost'}}

    def test_valid_dict_loads_and_validates(self):
        self.assertEqual(manifest_from_dict(self._base()).validate(), [])

    def test_misspelled_top_level_field_is_named_not_silently_ignored(self):
        d = self._base()
        d['llm_polcy'] = d.pop('llm_policy')       # 错字：原先是裸 KeyError
        with self.assertRaises(ValueError) as ctx:
            manifest_from_dict(d)
        self.assertIn('MANIFEST_UNKNOWN_FIELDS', str(ctx.exception))
        self.assertIn('llm_polcy', str(ctx.exception))

    def test_missing_required_field_gives_a_clear_error(self):
        d = self._base()
        del d['calendar_version']
        with self.assertRaises(ValueError) as ctx:
            manifest_from_dict(d)
        self.assertIn('MANIFEST_MISSING_FIELDS', str(ctx.exception))
        self.assertIn('calendar_version', str(ctx.exception))


class FrozenPacketTests(unittest.TestCase):
    """同一机会只有一个冻结证据包（设计 §3）：重跑不得用今天的信息改写当时的决策依据。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.store.save_experiment(manifest())
        self.quotes = {'SEC-A': {'price': to_micro(100), 'observed_at': '2026-01-04T21:00:00Z'}}

    def _freeze(self, collected):
        market = entry_market_cutoff(SIGNAL)
        deadline = entry_response_deadline(EXEC)
        reviews, _ = freeze_entry_reviews(
            self.store, [opp()], quotes=self.quotes, source=make_source(self.tmp),
            market_cutoff=market, deadline=deadline, evidence_mode='strict',
            collected_at=collected)
        return reviews[0][1]

    def test_packet_is_frozen_on_first_write(self):
        first = self._freeze('2026-01-05T22:00:00+00:00')
        second = self._freeze('2026-01-05T23:30:00+00:00')  # 重跑，采集时刻不同
        self.assertEqual(first['packet_id'], second['packet_id'])
        self.assertEqual(second['as_of'], '2026-01-05T22:00:00+00:00')  # 仍是首次冻结的值

    def test_store_keeps_the_whole_packet_not_just_a_hash(self):
        """设计 §7：不能只保存 packet_hash 及最终 action。"""
        frozen = self._freeze('2026-01-05T22:00:00+00:00')
        stored = self.store.packet_for_opportunity(opp().opportunity_id())
        self.assertEqual(stored, frozen)
        for key in ('events', 'rule_plan', 'identity', 'market_context', 'provenance',
                    'data_quality'):
            self.assertIn(key, stored)


class CommandSeparationTests(unittest.TestCase):
    """设计 §9：决策与结算分离。actions 必须在执行日之前冻结，settle 不得调用模型。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = Path(self.tmp) / 'out'

    def _manifest_file(self, **llm_overrides):
        llm = {'overlay': 'entry_veto', 'evidence_mode': 'strict',
               'evidence_window_days': 30, 'evidence_max_events': 50}
        llm.update(llm_overrides)
        d = {'experiment_id': 'exp1', 'parent_strategy_id': 'B3', 'parent_version': '1',
             'parent_code_hash': 'abc', 'universe_id': 'u', 'universe_hash': 'uh',
             'account_scopes': ['SHADOW:exp1:R', 'SHADOW:exp1:L'], 'initial_cash': 100000,
             'risk_policy': {'single_position_risk_bp': 100, 'max_weight_bp': 2000,
                             'max_positions': 5},
             'execution_policy': {'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': 60},
             'llm_policy': llm, 'calendar_version': 'v1',
             'evaluation_protocol': {'main_metric': 'L_minus_R_return',
                                     'enrollment_window': '3-6 months',
                                     'review_date': '2026-12-31',
                                     'cost_allocation': 'L_pays_model_cost'}}
        path = Path(self.tmp) / 'manifest.json'
        path.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
        return path

    def test_run_forward_refuses_real_model(self):
        """过去的执行日不能用今天生成的模型结果补填前瞻记录。"""
        from types import SimpleNamespace
        from scripts.portfolio_shadow.cli import cmd_run_forward
        args = SimpleNamespace(manifest=str(self._manifest_file(use_real_model=True,
                                                                knowledge_cutoff='unknown')),
                               output=str(self.out), to_session='2026-01-06', evidence=None)
        with self.assertRaises(ValueError) as ctx:
            cmd_run_forward(args)
        self.assertIn('RUN_FORWARD_FORBIDS_REAL_MODEL', str(ctx.exception))

    def test_review_entries_refuses_real_model_when_manifest_disallows_it(self):
        from types import SimpleNamespace
        from scripts.portfolio_shadow.cli import cmd_review_entries
        import json as _json
        manifest_path = self._manifest_file()
        m = manifest_from_dict(_json.loads(manifest_path.read_text())).freeze('2026-01-02')
        store_dir = self.out / m.experiment_id
        store_dir.mkdir(parents=True, exist_ok=True)
        ShadowStore(store_dir / 'ledger.sqlite3', m.experiment_id).save_experiment(m)
        args = SimpleNamespace(manifest=str(manifest_path), output=str(self.out),
                               execution_session='2026-01-06', model='real')
        with self.assertRaises(ValueError) as ctx:
            cmd_review_entries(args)
        self.assertIn('MANIFEST_DOES_NOT_ALLOW_REAL_MODEL', str(ctx.exception))

    def test_review_entries_requires_a_prepared_packet(self):
        """动作只能在已冻结的证据包上做：没有包就必须先去 prepare，不能临时现造。"""
        from types import SimpleNamespace
        from scripts.portfolio_shadow.cli import cmd_review_entries
        import json as _json
        manifest_path = self._manifest_file()
        m = manifest_from_dict(_json.loads(manifest_path.read_text())).freeze('2026-01-02')
        store_dir = self.out / m.experiment_id
        store_dir.mkdir(parents=True, exist_ok=True)
        store = ShadowStore(store_dir / 'ledger.sqlite3', m.experiment_id)
        store.save_experiment(m)
        store.put_opportunity(opp())
        args = SimpleNamespace(manifest=str(manifest_path), output=str(self.out),
                               execution_session='2026-01-06', model='fixture')
        with self.assertRaises(ValueError) as ctx:
            cmd_review_entries(args)
        self.assertIn('PACKET_NOT_PREPARED', str(ctx.exception))


class MarkAppliedTests(unittest.TestCase):
    """动作冻结 ≠ 成交（设计 §7）：结算时才标记 applied，且被引擎拒掉的不算。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.store.save_experiment(manifest())

    def _res(self, events=()):
        from types import SimpleNamespace
        return SimpleNamespace(events=list(events))

    def test_creates_intent_created_when_the_scope_has_no_application(self):
        """回归：R 侧没有模型决策，但账户动作仍要留痕（设计 §6 的 INTENT_CREATED）。"""
        from scripts.portfolio_shadow.cli import _mark_applied
        _mark_applied(self.store, 'SHADOW:exp1:R', self._res(), [opp()], '2026-01-06')
        a = self.store.application('SHADOW:exp1:R', opp().opportunity_id())
        self.assertEqual(a['action'], 'INTENT_CREATED')
        self.assertTrue(a['decision_frozen'])
        self.assertTrue(a['execution_applied'])

    def test_existing_frozen_action_is_marked_applied(self):
        from scripts.portfolio_shadow.cli import _mark_applied
        oid = opp().opportunity_id()
        self.store.put_application(Application(
            scope='SHADOW:exp1:L', opportunity_id=oid, action='ABSTAIN',
            reason_code='INSUFFICIENT_EVIDENCE', decision_id='d1', as_of='x',
            decision_frozen=True, execution_applied=False))
        _mark_applied(self.store, 'SHADOW:exp1:L', self._res(), [opp()], '2026-01-06')
        self.assertTrue(self.store.application('SHADOW:exp1:L', oid)['execution_applied'])

    def test_missed_intent_is_not_marked_applied(self):
        """冻结了但被引擎拒掉 —— 正是要和「成交」区分开的那种情况。"""
        from scripts.portfolio_shadow.cli import _mark_applied
        oid = opp().opportunity_id()
        self.store.put_application(Application(
            scope='SHADOW:exp1:L', opportunity_id=oid, action='ABSTAIN',
            reason_code='INSUFFICIENT_EVIDENCE', decision_id='d1', as_of='x',
            decision_frozen=True, execution_applied=False))
        res = self._res([{'type': 'missed', 'opportunity_id': oid,
                          'reason': 'MAX_POSITIONS'}])
        _mark_applied(self.store, 'SHADOW:exp1:L', res, [opp()], '2026-01-06')
        self.assertFalse(self.store.application('SHADOW:exp1:L', oid)['execution_applied'])


class SettleAccountingTests(unittest.TestCase):
    """用户 review 的三项验收阻塞：VETO 成本漏记、未评审静默跳过、归因与成交不原子。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.m = manifest().freeze('2026-01-02')
        self.store.save_experiment(self.m)
        self.L = 'SHADOW:exp1:L'
        self.R = 'SHADOW:exp1:R'
        self.deadline = entry_response_deadline('2026-01-06')

    def _opp(self, sid='SEC-A'):
        o = opp(sid, '2026-01-06')
        self.store.put_opportunity(o)
        return o

    def _app(self, scope, oid, action, **kw):
        self.store.put_application(Application(
            scope=scope, opportunity_id=oid, action=action,
            reason_code=kw.get('reason', 'R'), decision_id=kw.get('decision_id', ''),
            as_of='2026-01-05T23:00:00Z', decision_frozen=True, execution_applied=False,
            model_cost=kw.get('model_cost', 0),
            cost_uncertain=kw.get('cost_uncertain', False),
            attempt_id=kw.get('attempt_id', '')))

    # ---- 问题 2：模型成本必须独立于是否交易入账 ----

    def test_veto_cost_is_accounted(self):
        o = self._opp()
        oid = o.opportunity_id()
        self._app(self.L, oid, 'VETO', model_cost=123, decision_id='d1')
        resolved = {oid: self.store.application(self.L, oid)}
        cost, _ = _settle_cost(resolved)
        self.assertEqual(cost, 123)                    # 否决了，但调用过的钱要记
        self.assertEqual(_settle_intents([o], resolved), [])   # 同时不执行

    def test_veto_uncertain_cost_is_surfaced(self):
        o = self._opp()
        oid = o.opportunity_id()
        self._app(self.L, oid, 'VETO', cost_uncertain=True, attempt_id='d1')
        cost, uncertain = _settle_cost({oid: self.store.application(self.L, oid)})
        self.assertEqual(cost, 0)
        self.assertEqual(uncertain, ['d1'])

    def test_data_blocked_carries_no_cost_and_does_not_trade(self):
        o = self._opp()
        oid = o.opportunity_id()
        self._app(self.L, oid, 'DATA_BLOCKED')
        resolved = {oid: self.store.application(self.L, oid)}
        self.assertEqual(_settle_cost(resolved), (0, []))
        self.assertEqual(_settle_intents([o], resolved), [])

    # ---- 问题 3：未评审时按截止分别处理，不静默改变策略 ----

    def test_past_deadline_without_action_freezes_abstain_explicitly(self):
        o = self._opp()
        app = _ensure_reviewed(self.store, self.L, o, self.deadline,
                               '2026-01-06T15:00:00+00:00')
        self.assertEqual(app['action'], 'ABSTAIN')
        self.assertEqual(app['reason_code'], 'DECISION_DEADLINE_MISSED')
        self.assertTrue(app['decision_frozen'])
        self.assertFalse(app['execution_applied'])
        # 不是静默跳过：账目上留下了这条
        self.assertIsNotNone(self.store.application(self.L, o.opportunity_id()))

    def test_before_deadline_without_action_blocks_settlement(self):
        o = self._opp()
        with self.assertRaises(ValueError) as ctx:
            _ensure_reviewed(self.store, self.L, o, self.deadline,
                             '2026-01-06T13:00:00+00:00')
        self.assertIn('SETTLEMENT_BLOCKED_DECISION_PENDING', str(ctx.exception))
        self.assertIsNone(self.store.application(self.L, o.opportunity_id()))

    def test_existing_frozen_action_is_returned_unchanged(self):
        o = self._opp()
        oid = o.opportunity_id()
        self._app(self.L, oid, 'VETO')
        app = _ensure_reviewed(self.store, self.L, o, self.deadline,
                               '2026-01-07T00:00:00+00:00')
        self.assertEqual(app['action'], 'VETO')

    # ---- 问题 4：归因与成交同事务 ----

    def _run(self):
        state = new_account_state(self.L, self.m.initial_cash)
        return step(state, session='2026-01-06', bars={}, corporate_actions=[],
                    intents=[], manifest=self.m)

    def test_marks_are_committed_with_the_state(self):
        o = self._opp()
        oid = o.opportunity_id()
        self._app(self.L, oid, 'ABSTAIN')
        res = self._run()
        self.store.save_state(self.L, res.state, res.nav, res.events, session='2026-01-06',
                              applied_marks=[{'opportunity_id': oid, 'create': None}])
        self.assertTrue(self.store.application(self.L, oid)['execution_applied'])

    def test_crash_between_state_and_marks_is_healed_on_rerun(self):
        """模拟「状态已提交、标记未写」的崩溃：重跑的幂等分支要把标记补上。

        分开两次提交会留下**永久的决策—成交归因缺口**：账户已成交而标记未写时崩溃，
        重跑时 step 返回 no-op 直接跳过，缺口再也补不上。
        """
        o = self._opp()
        oid = o.opportunity_id()
        self._app(self.L, oid, 'ABSTAIN')
        res = self._run()
        self.store.save_state(self.L, res.state, res.nav, res.events, session='2026-01-06')
        self.assertFalse(self.store.application(self.L, oid)['execution_applied'])
        again = step(res.state, session='2026-01-06', bars={}, corporate_actions=[],
                     intents=[], manifest=self.m)
        self.assertIsNone(again.nav)                    # 账户不再推进
        self.store.save_state(self.L, again.state, None, [], session='2026-01-06',
                              applied_marks=[{'opportunity_id': oid, 'create': None}])
        self.assertTrue(self.store.application(self.L, oid)['execution_applied'])  # 补齐

    def test_executed_opportunities_reads_persisted_fills(self):
        """恢复要用**已落库的成交**判断谁真的成交了，而不是拿内存里的 intents 猜。"""
        o = self._opp()
        res = self._run()
        self.store.save_state(self.L, res.state, res.nav, res.events, session='2026-01-06')
        self.assertEqual(self.store.executed_opportunities(self.L, '2026-01-06'), set())

    def test_settle_marks_skip_missed_intents(self):
        from types import SimpleNamespace
        o = self._opp()
        oid = o.opportunity_id()
        self._app(self.L, oid, 'ABSTAIN')
        res = SimpleNamespace(events=[{'type': 'missed', 'opportunity_id': oid}])
        self.assertEqual(_settle_marks(self.store, self.L, res, [o], '2026-01-06'), [])

    def test_settle_marks_create_intent_created_for_unrecorded_scope(self):
        from types import SimpleNamespace
        o = self._opp()
        marks = _settle_marks(self.store, self.R, SimpleNamespace(events=[]), [o],
                              '2026-01-06')
        self.assertEqual(marks[0]['create'].action, 'INTENT_CREATED')


class ForwardCalendarTests(unittest.TestCase):
    """前向运行必须能确定「明天」——价格序列里只有过去。"""

    def test_price_derived_calendar_cannot_know_tomorrow(self):
        import pandas as pd
        cal = pd.DatetimeIndex(pd.to_datetime(['2026-09-15', '2026-09-16']))
        self.assertIsNone(_next_session(cal, '2026-09-16'))

    def test_forward_calendar_supplies_the_next_session(self):
        """设计 §3.1：T+1 由日历得到，不要求已取得 T+1 开盘价。"""
        import pandas as pd
        cal = pd.DatetimeIndex(pd.to_datetime(['2026-09-15', '2026-09-16']))
        fwd = _forward_calendar(cal, '2026-09-16')
        self.assertEqual(_next_session(fwd, '2026-09-16'), '2026-09-17')   # 周四
        self.assertEqual(_next_session(fwd, '2026-09-18'), '2026-09-21')   # 跳过周末
        # 实际日线覆盖临时休市：规则历里没有的日子也能由价格序列补进来
        union = _forward_calendar(pd.DatetimeIndex(pd.to_datetime(['2026-09-17'])), '2026-09-16')
        self.assertIn(pd.Timestamp('2026-09-17'), union)
