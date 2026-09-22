"""行情数据完整性检查（抓「静默停摆」）。

覆盖两件要防的事与一条**防假警**：面板落后于原始库、分区与检查点不同步；
以及**旧格式三段键不得触发告警**（下载器从不查它们，报了就是假警，而假警会淹掉真警）。
"""
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.live_trading.data_integrity_check import (_sha256, checkpoint_stale,
                                                       panel_path, check)

CODE = 'SEC-US-AAPL'
CODE_KEY = 'US.AAPL'


class IntegrityFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.raw = root / 'raw'
        self.panels = root / 'panels'
        (self.raw / 'day/none/year=2026').mkdir(parents=True)
        self.panels.mkdir(parents=True)
        self.checkpoint = root / 'download_state.json'
        self.write_raw(['2026-09-18', '2026-09-21'])
        self.write_panel(['2026-09-18', '2026-09-21'])
        # 记录形状与真实检查点一致（dict 带 sha256；下载器用的就是下载器写下的那种）
        self.write_checkpoint({f'{CODE_KEY}|day|none|2026': {'sha256': self.raw_hash()}})

    def tearDown(self):
        self.tmp.cleanup()

    def raw_path(self):
        return self.raw / 'day/none/year=2026' / 'US_AAPL.csv.gz'

    def write_raw(self, sessions, close=100.0):
        pd.DataFrame({'code': CODE_KEY, 'time_key': sessions, 'open': close,
                      'high': close, 'low': close, 'close': close,
                      'volume': 1_000_000}).to_csv(self.raw_path(), index=False,
                                                   compression='gzip')

    def write_panel(self, sessions):
        pd.DataFrame({'session': sessions, 'raw_close': 100.0}).to_csv(
            panel_path(CODE, self.panels), index=False, compression='gzip')

    def write_checkpoint(self, completed):
        self.checkpoint.write_text(json.dumps({'completed': completed}))

    def raw_hash(self):
        return _sha256(self.raw_path())

    def run_check(self):
        return check(which='tech', panels=self.panels, raw_root=self.raw,
                     checkpoint=self.checkpoint)


class IntegrityTests(IntegrityFixture):
    def test_baseline_is_ok(self):
        result = self.run_check()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['problems'], [])

    def test_panels_behind_raw_is_flagged(self):
        """作业照常跑、判「没有新 session」、结果永久停住 —— 这就是那个形态。"""
        self.write_raw(['2026-09-18', '2026-09-21'])
        self.write_panel(['2026-09-18'])
        result = self.run_check()
        self.assertFalse(result['ok'])
        self.assertIn('PANELS_BEHIND_RAW:1', result['problems'])
        self.assertEqual(result['panels_behind_raw'][0]['raw'], '2026-09-21')
        self.assertEqual(result['panels_behind_raw'][0]['panel'], '2026-09-18')

    def test_checkpoint_mismatch_is_flagged(self):
        """分区被改写而检查点没更新 ⇒ 下一次刷新会硬失败，必须提前喊。"""
        self.write_raw(['2026-09-18', '2026-09-21'], close=101.0)   # 内容变了
        result = self.run_check()
        self.assertFalse(result['ok'])
        self.assertIn('CHECKPOINT_MISMATCH:1', result['problems'])

    def test_a_legacy_three_part_key_never_alarms(self):
        """**防假警**：检查点里有 121 条三段旧格式键，下载器从不查它们。

        我的第一版把两种键都查 ⇒ 对 LITE/MU 恒报「检查点不符」。假警与真警长得一样，
        看门狗会因此失效（本项目已有过一次「告警通道从未送达」的教训）。
        """
        self.write_checkpoint({f'{CODE_KEY}|day|2026': {'sha256': 'deadbeef'},   # 陈旧的三段键
                               f'{CODE_KEY}|day|none|2026': {'sha256': self.raw_hash()}})
        self.assertEqual(checkpoint_stale([CODE], checkpoint=self.checkpoint,
                                          raw_root=self.raw), [])
        self.assertTrue(self.run_check()['ok'])

    def test_a_missing_checkpoint_is_reported_not_crashed(self):
        self.checkpoint.unlink()
        result = self.run_check()
        self.assertFalse(result['ok'])
        self.assertIn('CHECKPOINT_MISMATCH:1', result['problems'])


if __name__ == '__main__':
    unittest.main()
