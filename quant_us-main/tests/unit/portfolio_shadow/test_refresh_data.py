"""行情刷新的安全防线：只追加，历史段一格都不能变；选表选不满就报错。"""
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.medium_term.p2_selection_check import TECH
from scripts.portfolio_shadow.refresh_data import (HISTORY_TOL, ROOT, force_tail_refetch,
                                                   history_unchanged, select_tech_master,
                                                   tech_master_codes)


def frame(rows):
    return pd.DataFrame(rows, columns=['session', 'raw_close', 'asof_ma20'])


class HistoryUnchangedTests(unittest.TestCase):
    def test_identical_frames_pass(self):
        f = frame([['2026-01-05', 100.0, 99.0]])
        ok, worst = history_unchanged(f, f)
        self.assertTrue(ok)
        self.assertEqual(worst, 0.0)

    def test_float_noise_within_tolerance_passes(self):
        """重建面板与既有文件之间的差异只有浮点求和顺序噪声（实测 ≤5.7e-14）。"""
        a = frame([['2026-01-05', 100.0, 99.0]])
        b = frame([['2026-01-05', 100.0, 99.0 + 1e-13]])
        ok, worst = history_unchanged(a, b)
        self.assertTrue(ok)
        self.assertLess(worst, HISTORY_TOL)

    def test_a_real_change_is_rejected(self):
        """哪怕一格真的变了也必须拒绝 —— 那会让已冻结的回测对照失去可复现性。"""
        a = frame([['2026-01-05', 100.0, 99.0]])
        b = frame([['2026-01-05', 100.0, 99.01]])
        ok, worst = history_unchanged(a, b)
        self.assertFalse(ok)
        self.assertGreater(worst, HISTORY_TOL)

    def test_one_side_only_nan_is_rejected(self):
        a = frame([['2026-01-05', 100.0, None]])
        b = frame([['2026-01-05', 100.0, 99.0]])
        ok, _ = history_unchanged(a, b)
        self.assertFalse(ok)

    def test_both_nan_is_not_a_change(self):
        f = frame([['2026-01-05', 100.0, None]])
        ok, _ = history_unchanged(f, f)
        self.assertTrue(ok)

    def test_row_count_mismatch_fails_closed(self):
        """行数不一致时 pandas 按索引对齐会造出单侧 NaN —— 必须判为改动（宁可误停）。"""
        a = frame([['2026-01-05', 100.0, 99.0], ['2026-01-06', 101.0, 100.0]])
        b = frame([['2026-01-05', 100.0, 99.0]])
        ok, _ = history_unchanged(a, b)
        self.assertFalse(ok)


class TechMasterTests(unittest.TestCase):
    """选表：证券 id → 主表 code 的转换，以及「选不满必须报错」。

    实测事故（2026-09-17 起）：`refresh()` 拿 `SEC-US-AAPL` 去匹配主表的 `US.AAPL`，得到
    **0 行**；空表一路变成 `download(codes=[])`，而下载工具对空 codes 不报错、只是什么都
    不做 —— 于是任务在跑、日志正常，个股日线一天都没前进。
    """

    @staticmethod
    def master(codes):
        return pd.DataFrame({'code': codes, 'listing_date': ['2000-01-01'] * len(codes)})

    def test_id_maps_to_master_code_format(self):
        self.assertEqual(tech_master_codes(), {'US.' + s.replace('SEC-US-', '') for s in TECH})
        self.assertIn('US.AAPL', tech_master_codes())
        self.assertEqual(len(tech_master_codes()), len(TECH))

    def test_production_format_master_selects_all_thirteen(self):
        """主表用生产格式（`US.AAPL`）时必须选满 13 只 —— 这是那次事故的回归测试。"""
        selected = select_tech_master(self.master(sorted(tech_master_codes())))
        self.assertEqual(len(selected), len(tech_master_codes()))

    def test_证券id格式的主表必须报错而不是选成空表(self):
        """主表若真的长成 `SEC-US-AAPL`（或又换了格式），要炸出来，不能静默空表。"""
        with self.assertRaises(ValueError) as ctx:
            select_tech_master(self.master(['SEC-US-AAPL', 'SEC-US-AMD']))
        # 错误码去掉了 TECH 前缀：现在也服务三臂前向实验的 32 只（不只 TECH）
        self.assertIn('MASTER_EMPTY', str(ctx.exception))

    def test_少一只也要报错(self):
        codes = sorted(tech_master_codes())[:-1]
        with self.assertRaises(ValueError) as ctx:
            select_tech_master(self.master(codes))
        self.assertIn('MASTER_INCOMPLETE', str(ctx.exception))

    def test_real_master_still_contains_all_thirteen(self):
        """对着**真实**主表跑一遍：主表换代/改格式时这条会先失败。"""
        path = ROOT / 'data/security_master_39.csv'
        if not path.exists():
            self.skipTest('缺少行情主表')
        selected = select_tech_master(pd.read_csv(path))
        self.assertEqual(set(selected.code.astype(str)), tech_master_codes())


class ForceTailRefetchTests(unittest.TestCase):
    """回退「已覆盖」上界：只动目标年份，别的记录一格都不能改。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / 'download_state.json'

    def write(self, **completed):
        self.path.write_text(json.dumps(
            {'completed': completed, 'failed': {}, 'unavailable': {}}, indent=2))
        return self.path.read_bytes()

    @staticmethod
    def record(year, start='2026-01-01'):
        return {'path': '/tmp/x.csv.gz', 'rows': 100, 'sha256': 'deadbeef',
                'requested_start': start, 'requested_end': f'{year}-09-18'}

    def test_回退上界到去年年底(self):
        self.write(**{'US.AAPL|day|none|2026': self.record(2026)})
        self.assertEqual(force_tail_refetch(self.path, 2026), 1)
        rec = json.loads(self.path.read_text())['completed']['US.AAPL|day|none|2026']
        # 下载工具的 covered 判据是 `covered_end >= part_end`；回退到去年年底必然判不覆盖，
        # 于是重取尾部。sha256 等其它字段必须原样保留（那只校验文件内容）。
        self.assertEqual(rec['requested_end'], '2025-12-31')
        self.assertEqual(rec['requested_start'], '2026-01-01')
        self.assertEqual(rec['sha256'], 'deadbeef')

    def test_只动目标年份_别的记录逐字段不变(self):
        self.write(**{'US.AAPL|day|none|2025': self.record(2025),
                      'US.AAPL|day|none|2026': self.record(2026),
                      'US.AAPL|day|none|2027': self.record(2027)})
        before = json.loads(self.path.read_text())['completed']
        self.assertEqual(force_tail_refetch(self.path, 2026), 1)
        after = json.loads(self.path.read_text())['completed']
        for key in ('US.AAPL|day|none|2025', 'US.AAPL|day|none|2027'):
            self.assertEqual(before[key], after[key], key)

    def test_重复调用是空操作_且不重写文件(self):
        """定时任务一天跑三次，第二次起必须什么都不做（也免得把检查点的缩进格式改掉）。"""
        self.write(**{'US.AAPL|day|none|2026': self.record(2026)})
        self.assertEqual(force_tail_refetch(self.path, 2026), 1)
        bytes_once = self.path.read_bytes()
        self.assertEqual(force_tail_refetch(self.path, 2026), 0)
        self.assertEqual(self.path.read_bytes(), bytes_once)

    def test_年份对不上时一个字都不写(self):
        """`if changed:` 守卫：没有可回退的记录时不能重写检查点。"""
        before = self.write(**{'US.AAPL|day|none|2025': self.record(2025)})
        self.assertEqual(force_tail_refetch(self.path, 2026), 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_认不出的键跳过且不改动(self):
        """键形如 `<code>|<kind>|<autype>|<year>`（4 段）。历史遗留的 3 段键是旧 qfq 记录，
        本路径只下载 `autype=none`，解析不了就跳过 —— 但绝不能顺手改掉它们。"""
        before = self.write(**{'US.AAPL|day|2026': self.record(2026),
                               'US.AAPL|day|none|2026': self.record(2026)})
        self.assertEqual(force_tail_refetch(self.path, 2026), 1)
        after = json.loads(self.path.read_text())['completed']
        self.assertEqual(after['US.AAPL|day|2026'],
                         json.loads(before)['completed']['US.AAPL|day|2026'])

    def test_检查点不存在时是空操作_且不新建文件(self):
        """与下载工具 `_load_checkpoint` 的语义一致：没有检查点就没有「已覆盖」可言。"""
        self.assertEqual(force_tail_refetch(self.path, 2026), 0)
        self.assertFalse(self.path.exists())

    def test_failed与unavailable段原样保留(self):
        self.path.write_text(json.dumps(
            {'completed': {'US.AAPL|day|none|2026': self.record(2026)},
             'failed': {'US.MU|day|none|2026': {'error': 'EMPTY_RESPONSE'}},
             'unavailable': {'US.SOXL|day|none|2026': {'reason': 'BEFORE_REPORTED_LISTING'}}},
            indent=2))
        self.assertEqual(force_tail_refetch(self.path, 2026), 1)
        state = json.loads(self.path.read_text())
        self.assertIn('US.MU|day|none|2026', state['failed'])
        self.assertIn('US.SOXL|day|none|2026', state['unavailable'])
