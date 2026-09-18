"""行情刷新的安全防线：只追加，历史段一格都不能变；选表选不满就报错。"""
import unittest

import pandas as pd

from scripts.medium_term.p2_selection_check import TECH
from scripts.portfolio_shadow.refresh_data import (HISTORY_TOL, ROOT, history_unchanged,
                                                   select_tech_master, tech_master_codes)


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
        self.assertIn('TECH_MASTER_EMPTY', str(ctx.exception))

    def test_少一只也要报错(self):
        codes = sorted(tech_master_codes())[:-1]
        with self.assertRaises(ValueError) as ctx:
            select_tech_master(self.master(codes))
        self.assertIn('TECH_MASTER_INCOMPLETE', str(ctx.exception))

    def test_real_master_still_contains_all_thirteen(self):
        """对着**真实**主表跑一遍：主表换代/改格式时这条会先失败。"""
        path = ROOT / 'data/security_master_39.csv'
        if not path.exists():
            self.skipTest('缺少行情主表')
        selected = select_tech_master(pd.read_csv(path))
        self.assertEqual(set(selected.code.astype(str)), tech_master_codes())
