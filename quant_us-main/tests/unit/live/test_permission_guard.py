"""PermissionGuard 测试（§13.2/§7.2）。"""
import tempfile
import unittest
from pathlib import Path

from scripts.live_trading.decision_ledger.permission_guard import PermissionGuard
from scripts.live_trading.position_registry import PositionRegistry


class PermissionGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 'state.db', 'DRY-RUN')
        self.guard = PermissionGuard(self.registry)

    def _config(self, entry_review='shadow'):
        return {'llm_permissions': {'_default': 'shadow', 'entry_review': entry_review}}

    def _versions(self, prompt='entry-v1', model='m1'):
        return {'packet_schema': 'entry-v2', 'prompt': prompt, 'output_schema': 'entry-v2',
                'feature': 'feature-v2', 'rule': 'rule-v2', 'permission': 'permission-v2',
                'model_id': model}

    def test_snapshot_roundtrip(self):
        snap = self.guard.snapshot_permissions(self._config('shadow'), self._versions())
        key = self.guard.save_permission_snapshot('d1', snap)
        loaded = self.guard.load_permission_snapshot('d1')
        self.assertEqual(key, 'd1')
        self.assertEqual(loaded['permissions']['entry_review'], 'shadow')

    def test_lower_current_wins(self):
        snap = self.guard.snapshot_permissions(self._config('constrained_action'), self._versions())
        eff = self.guard.effective_level('entry_review', snap, self._config('shadow'), self._versions())
        self.assertEqual(eff['level'], 'shadow')

    def test_higher_current_uses_snapshot(self):
        snap = self.guard.snapshot_permissions(self._config('shadow'), self._versions())
        eff = self.guard.effective_level('entry_review', snap, self._config('constrained_action'),
                                         self._versions())
        self.assertEqual(eff['level'], 'shadow')

    def test_version_change_returns_shadow(self):
        snap = self.guard.snapshot_permissions(self._config('constrained_action'), self._versions())
        eff = self.guard.effective_level('entry_review', snap, self._config('constrained_action'),
                                         self._versions(prompt='entry-v2'))
        self.assertEqual(eff['level'], 'shadow')

    def test_most_restrictive_across_permissions(self):
        snap = self.guard.snapshot_permissions(
            {'llm_permissions': {'_default': 'shadow', 'exit_review': 'constrained_action',
                                 'auto_exit_thesis': 'shadow'}}, self._versions())
        r = self.guard.most_restrictive(['exit_review', 'auto_exit_thesis'], snap,
                                        {'llm_permissions': {'_default': 'shadow',
                                                             'exit_review': 'constrained_action',
                                                             'auto_exit_thesis': 'shadow'}},
                                        self._versions())
        self.assertEqual(r['level'], 'shadow')  # auto_exit_thesis shadow 拉低整体

    def test_detect_downgrade_triggers(self):
        triggers = self.guard.detect_downgrade(
            {'consecutive_data_quality_failures': 6, 'citation_error_rate': 0.2,
             'all_same_action': True},
            {'max_data_quality_failures': 5, 'max_citation_error_rate': 0.1})
        self.assertTrue(any('数据质量' in t for t in triggers))
        self.assertTrue(any('引用错误' in t for t in triggers))
        self.assertTrue(any('单一动作' in t for t in triggers))

    def test_detect_downgrade_clean(self):
        triggers = self.guard.detect_downgrade(
            {'consecutive_data_quality_failures': 0, 'citation_error_rate': 0.0},
            {'max_data_quality_failures': 5, 'max_citation_error_rate': 0.1})
        self.assertEqual(triggers, [])


if __name__ == '__main__':
    unittest.main()
