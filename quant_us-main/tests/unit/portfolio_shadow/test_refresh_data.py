"""行情刷新的安全防线：只追加，历史段一格都不能变。"""
import unittest

import pandas as pd

from scripts.portfolio_shadow.refresh_data import HISTORY_TOL, history_unchanged


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
