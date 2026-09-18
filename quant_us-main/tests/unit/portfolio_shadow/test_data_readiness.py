"""数据就绪门：区分「上游还没发布」「本地没在请求」「两个源不一致」。

2026-09-18 事故的验收项（`docs/llm-decision-role-audit-2026-09-18.md` §4.1/P0-1）：
**能复现个股慢于 ETF 的场景**、状态明确、过期时不再拿旧会话当今天的任务。

只判「数据够不够新」是不够的：`PARTIAL_DATA`（个股没动、ETF 动了）与 `STALE_REQUEST`
（谁都没动、请求也没发出去）在那一维上都表现为「不够新」，会双双落进 `WAITING_FOR_DATA`
—— 一个「上游还没出」的结论，而真相是本地把请求变成了空操作。
"""
import gzip
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from scripts.portfolio_shadow import data_readiness as dr
from scripts.portfolio_shadow.cli import cmd_freeze, cmd_run_daily
from scripts.portfolio_shadow.store import ShadowStore

REPO = Path(__file__).resolve().parents[4]
NOW = '2026-09-18T06:40:00+00:00'          # 美东 09-18 02:40，09-17 已收盘
EXPECTED = '2026-09-17'


def src(session, fetched_at):
    return {'session': session, 'fetched_at': fetched_at}


FRESH = '2026-09-18T06:39:00+00:00'        # 12 小时内
OLD = '2026-09-15T06:39:00+00:00'          # 远超 12 小时


class ExpectedSessionTests(unittest.TestCase):
    """T =「规则历里收盘已过的最新 session」，不是「日历上的最后一天」。"""

    def test_收盘前仍算上一个交易日(self):
        self.assertEqual(dr.expected_session('2026-09-18T19:59:00+00:00'), '2026-09-17')

    def test_收盘后就是当天(self):
        self.assertEqual(dr.expected_session('2026-09-18T20:01:00+00:00'), '2026-09-18')

    def test_周末回退到周五(self):
        self.assertEqual(dr.expected_session('2026-09-19T12:00:00+00:00'), '2026-09-18')
        self.assertEqual(dr.expected_session('2026-09-20T12:00:00+00:00'), '2026-09-18')

    def test_夏令时下收盘时刻仍然正确(self):
        """9 月是 EDT（UTC-4），收盘 16:00 ET = 20:00Z；边界两侧各错一秒都必须分得开。"""
        self.assertEqual(dr.expected_session('2026-09-18T19:59:59+00:00'), '2026-09-17')
        self.assertEqual(dr.expected_session('2026-09-18T20:00:01+00:00'), '2026-09-18')


class AssessTests(unittest.TestCase):
    def assess(self, tech, etf, **kw):
        return dr.assess(sources={'tech': tech, 'etf': etf}, expected=EXPECTED, now=NOW, **kw)

    def test_两个源都到齐就是READY(self):
        v = self.assess(src(EXPECTED, FRESH), src(EXPECTED, FRESH))
        self.assertEqual(v['state'], dr.READY)
        self.assertFalse(v['blocking'])

    def test_个股慢于ETF判PARTIAL_DATA(self):
        """P0-1 的验收场景：ETF 已经到 09-17，个股还停在 09-16。"""
        v = self.assess(src('2026-09-16', FRESH), src('2026-09-17', FRESH))
        self.assertEqual(v['state'], dr.PARTIAL_DATA)
        self.assertTrue(v['blocking'])
        self.assertIn('2026-09-16', v['reason'])
        self.assertIn('2026-09-17', v['reason'])

    def test_个股跑到ETF前面也是PARTIAL_DATA(self):
        v = self.assess(src('2026-09-17', FRESH), src('2026-09-16', FRESH))
        self.assertEqual(v['state'], dr.PARTIAL_DATA)

    def test_两个源一致落后且最近请求过是等待(self):
        v = self.assess(src('2026-09-16', FRESH), src('2026-09-16', FRESH))
        self.assertEqual(v['state'], dr.WAITING_FOR_DATA)
        self.assertFalse(v['blocking'])          # 等待不是故障
        self.assertIn('上游尚未发布', v['reason'])

    def test_两个源一致落后但很久没请求是STALE_REQUEST(self):
        v = self.assess(src('2026-09-16', OLD), src('2026-09-16', OLD))
        self.assertEqual(v['state'], dr.STALE_REQUEST)
        self.assertTrue(v['blocking'])
        self.assertEqual(v['stale_sources'], ['etf', 'tech'])

    def test_只有一个源没在请求也报STALE_REQUEST(self):
        """会话恰好一致、但其中一个源根本没在请求 —— 只看「数据到哪天」会漏掉这种情况。"""
        v = self.assess(src('2026-09-16', FRESH), src('2026-09-16', OLD))
        self.assertEqual(v['state'], dr.STALE_REQUEST)
        self.assertEqual(v['stale_sources'], ['etf'])

    def test_没有取数时刻等于没在请求(self):
        v = self.assess(src('2026-09-16', None), src('2026-09-16', FRESH))
        self.assertEqual(v['state'], dr.STALE_REQUEST)
        self.assertEqual(v['stale_sources'], ['tech'])

    def test_数据比日历新是AHEAD_OF_CALENDAR(self):
        v = self.assess(src('2026-09-18', FRESH), src('2026-09-18', FRESH))
        self.assertEqual(v['state'], dr.AHEAD_OF_CALENDAR)
        self.assertTrue(v['blocking'])

    def test_源缺失是PARTIAL_DATA而不是沉默(self):
        v = self.assess(src(None, None), src(EXPECTED, FRESH))
        self.assertEqual(v['state'], dr.PARTIAL_DATA)
        self.assertIn('tech', v['reason'])

    def test_日历给不出会话是NO_CALENDAR(self):
        v = dr.assess(sources={'tech': src(EXPECTED, FRESH), 'etf': src(EXPECTED, FRESH)},
                      expected=None, now=NOW)
        self.assertEqual(v['state'], dr.NO_CALENDAR)
        self.assertTrue(v['blocking'])

    def test_只有等待与就绪不拦(self):
        self.assertEqual(dr.BLOCKING,
                         {dr.PARTIAL_DATA, dr.STALE_REQUEST, dr.AHEAD_OF_CALENDAR, dr.NO_CALENDAR})
        self.assertNotIn(dr.READY, dr.BLOCKING)
        self.assertNotIn(dr.WAITING_FOR_DATA, dr.BLOCKING)


class SourceStateTests(unittest.TestCase):
    """状态取自分区文件本身：数据到哪天、最后一次成功取数在什么时候。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.dir = Path(self.dir.name)

    def part(self, name, rows):
        path = self.dir / name
        pd.DataFrame(rows, columns=['time_key', 'downloaded_at']).to_csv(
            path, index=False, compression='gzip')
        return path

    def test_取最新会话与最新取数时刻(self):
        a = self.part('a.csv.gz', [('2026-09-16', '2026-09-17T10:00:00+00:00')])
        b = self.part('b.csv.gz', [('2026-09-17', '2026-09-18T06:00:00+00:00')])
        st = dr.source_state(paths=[a, b])
        self.assertEqual(st['session'], '2026-09-17')
        self.assertEqual(st['fetched_at'], '2026-09-18T06:00:00+00:00')

    def test_混合时间格式不会丢掉少数派(self):
        """`downloaded_at` 一批取数一个值，同一列里格式可能混用（带/不带微秒）。

        列级 `pd.to_datetime` 会按多数派推断、把少数派整列判成 NaT —— 静默丢数据
        （见 `evidence_store.to_utc_series`）。这里少数派恰好是**最新**那批：丢了它
        就会得出「很久没取数」的相反结论。
        """
        rows = [('2026-09-15', '2026-09-17T10:58:00+00:00'),
                ('2026-09-16', '2026-09-17T10:58:00+00:00'),
                ('2026-09-17', '2026-09-18T06:39:24.142221+00:00')]
        st = dr.source_state(paths=[self.part('mixed.csv.gz', rows)])
        self.assertEqual(st['session'], '2026-09-17')
        self.assertEqual(st['fetched_at'], '2026-09-18T06:39:24.142221+00:00')

    def test_文件不存在或为空不炸(self):
        st = dr.source_state(paths=[self.dir / 'nope.csv.gz'])
        self.assertIsNone(st['session'])
        self.assertIsNone(st['fetched_at'])


# ---- 接线：门拦住时 run-daily 不做 prepare，且用退出码把故障暴露出来 ----

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


def load_shadow_status():
    """`ops/shadow_status.py` 不在包内 —— 按路径加载，不污染 sys.path。"""
    path = REPO / 'ops' / 'shadow_status.py'
    spec = importlib.util.spec_from_file_location('shadow_status_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunDailyGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = Path(self.tmp) / 'out'
        manifest_path = Path(self.tmp) / 'manifest.json'
        manifest_path.write_text(json.dumps(draft()), encoding='utf-8')
        cmd_freeze(SimpleNamespace(manifest=str(manifest_path),
                                   start_session='2026-01-02', output=str(self.out)))
        self.manifest = self.out / 'EXP' / 'manifest.json'
        self.prices = pd.DataFrame({'session': [pd.Timestamp('2026-09-17')],
                                    'security_id': ['SEC-US-AAPL']})

    def run_daily(self, gate_verdict, prepare_result=None):
        """跑一次 run-daily，把数据层与门替换掉，只验接线。

        `_capture` 是「传一个 namespace、读回它打印的 JSON」，假实现必须照这个契约来。
        """
        args = SimpleNamespace(manifest=str(self.manifest), output=str(self.out),
                               session=None, evidence=None, etf_raw=None,
                               model='fixture', fixture_action='PASS')
        calls = []
        prepare_result = prepare_result or {'opportunities': 0, 'session': '2026-09-17'}

        def fake_prepare(ns):
            calls.append(ns)
            print(json.dumps(prepare_result))

        buf = io.StringIO()
        with patch('scripts.portfolio_shadow.cli._market_data',
                   return_value=(self.prices, None, None, None)), \
             patch('scripts.portfolio_shadow.cli._data_gate', return_value=gate_verdict), \
             patch('scripts.portfolio_shadow.cli.cmd_prepare_entry_reviews', fake_prepare), \
             redirect_stdout(buf):
            rc = cmd_run_daily(args)
        return rc, json.loads(buf.getvalue()), calls

    def verdict(self, state, blocking=True):
        return {'state': state, 'blocking': blocking, 'sources': {},
                'expected_session': EXPECTED, 'reason': f'{state} 的理由'}

    def test_门拦住时不做prepare且退出码非零(self):
        rc, result, calls = self.run_daily(self.verdict(dr.PARTIAL_DATA))
        self.assertEqual(rc, 1)
        self.assertEqual(calls, [], '门拦住了却还去 prepare 了')
        self.assertEqual(result['steps']['skipped_by_gate']['state'], dr.PARTIAL_DATA)

    def test_故障与等待用退出码分开(self):
        """等待明天再来（0）；故障必须看得见（非 0）—— 两者在日志上不能长得一样。"""
        rc_wait, _, calls_wait = self.run_daily(
            self.verdict(dr.WAITING_FOR_DATA, blocking=False))
        self.assertEqual(rc_wait, 0)
        self.assertEqual(calls_wait, [])
        for state in (dr.STALE_REQUEST, dr.AHEAD_OF_CALENDAR, dr.NO_CALENDAR):
            with self.subTest(state=state):
                self.assertEqual(self.run_daily(self.verdict(state))[0], 1)

    def test_就绪时照常prepare(self):
        rc, result, calls = self.run_daily(self.verdict(dr.READY, blocking=False))
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['steps']['prepare']['opportunities'], 0)
        self.assertNotIn('skipped_by_gate', result['steps'])

    def test_状态判定优先报门的原因(self):
        """门拦住 → 不可能有新的决策，状态必须先说门，而不是「无候选」。"""
        shadow_status = load_shadow_status()
        run_json = Path(self.tmp) / 'run.json'
        _, result, _ = self.run_daily(self.verdict(dr.PARTIAL_DATA))
        run_json.write_text(json.dumps(result), encoding='utf-8')
        got = shadow_status.classify(str(self.manifest), str(self.out), str(run_json))
        self.assertEqual(got['status'], 'DATA_PARTIAL')
        self.assertEqual(got['gate_state'], dr.PARTIAL_DATA)
        self.assertIn('PARTIAL_DATA 的理由', got['gate_reason'])

    def test_等待态在状态判定里也看得见(self):
        shadow_status = load_shadow_status()
        run_json = Path(self.tmp) / 'run.json'
        _, result, _ = self.run_daily(self.verdict(dr.WAITING_FOR_DATA, blocking=False))
        run_json.write_text(json.dumps(result), encoding='utf-8')
        got = shadow_status.classify(str(self.manifest), str(self.out), str(run_json))
        self.assertEqual(got['status'], 'WAITING_FOR_DATA')
