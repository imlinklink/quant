"""历史证券主数据 v2 的契约与质量门测试（技术设计 §2.2/§2.6）。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
FIX = Path(__file__).resolve().parents[2] / 'fixtures' / 'security_master_v2'

from scripts.data.security_master_v2 import (ACTION_COLUMNS, MASTER_COLUMNS, SYMBOL_COLUMNS,
                                             audit_master, build_outputs, canonical_record_hash,
                                             merge_sources, normalize_master, resolve_symbol,
                                             symbol_reuse_conflicts, validate_master,
                                             validate_symbols)
from scripts.data.source_archive import archive_source

BASE_ROW = {
    'security_id': 'SEC-900001', 'issuer_id': 'ISS-900001', 'asset_type': 'stock',
    'exchange': 'NASDAQ', 'currency': 'USD', 'valid_from': '2014-01-01', 'valid_to': '',
    'listed_at': '2014-01-01', 'source_record_id': 'm-9',
    'source_observed_at': '2014-01-01T12:05:00Z',
    'quality_status': 'verified',
}


def _archive(tmp, *source_ids, run_id='R1'):
    return [{'source_id': sid,
             'archive_dir': archive_source(FIX / sid, sid, run_id, Path(tmp) / 'archive')}
            for sid in source_ids]


class SecurityMasterV2Tests(unittest.TestCase):
    def test_build_outputs_merges_sources_and_flags_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            summary = build_outputs(_archive(tmp, 'source_a', 'source_b'), out)
            self.assertEqual(summary['master_rows'], 4)
            self.assertEqual(summary['symbol_rows'], 6)
            self.assertEqual(summary['action_rows'], 3)
            self.assertEqual(summary['master_errors'], [])
            self.assertEqual(summary['symbol_errors'], [])
            self.assertEqual(summary['conflicts'], 1)          # SEC-000004 exchange 冲突
            conflicts = pd.read_csv(out / 'master_conflicts.csv')
            self.assertEqual(conflicts.iloc[0]['security_id'], 'SEC-000004')
            self.assertEqual(conflicts.iloc[0]['field'], 'exchange')
            master = pd.read_csv(out / 'security_master_v2.csv')
            merged = master[master.security_id == 'SEC-000004'].iloc[0]
            self.assertEqual(merged['quality_status'], 'conflict')
            self.assertIn('security_master_v2.csv', summary['outputs'])
            self.assertTrue(summary['outputs']['security_master_v2.csv'])

    def test_resolve_symbol_rename_and_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            build_outputs(_archive(tmp, 'source_a'), out, tables=('symbols',))
            symbols = pd.read_csv(out / 'symbol_history.csv')
            # 改名：同一 security_id 连续
            self.assertEqual(resolve_symbol(symbols, 'US.OLD', '2015-06-01')['security_id'], 'SEC-000001')
            self.assertEqual(resolve_symbol(symbols, 'US.NEW', '2020-01-01')['security_id'], 'SEC-000001')
            self.assertEqual(resolve_symbol(symbols, 'US.OLD', '2020-01-01')['status'], 'not_found')
            # 复用：同一 symbol 在两个不重叠窗口属于不同证券
            self.assertEqual(resolve_symbol(symbols, 'US.REUSE', '2017-06-01')['security_id'], 'SEC-000003')
            self.assertEqual(resolve_symbol(symbols, 'US.REUSE', '2019-06-01')['security_id'], 'SEC-000004')
            self.assertEqual(resolve_symbol(symbols, 'US.NOPE', '2019-01-01')['status'], 'not_found')

    def test_validate_master_rejects_bad_rows(self):
        base = normalize_master(pd.DataFrame([BASE_ROW]), 'src', ingested_at='2026-01-01T00:00:00Z')
        self.assertEqual(validate_master(base), [])
        # 区间重叠（首行 valid_to 空视为无穷）
        overlap = pd.concat([base, base.assign(valid_from='2015-01-01')], ignore_index=True)
        self.assertIn('MASTER_INTERVAL_OVERLAP', validate_master(overlap))
        # 占位日期
        placeholder = base.copy(); placeholder['listed_at'] = '1970-01-01'
        self.assertIn('PLACEHOLDER_DATE:listed_at', validate_master(placeholder))
        # valid_to 不晚于 valid_from
        bad_range = base.copy(); bad_range['valid_to'] = '2014-01-01'
        self.assertIn('VALID_TO_NOT_AFTER_FROM', validate_master(bad_range))
        # 哈希被改
        tampered = base.copy(); tampered['exchange'] = 'NYSE'
        self.assertIn('RECORD_HASH_MISMATCH', validate_master(tampered))
        # 非法资产类型
        bad_type = base.copy(); bad_type['asset_type'] = 'crypto'
        self.assertIn('BAD_ASSET_TYPE:crypto', validate_master(bad_type))
        # 缺列
        self.assertTrue(validate_master(base.drop(columns=['currency']))[0].startswith(
            'MASTER_MISSING_COLUMNS'))

    def test_record_hash_stable_and_sensitive(self):
        base = normalize_master(pd.DataFrame([BASE_ROW]), 'src', ingested_at='2026-01-01T00:00:00Z')
        row = base.iloc[0].to_dict()
        self.assertEqual(canonical_record_hash(row), canonical_record_hash(dict(row)))
        changed = dict(row); changed['exchange'] = 'NYSE'
        self.assertNotEqual(canonical_record_hash(row), canonical_record_hash(changed))

    def test_symbol_reuse_conflict_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            build_outputs(_archive(tmp, 'source_reuse_bad'), out, tables=('master', 'symbols'))
            symbols = pd.read_csv(out / 'symbol_history.csv')
            self.assertIn('SYMBOL_REUSE_CONFLICT', validate_symbols(symbols))
            conflicts = pd.read_csv(out / 'master_conflicts.csv')
            self.assertTrue((conflicts['field'] == 'symbol_reuse').any())
            self.assertEqual(symbol_reuse_conflicts(symbols)[0]['symbol'], 'US.SAME')

    def test_audit_flags_corporate_action_and_history_gaps(self):
        rows = [dict(BASE_ROW), dict(BASE_ROW, security_id='SEC-900002', listed_at='')]
        master = normalize_master(pd.DataFrame(rows), 'src', ingested_at='2026-01-01T00:00:00Z')
        actions = pd.DataFrame([{'security_id': 'SEC-900001', 'action_type': 'merger',
                                 'ex_date': '2020-03-15', 'cash_amount': None, 'ratio': None,
                                 'source_id': 'src', 'source_record_id': 'a-1',
                                 'source_observed_at': ''}])
        quality, summary = audit_master(master, pd.DataFrame(columns=list(SYMBOL_COLUMNS)), actions)
        by_id = quality.set_index('security_id')
        self.assertIn('corporate_action_unresolved', by_id.loc['SEC-900001', 'problems'])
        self.assertIn('MISSING_SYMBOL_HISTORY', by_id.loc['SEC-900001', 'problems'])
        self.assertIn('MISSING_HISTORY', by_id.loc['SEC-900002', 'problems'])   # 上市日未知
        self.assertEqual(summary['corporate_action_unresolved'], 1)
        self.assertEqual(summary['missing_history'], 1)

    def test_cli_import_and_audit_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            cmd = [sys.executable, 'scripts/data/import_security_master_v2.py',
                   '--source-archive', f'source_a={FIX / "source_a"}', f'source_b={FIX / "source_b"}',
                   '--run-id', 'R1', '--archive-root', str(Path(tmp) / 'arch'),
                   '--output-dir', str(out)]
            first = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            for name in ('security_master_v2.csv', 'symbol_history.csv',
                         'corporate_actions.csv', 'master_conflicts.csv', 'import_summary.json'):
                self.assertTrue((out / name).is_file(), name)
            # 同一 run 再跑必须拒绝覆盖
            second = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            # 审计 CLI
            audit = subprocess.run(
                [sys.executable, 'scripts/data/audit_security_master_v2.py',
                 '--master', str(out / 'security_master_v2.csv'),
                 '--symbols', str(out / 'symbol_history.csv'),
                 '--actions', str(out / 'corporate_actions.csv'),
                 '--output-dir', str(Path(tmp) / 'qa')],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(audit.returncode, 0, audit.stderr)
            summary = json.loads((Path(tmp) / 'qa' / 'summary.json').read_text())
            self.assertEqual(summary['corporate_action_unresolved'], 1)   # SEC-000002 的 merger 无 ratio/cash


if __name__ == '__main__':
    unittest.main()
