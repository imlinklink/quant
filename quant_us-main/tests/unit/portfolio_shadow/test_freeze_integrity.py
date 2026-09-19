"""冻结完整性：盘上的 manifest 与冻结时不一致时，所有运行入口都必须拒绝。

冻结不是仪式。改一个风险参数或起始日，实验身份没变、账本里的哈希也没变，但跑出来的
数字已经不是同一把尺子。本文件覆盖用户指定的验收项：参数篡改、起始日修改、重复 freeze、
缺冻结记录、各入口绕过；并断言**拒绝之后没有模型调用、账本没有变化**。
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.portfolio_shadow.cli import (cmd_prepare_entry_reviews,
                                          cmd_prepare_portfolio_review,
                                          cmd_prepare_position_reviews,
                                          cmd_review_entries, cmd_review_portfolio,
                                          cmd_review_positions,
                                          cmd_run_daily, cmd_run_forward, cmd_run_session,
                                          cmd_settle_session, manifest_from_dict)
from scripts.portfolio_shadow.store import ShadowStore

OUT = None


def draft(experiment_id='EXP'):
    return {
        'experiment_id': experiment_id,
        'parent_strategy_id': 'B3', 'parent_version': '1', 'parent_code_hash': 'abc',
        'universe_id': 'u', 'universe_hash': 'uh',
        'account_scopes': [f'SHADOW:{experiment_id}:R', f'SHADOW:{experiment_id}:L'],
        'initial_cash': 100000,
        'risk_policy': {'single_position_risk_bp': 100, 'max_weight_bp': 2000,
                        'max_positions': 5},
        'execution_policy': {'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': 60},
        'llm_policy': {'overlay': 'entry_veto', 'evidence_mode': 'strict',
                       'evidence_window_days': 30, 'evidence_max_events': 50},
        'calendar_version': 'v1',
        'evaluation_protocol': {'main_metric': 'L_minus_R_return',
                                'enrollment_window': '1-3 months',
                                'review_date': '2026-12-31',
                                'cost_allocation': 'L_pays_model_cost'},
    }


class FreezeIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = Path(self.tmp) / 'out'
        self.draft_path = Path(self.tmp) / 'manifest.json'
        self.draft_path.write_text(json.dumps(draft()), encoding='utf-8')
        from scripts.portfolio_shadow.cli import cmd_freeze
        cmd_freeze(SimpleNamespace(manifest=str(self.draft_path),
                                   start_session='2026-01-02', output=str(self.out)))
        self.manifest = self.out / 'EXP' / 'manifest.json'
        self.store = ShadowStore(self.out / 'EXP' / 'ledger.sqlite3', 'EXP')

    # ---- 账本快照：拒绝后必须一字未动 ----

    def _snapshot(self):
        import sqlite3
        con = sqlite3.connect(str(self.out / 'EXP' / 'ledger.sqlite3'))
        out = {}
        for table in ('shadow_job_runs', 'shadow_applications', 'shadow_opportunities',
                      'shadow_account_state', 'shadow_packets', 'decision_events'):
            out[table] = con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        con.close()
        return out

    def _tamper(self, mutate):
        data = json.loads(self.manifest.read_text())
        mutate(data)
        self.manifest.write_text(json.dumps(data), encoding='utf-8')

    def _entries(self):
        """所有运行入口。它们都必须在**调用模型与写账本之前**拒绝。"""
        base = dict(manifest=str(self.manifest), output=str(self.out))
        return {
            'run-daily': lambda: cmd_run_daily(SimpleNamespace(
                **base, session=None, evidence=None, etf_raw=None, model='fixture',
                fixture_action='PASS')),
            'prepare': lambda: cmd_prepare_entry_reviews(SimpleNamespace(
                **base, session='2026-01-06', evidence=None, etf_raw=None)),
            'review': lambda: cmd_review_entries(SimpleNamespace(
                **base, execution_session='2026-01-06', model='fixture',
                fixture_action='PASS')),
            'prepare-position': lambda: cmd_prepare_position_reviews(SimpleNamespace(
                **base, session='2026-01-06', evidence=None, etf_raw=None)),
            'review-position': lambda: cmd_review_positions(SimpleNamespace(
                **base, execution_session='2026-01-06', model='fixture',
                fixture_action='hold')),
            'prepare-portfolio': lambda: cmd_prepare_portfolio_review(SimpleNamespace(
                **base, session='2026-01-06', etf_raw=None)),
            'review-portfolio': lambda: cmd_review_portfolio(SimpleNamespace(
                **base, execution_session='2026-01-06', model='fixture')),
            'settle': lambda: cmd_settle_session(SimpleNamespace(
                **base, session='2026-01-06', etf_raw=None)),
            'run-session': lambda: cmd_run_session(SimpleNamespace(
                **base, session='2026-01-06', schedule=str(self.draft_path))),
            'run-forward': lambda: cmd_run_forward(SimpleNamespace(
                **base, to_session='2026-01-06', evidence=None)),
        }

    def _assert_all_rejected(self, marker):
        for name, call in self._entries().items():
            before = self._snapshot()
            with self.subTest(entry=name):
                with self.assertRaises(ValueError) as ctx:
                    call()
                self.assertIn(marker, str(ctx.exception))
                # 拒绝之后：没有模型尝试、账本一字未动
                self.assertEqual(self._snapshot(), before, f'{name} 在拒绝前改动了账本')

    # ---- 验收项 ----

    def test_risk_parameter_tampering_is_rejected_everywhere(self):
        self._tamper(lambda d: d['risk_policy'].__setitem__('single_position_risk_bp', 500))
        self._assert_all_rejected('MANIFEST_CHANGED_SINCE_FREEZE')

    def test_initial_cash_tampering_is_rejected(self):
        """初始资金不会给已有账户充值，但会改变新账户初始化与所有以它为分母的绩效计算。"""
        self._tamper(lambda d: d.__setitem__('initial_cash', 1_000_000))
        self._assert_all_rejected('MANIFEST_CHANGED_SINCE_FREEZE')

    def test_start_session_change_is_rejected(self):
        """起始日必须在哈希内，否则改了它实验身份看起来毫无变化。"""
        self._tamper(lambda d: d.__setitem__('start_session', '2026-06-01'))
        self._assert_all_rejected('MANIFEST_CHANGED_SINCE_FREEZE')

    def test_llm_policy_tampering_is_rejected(self):
        self._tamper(lambda d: d['llm_policy'].__setitem__('evidence_window_days', 3650))
        self._assert_all_rejected('MANIFEST_CHANGED_SINCE_FREEZE')

    def test_missing_freeze_record_is_rejected_not_allowed(self):
        """没有冻结记录时默认放行，等于没有冻结。"""
        self.manifest.write_text(json.dumps(draft('UNFROZEN')), encoding='utf-8')
        for name, call in self._entries().items():
            with self.subTest(entry=name):
                with self.assertRaises(ValueError) as ctx:
                    call()
                self.assertIn('EXPERIMENT_NOT_FROZEN', str(ctx.exception))

    def test_status_is_not_part_of_the_frozen_identity(self):
        """暂停/恢复是运行状态，不该让实验看起来「被改过」；但要单独拦住。"""
        self.store.set_experiment_status('PAUSED', note='人工暂停')
        self._assert_all_rejected('EXPERIMENT_PAUSED')
        # 恢复后照常运行
        self.store.set_experiment_status('FROZEN')
        from scripts.portfolio_shadow.cli import verify_manifest_frozen
        verify_manifest_frozen(self.store, manifest_from_dict(   # 恢复后不再抛
            json.loads(self.manifest.read_text())))

    # ---- 重复 freeze ----

    def test_refreeze_with_same_config_is_idempotent(self):
        from scripts.portfolio_shadow.cli import cmd_freeze
        before = self.store.frozen_manifest_hash()
        cmd_freeze(SimpleNamespace(manifest=str(self.draft_path),
                                   start_session='2026-01-02', output=str(self.out)))
        self.assertEqual(self.store.frozen_manifest_hash(), before)

    def test_refreeze_with_changed_config_is_rejected(self):
        """参数变更必须新建 experiment_id，不能顶着同一身份改尺子。"""
        from scripts.portfolio_shadow.cli import cmd_freeze
        changed = draft()
        changed['risk_policy']['single_position_risk_bp'] = 500
        self.draft_path.write_text(json.dumps(changed), encoding='utf-8')
        with self.assertRaises(ValueError) as ctx:
            cmd_freeze(SimpleNamespace(manifest=str(self.draft_path),
                                       start_session='2026-01-02', output=str(self.out)))
        self.assertIn('EXPERIMENT_ALREADY_FROZEN', str(ctx.exception))

    def test_rejected_refreeze_does_not_overwrite_the_frozen_manifest_file(self):
        """原实现先写文件后校验 —— 拒绝反而把盘上的冻结产物改掉了。"""
        from scripts.portfolio_shadow.cli import cmd_freeze
        original = json.loads(self.manifest.read_text())
        changed = draft()
        changed['risk_policy']['single_position_risk_bp'] = 500
        self.draft_path.write_text(json.dumps(changed), encoding='utf-8')
        with self.assertRaises(ValueError):
            cmd_freeze(SimpleNamespace(manifest=str(self.draft_path),
                                       start_session='2026-01-02', output=str(self.out)))
        self.assertEqual(json.loads(self.manifest.read_text()), original)

    def test_refreeze_with_different_start_session_is_rejected(self):
        from scripts.portfolio_shadow.cli import cmd_freeze
        with self.assertRaises(ValueError) as ctx:
            cmd_freeze(SimpleNamespace(manifest=str(self.draft_path),
                                       start_session='2026-06-01', output=str(self.out)))
        self.assertIn('EXPERIMENT_ALREADY_FROZEN', str(ctx.exception))
