"""行情刷新的安全防线：只追加，历史段一格都不能变；选表选不满就报错。"""
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import pandas as pd

from scripts.medium_term.p2_selection_check import TECH
from scripts.portfolio_shadow import refresh_data
from scripts.portfolio_shadow.refresh_data import (HISTORY_TOL, ROOT, force_tail_refetch,
                                                   history_unchanged, select_tech_master,
                                                   tech_master_codes)


def frame(rows):
    return pd.DataFrame(rows, columns=['session', 'raw_close', 'asof_ma20'])


class HistoryUnchangedTests(unittest.TestCase):
    def test_identical_frames_pass(self):
        f = frame([['2026-01-05', 100.0, 99.0]])
        ok, worst, adjusted = history_unchanged(f, f)
        self.assertTrue(ok)
        self.assertEqual(worst, 0.0)
        self.assertEqual(adjusted, 0.0)

    def test_float_noise_within_tolerance_passes(self):
        """重建面板与既有文件之间的差异只有浮点求和顺序噪声（实测 ≤5.7e-14）。"""
        a = frame([['2026-01-05', 100.0, 99.0]])
        b = frame([['2026-01-05', 100.0, 99.0 + 1e-13]])
        ok, worst, adjusted = history_unchanged(a, b)
        self.assertTrue(ok)
        self.assertLess(worst, HISTORY_TOL)
        self.assertLess(adjusted, HISTORY_TOL)

    def test_a_raw_change_is_rejected(self):
        """**原始列**：哪怕一格真的变了也必须拒绝 —— 那是数据被改写。"""
        a = frame([['2026-01-05', 100.0, 99.0]])
        b = frame([['2026-01-05', 100.01, 99.0]])
        ok, worst, _adjusted = history_unchanged(a, b)
        self.assertFalse(ok)
        self.assertGreater(worst, HISTORY_TOL)

    def test_an_adjusted_restatement_is_reported_not_rejected(self):
        """**复权列**：行动表新增一条记录会把整段历史按同一因子重述，这是构造性的。

        实测形状：META 2026-09-21 新宣告股息 0.525 ⇒ 整段 `asof_close` 平移 7.9e-4。
        旧实现把它判成数据损坏，日作业从此停摆。现在放行，但幅度必须报出来。
        """
        a = frame([['2026-01-05', 100.0, 99.0]])
        b = frame([['2026-01-05', 100.0, 99.0 * (1 - 7.886e-4)]])
        ok, raw_worst, adjusted = history_unchanged(a, b)
        self.assertTrue(ok, '复权列重述不得阻塞日常刷新')
        self.assertEqual(raw_worst, 0.0)
        self.assertGreater(adjusted, HISTORY_TOL)

    def test_a_raw_column_gaining_a_value_is_rejected(self):
        """原始列出现/消失一个值 = 数据被改写 ⇒ 硬失败。"""
        a = frame([['2026-01-05', None, 99.0]])
        b = frame([['2026-01-05', 100.0, 99.0]])
        ok, _w, _a = history_unchanged(a, b)
        self.assertFalse(ok)

    def test_a_derived_column_gaining_a_value_is_only_reported(self):
        """复权列拿到值（例如 MA200 预热够了）是构造性的，不该阻塞刷新。"""
        a = frame([['2026-01-05', 100.0, None]])
        b = frame([['2026-01-05', 100.0, 99.0]])
        ok, raw_worst, _adjusted = history_unchanged(a, b)
        self.assertTrue(ok)
        self.assertEqual(raw_worst, 0.0)

    def test_both_nan_is_not_a_change(self):
        f = frame([['2026-01-05', 100.0, None]])
        ok, _w, _a = history_unchanged(f, f)
        self.assertTrue(ok)

    def test_row_count_mismatch_fails_closed(self):
        """行数不一致是**结构性**改动：显式判，不依赖 pandas 对齐时的 dtype 意外。"""
        a = frame([['2026-01-05', 100.0, 99.0], ['2026-01-06', 101.0, 100.0]])
        b = frame([['2026-01-05', 100.0, 99.0]])
        ok, _w, _a = history_unchanged(a, b)
        self.assertFalse(ok)


class ClosedSessionScopingTests(unittest.TestCase):
    """刷新只能刷到**收盘已过**的 session，且要能自愈早先写进去的未收盘行。

    实测动机：2026-09-22 00:09（美东 09-21 盘中）跑刷新，把当天那根还没走完的 bar
    追加进了面板；两分钟后同一行的 raw_close/volume 就变了，于是「只追加」的守卫
    在下一次刷新时必然失败 —— 日作业会因此永久停摆。
    """

    def setUp(self):
        # 先把真身存下来：`refresh_data.pd` 就是 pandas 模块，直接 patch `pd.read_csv`
        # 会让下面那个 side_effect 递归调到自己（RecursionError）。
        self._real_read_csv = pd.read_csv
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.raw = root / 'raw'
        self.panels = root / 'panels'
        self.panels.mkdir(parents=True)
        (self.raw / 'day/none/year=2026').mkdir(parents=True)
        self.sid = 'SEC-US-AAPL'
        self.sessions = pd.date_range('2026-08-03', periods=10, freq='B').normalize()
        rows = pd.DataFrame({
            'code': 'US.AAPL', 'time_key': self.sessions,
            'open': [100.0] * 10, 'high': [101.0] * 10, 'low': [99.0] * 10,
            'close': [100.0] * 10, 'volume': [1_000] * 10})
        rows.to_csv(self.raw / 'day/none/year=2026/US_AAPL.csv.gz', index=False,
                    compression='gzip')
        self.actions = pd.DataFrame(columns=['security_id', 'action_type', 'ex_date',
                                             'ratio', 'cash_amount'])
        # 面板必须先存在（`refresh_panels` 读它做历史段比对）—— 用与生产同一条管线造：
        # 拿原始行情走 `build_asof_panel`，这样「旧面板」与「重建面板」的差异只来自
        # 我故意注入的改动。
        norm = refresh_data.normalize(rows, 'day').rename(columns={'date': 'session'})
        norm['security_id'] = self.sid
        panel = refresh_data.build_asof_panel(norm, self.actions)
        panel.to_csv(self.panels / 'US_AAPL.csv.gz', index=False, compression='gzip')

    def tearDown(self):
        self.tmp.cleanup()

    def _read_csv(self, path, *a, **kw):
        if str(path).endswith('actions.csv'):
            return self.actions.copy()
        return self._real_read_csv(path, *a, **kw)

    def _build(self, through):
        """在临时目录上跑一次 `refresh_panels`（只放一只证券）。"""
        (self.panels / 'actions.csv').write_text('security_id\n')
        with unittest.mock.patch.object(refresh_data, 'PANELS', self.panels), \
             unittest.mock.patch.object(refresh_data, 'RAW_ROOT', self.raw), \
             unittest.mock.patch.object(refresh_data, 'ACTIONS',
                                        str(self.panels / 'actions.csv')), \
             unittest.mock.patch.object(refresh_data.pd, 'read_csv',
                                        side_effect=self._read_csv):
            return refresh_data.refresh_panels(through=through, names=[self.sid])

    def test_history_before_the_last_row_must_be_raw_identical(self):
        self._build(str(self.sessions[-1].date()))
        # 改写历史段（倒数第二行）的原始价 ⇒ 必须拒绝
        rows = pd.read_csv(self.raw / 'day/none/year=2026/US_AAPL.csv.gz')
        rows.loc[5, 'close'] = 123.0
        rows.to_csv(self.raw / 'day/none/year=2026/US_AAPL.csv.gz', index=False,
                    compression='gzip')
        with self.assertRaises(ValueError) as ctx:
            self._build(str(self.sessions[-1].date()))
        self.assertIn('PANEL_RAW_HISTORY_CHANGED', str(ctx.exception))

    def test_an_unclosed_row_already_stored_is_dropped_and_healed(self):
        """面板里已经有一行「当时还没收盘」的 session ⇒ 丢掉它，下次正常刷新自己补上。"""
        self._build(str(self.sessions[-1].date()))
        panel = self.panels / 'US_AAPL.csv.gz'
        stored = pd.read_csv(panel)
        # 伪造一行未收盘的 session（盘中写入的部分 bar）
        extra = stored.iloc[[-1]].copy()
        # 写成与文件里其它行**同一格式**的日期串：混进 Timestamp 会让再次读取时
        # 格式推断给出 NaT（本项目踩过同类的混合格式坑）
        extra['session'] = str((pd.Timestamp(self.sessions[-1])
                                + pd.Timedelta(days=1)).date())
        extra['raw_close'] = 777.0
        pd.concat([stored, extra], ignore_index=True).to_csv(panel, index=False,
                                                            compression='gzip')
        out = self._build(str(self.sessions[-1].date()))
        self.assertEqual(out[0]['dropped_unclosed_rows'], 1)
        after = pd.read_csv(panel)
        self.assertLessEqual(pd.to_datetime(after.session).max(),
                             pd.Timestamp(self.sessions[-1]))

    def test_new_sessions_are_appended(self):
        """**最基本的职责**：面板必须把新 session 追加上去。

        第一版改写漏掉了这一步（只保留 ≤ 上周期的行）⇒ 面板永久冻结、影子作业静默停摆。
        这条测试是本轮唯一能抓住它的东西 —— 我原来的三条测试只覆盖了「拒绝改写」与
        「丢掉未收盘行」，没有一条检验「新数据进得来」。
        """
        self._build(str(self.sessions[3].date()))
        before = len(pd.read_csv(self.panels / 'US_AAPL.csv.gz'))
        out = self._build(str(self.sessions[-1].date()))
        after = pd.read_csv(self.panels / 'US_AAPL.csv.gz')
        self.assertEqual(out[0]['appended'], len(self.sessions) - 4)
        self.assertEqual(len(after), len(self.sessions))
        self.assertGreater(len(after), before)
        self.assertEqual(str(pd.to_datetime(after.session).max().date()),
                         str(self.sessions[-1].date()))

    def test_the_default_comes_from_the_closed_session_gate(self):
        """默认 `through` 必须来自 `data_readiness.expected_session`（同一处判据）。"""
        with unittest.mock.patch(
                'scripts.portfolio_shadow.data_readiness.expected_session',
                return_value=str(self.sessions[3].date())) as gate:
            self._build(None)
        self.assertTrue(gate.called, '默认值必须走数据就绪门，而不是「今天」')


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


class DownloadFailureMessageTests(unittest.TestCase):
    """下载失败时的报错要**带得上原因**。

    实测 2026-09-23：`shadow-daily` 报的是 `RuntimeError: DOWNLOAD_FAILED:` —— 冒号后面
    什么都没有。根因是下载工具**失败时不写 stderr**：它把摘要 JSON 打到 stdout
    （`{"completed": n, "failed": m}`）然后 `return 1`，而旧实现只看 `proc.stderr`。
    结果是把一个可以一眼看懂的原因（failed=N）丢成了空白 —— 当时只能靠手动重跑才排除
    "是不是数据问题"。
    """

    def _run(self, *, rc, stdout='', stderr=''):
        proc = unittest.mock.Mock(returncode=rc, stdout=stdout, stderr=stderr)
        with unittest.mock.patch.object(refresh_data.subprocess, 'run', return_value=proc):
            return refresh_data.download(
                master=Path('/tmp/m.csv'), start='2026-01-01', end='2026-01-02',
                output_root=Path('/tmp/raw'), checkpoint=Path('/tmp/cp.json'))

    def test_summary_on_stdout_is_surfaced(self):
        """下载工具的实际形态：stderr 空、摘要 JSON 在 stdout。"""
        with self.assertRaises(RuntimeError) as ctx:
            self._run(rc=1, stdout='{"completed": 3, "failed": 2}\n')
        msg = str(ctx.exception)
        self.assertIn('DOWNLOAD_FAILED', msg)
        self.assertIn('rc=1', msg)
        self.assertIn('failed=2', msg)

    def test_stderr_wins_when_present(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(rc=2, stdout='{"completed": 0, "failed": 1}\n',
                      stderr='Traceback: 连接被拒\n')
        self.assertIn('连接被拒', str(ctx.exception))

    def test_both_empty_still_reports_rc(self):
        """两个流都空（例如被信号杀掉）时，退出码是唯一线索，不能说成空白。"""
        with self.assertRaises(RuntimeError) as ctx:
            self._run(rc=-9)
        msg = str(ctx.exception)
        self.assertIn('rc=-9', msg)
        self.assertIn('没有输出', msg)

    def test_success_still_parses_the_summary(self):
        self.assertEqual(self._run(rc=0, stdout='noise\n{"completed": 5, "failed": 0}\n'),
                         {'completed': 5, 'failed': 0})
