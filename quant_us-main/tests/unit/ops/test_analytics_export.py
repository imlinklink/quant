"""导出层的回归测试。

覆盖四类**容易静默做错**的地方：零与空值的区分、schema 9/10 的字段差异、
初始化回放与前向的边界、以及来源失败不得退化。每条都对应一个真实踩过的形态。
"""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]        # quant_us-main
REPO = ROOT.parent                                # 仓库根（`ops/` 在这一层）
for p in (str(REPO), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from ops.analytics_export import contract as C          # noqa: E402
from ops.analytics_export import registry as reg        # noqa: E402
from ops.analytics_export import sections as S          # noqa: E402
from ops.analytics_export import write as W             # noqa: E402
from ops.analytics_export.manifest_view import ManifestView, to_micro  # noqa: E402
from ops.analytics_export.readers import RawSqlStore    # noqa: E402
from ops.analytics_export.vocabulary import (PINNED_VOCABULARY,  # noqa: E402
                                             classify_unknown)

DDL = """
CREATE TABLE shadow_schema (version INTEGER PRIMARY KEY);
CREATE TABLE shadow_experiments (experiment_id TEXT PRIMARY KEY, status TEXT,
  manifest_hash TEXT, body TEXT);
CREATE TABLE shadow_opportunities (experiment_id TEXT, opportunity_id TEXT, session TEXT,
  rank INTEGER, terminal TEXT, body TEXT, PRIMARY KEY (experiment_id, opportunity_id));
CREATE TABLE shadow_applications (experiment_id TEXT, scope TEXT, opportunity_id TEXT,
  action TEXT, decision_id TEXT, execution_applied INTEGER, body TEXT,
  PRIMARY KEY (experiment_id, scope, opportunity_id));
CREATE TABLE shadow_packets (experiment_id TEXT, opportunity_id TEXT, packet_id TEXT,
  body TEXT, PRIMARY KEY (experiment_id, opportunity_id));
CREATE TABLE shadow_account_state (experiment_id TEXT, scope TEXT, sequence INTEGER,
  state_hash TEXT, body TEXT, PRIMARY KEY (experiment_id, scope, sequence));
CREATE TABLE shadow_daily_nav (experiment_id TEXT, scope TEXT, session TEXT,
  revision INTEGER, equity INTEGER, cash_available INTEGER, gross_exposure INTEGER,
  fees INTEGER, valuation_status TEXT, sequence INTEGER, body TEXT);
CREATE TABLE shadow_job_runs (experiment_id TEXT, job_key TEXT, attempt INTEGER,
  status TEXT, fencing_token TEXT, body TEXT,
  PRIMARY KEY (experiment_id, job_key, attempt));
CREATE TABLE decision_events (event_id TEXT PRIMARY KEY, account_scope TEXT,
  event_type TEXT, observed_at TEXT, payload_hash TEXT, body TEXT);
"""


def build_ledger(path: Path, *, version: int, position: dict, nav_sessions=()):
    con = sqlite3.connect(path)
    con.executescript(DDL)
    exp = 'EXP-T'
    con.execute('INSERT INTO shadow_schema VALUES (?)', (version,))
    con.execute('INSERT INTO shadow_experiments VALUES (?,?,?,?)',
                (exp, 'FROZEN', 'hash-x', json.dumps({'experiment_id': exp})))
    body = {'scope': f'SHADOW:{exp}:R', 'sequence': 1, 'cash_available': 1000,
            'cash_reserved': 0, 'unsettled_cash': 0, 'dividend_receivable': {},
            'positions': {'SEC-US-X': position}, 'fees': 0, 'model_cost': 0,
            'initial_equity': 100000000, 'high_water': 100000000,
            'last_session': '2026-09-18', 'valuation_status': 'OK'}
    con.execute('INSERT INTO shadow_account_state VALUES (?,?,?,?,?)',
                (exp, f'SHADOW:{exp}:R', 1, 'h', json.dumps(body)))
    for i, s in enumerate(nav_sessions):
        nav = {'session': s, 'equity': 100000000, 'full_cost_equity': 100000000,
               'gross_exposure': 0, 'revision': 1, 'valuation_status': 'OK'}
        con.execute('INSERT INTO shadow_daily_nav VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (exp, f'SHADOW:{exp}:R', s, 1, nav['equity'], 0, 0, 0, 'OK', i, json.dumps(nav)))
    con.commit()
    con.close()
    return exp


class ZeroVsNullTests(unittest.TestCase):
    """需求 §7：只有**真实算出来的零**才能显示 0。"""

    def test_present_distinguishes_missing_key_from_zero(self):
        self.assertEqual(C.present({'x': 0}, 'x')['value'], 0)
        self.assertEqual(C.present({'x': 0}, 'x')['status'], C.OK)
        got = C.present({}, 'x')
        self.assertIsNone(got['value'])
        self.assertEqual(got['status'], C.NOT_COLLECTED)

    def test_metric_never_turns_none_into_zero(self):
        self.assertEqual(C.metric(None)['status'], C.NOT_COLLECTED)
        self.assertIsNone(C.metric(None)['value'])
        self.assertEqual(C.metric(0)['value'], 0)
        self.assertEqual(C.metric(0)['status'], C.OK)

    def test_position_rows_marks_absent_protection_fields_not_collected(self):
        """schema 9 里没有 v10 的保护位 ⇒ 「未采集」，**不是 0**。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'l.sqlite3'
            exp = build_ledger(p, version=9,
                               position={'security_id': 'SEC-US-X', 'shares': 6,
                                         'entry_price_micro': 934880000,
                                         'initial_stop_micro': 783168625,
                                         'stop_micro': 783168625, 'holding_sessions': 2,
                                         'entry_session': '2026-09-17'})
            store = RawSqlStore(p, exp)
            mv = ManifestView({'experiment_id': exp, 'status': 'FROZEN',
                               'account_scopes': [f'SHADOW:{exp}:R', f'SHADOW:{exp}:L'],
                               'initial_cash': 100000.0, 'llm_policy': {},
                               'execution_policy': {}, 'risk_protocol': {}})
            rp = S._report()
            rows = rp.position_rows(store, mv, PINNED_VOCABULARY, {})
            prot = rows['accounts'][f'SHADOW:{exp}:R']['positions'][0]['protection']
            self.assertEqual(prot['initial_stop_micro']['status'], C.OK)   # 存在 ⇒ 有值
            for key in ('pending_stop_micro', 'protection_activated',
                        'highest_completed_close_micro', 'initial_risk_micro'):
                self.assertEqual(prot[key]['status'], C.NOT_COLLECTED, key)
                self.assertIsNone(prot[key]['value'], key)

    def test_v10_position_fields_are_surfaced_as_values(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'l.sqlite3'
            exp = build_ledger(p, version=10,
                               position={'security_id': 'SEC-US-X', 'shares': 6,
                                         'entry_price_micro': 934880000,
                                         'stop_micro': 783168625, 'holding_sessions': 2,
                                         'entry_session': '2026-09-17',
                                         'initial_risk_micro': 90000000,
                                         'highest_completed_close_micro': 950000000,
                                         'protection_activated': True,
                                         'pending_stop_micro': 800000000,
                                         'pending_stop_effective_session': '2026-09-21'})
            store = RawSqlStore(p, exp)
            mv = ManifestView({'experiment_id': exp, 'status': 'FROZEN',
                               'account_scopes': [f'SHADOW:{exp}:R', f'SHADOW:{exp}:L'],
                               'initial_cash': 100000.0, 'llm_policy': {},
                               'execution_policy': {}})
            rp = S._report()
            rows = rp.position_rows(store, mv, PINNED_VOCABULARY, {'SEC-US-X': 900000000})
            pos = rows['accounts'][f'SHADOW:{exp}:R']['positions'][0]
            self.assertEqual(pos['protection']['pending_stop_micro']['value'], 800000000)
            self.assertTrue(pos['protection']['protection_activated']['value'])
            # 回吐 = 最大收益率 − 当前收益率 = (950/934.88 − 1) − (900/934.88 − 1)
            self.assertAlmostEqual(pos['giveback']['value_pp'],
                                   (950000000 - 900000000) / 934880000 * 100, places=4)


class ReplayForwardBoundaryTests(unittest.TestCase):
    """需求场景 7：初始化回放不得显示成前向业绩。"""

    def test_boundary_splits_nav_sessions(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            p = base / 'l.sqlite3'
            exp = build_ledger(p, version=10, position={},
                               nav_sessions=['2026-08-17', '2026-08-18', '2026-09-21'])
            entry = {'scope_id': f'paper:{exp}', 'kind': 'paper', 'experiment_id': exp,
                     'run_dir': str(base), 'manifest': None,
                     'ledger': 'l.sqlite3',
                     'accounts': [{'account_id': f'SHADOW:{exp}:R', 'role': 'R'}],
                     'replay_start_session': '2026-08-17',
                     'replay_end_session': '2026-08-18',
                     'forward_start_session': '2026-09-21',
                     'boundary_source': 'declared'}
            store = RawSqlStore(p, exp)
            navs = sorted({n['session'] for n in store.daily_nav(f'SHADOW:{exp}:R')})
            replay = [s for s in navs if s <= entry['replay_end_session']]
            forward = [s for s in navs if s > entry['replay_end_session']]
            self.assertEqual(replay, ['2026-08-17', '2026-08-18'])
            self.assertEqual(forward, ['2026-09-21'])
            self.assertEqual(len(replay) + len(forward), len(navs), '不丢不重')


class VocabularyTests(unittest.TestCase):
    def test_pinned_vocabulary_is_superset_of_package(self):
        """pin 的 `ABSTAIN_REASONS` 缺 `MODEL_BUDGET_EXHAUSTED` —— 这正是要超集的原因。"""
        from scripts.portfolio_shadow import llm_overlay as lo
        from ops.analytics_export import vocabulary as V
        self.assertEqual(set(lo.ABSTAIN_REASONS) - set(V.ABSTAIN_REASONS), set())
        for code in lo.ABSTAIN_REASONS:
            self.assertEqual(lo.is_program_abstain(code), PINNED_VOCABULARY.is_program_abstain(code))
            self.assertEqual(lo.program_abstain_class(code),
                             PINNED_VOCABULARY.program_abstain_class(code))

    def test_budget_exhausted_is_a_program_abstain(self):
        self.assertTrue(PINNED_VOCABULARY.is_program_abstain('MODEL_BUDGET_EXHAUSTED'))
        self.assertEqual(PINNED_VOCABULARY.program_abstain_class('MODEL_BUDGET_EXHAUSTED'),
                         'failure')

    def test_unknown_codes_are_reported_not_bucketed(self):
        self.assertEqual(classify_unknown({'NOT_A_REAL_CODE'}), ['NOT_A_REAL_CODE'])
        self.assertEqual(classify_unknown({'ABSTAIN', 'READY', 'INTENT_CREATED'}), [])

    def test_action_constants_match_schema(self):
        from scripts.portfolio_shadow import position_overlay as po
        from scripts.portfolio_shadow import schema as sc
        from ops.analytics_export import vocabulary as V
        self.assertEqual(set(V.PATH_CHANGING_ACTIONS),
                         set(po.PATH_CHANGING_ACTIONS))
        self.assertEqual(V.POSITION_HOLD, po.POSITION_HOLD)
        self.assertEqual(V.POSITION_TIGHTEN_NOT_APPLIED, po.POSITION_TIGHTEN_NOT_APPLIED)
        self.assertEqual(set(V.SHADOW_TERMINALS), set(sc.SHADOW_TERMINALS))
        self.assertEqual(set(V.ACCOUNT_ACTIONS), set(sc.ACCOUNT_ACTIONS))
        self.assertEqual(set(V.ATTEMPT_STATUSES), set(sc.ATTEMPT_STATUSES))


class ManifestUnitTests(unittest.TestCase):
    def test_initial_cash_matches_package_conversion(self):
        """`initial_cash` 文件里是美元、内存里是微美元 —— 差 1e6 会让收益率错一百万倍。"""
        from scripts.portfolio_shadow.cli import manifest_from_dict
        from scripts.portfolio_shadow.schema import to_micro
        d = json.loads((ROOT / 'data' / 'portfolio_shadow' /
                        'M1-FORWARD-S-20260917' / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(ManifestView(d).initial_cash, manifest_from_dict(d).initial_cash)
        for v in (0.01, 100000.0, 1.5, 99.999999):
            self.assertEqual(to_micro(v), to_micro(v))


class RegistryTests(unittest.TestCase):
    BASE = ROOT / 'data'

    def test_every_run_dir_on_disk_is_registered(self):
        """盘上每个实验目录都必须在 `scopes` 或 `unregistered`(带 why) 里。"""
        r = reg.load_registry()
        self.assertEqual(reg.check_completeness(self.BASE, r), [],
                         '有新实验落地但没登记 —— 加进 ops/analytics_scopes.json')
        for u in r.get('unregistered') or []:
            self.assertTrue(u.get('why'), f'{u["run_dir"]} 必须写明不登记的理由')

    def test_registry_has_no_glob_or_latest_pointer(self):
        """需求 §4：不按「最新文件」猜当前实验。"""
        text = reg.DEFAULT_REGISTRY.read_text(encoding='utf-8')
        for bad in ('*', 'latest', 'glob'):
            self.assertNotIn(f'"{bad}"', text, f'登记里出现了 {bad}')
        src = REPO / 'ops' / 'analytics_export' / 'registry.py'
        self.assertNotIn('import glob', src.read_text(encoding='utf-8'))

    def test_arm_scopes_derive_from_arms_json(self):
        """三臂的实验号从 `arms.json` 读，不另抄一份。"""
        base = self.BASE / 'strategy_diagnostics' / 'forward'
        arms = json.loads((base / 'arms.json').read_text(encoding='utf-8'))
        scopes = {s['scope_id']: s for s in reg.expand(self.BASE, reg.load_registry())}
        for arm, meta in arms['arms'].items():
            sid = f'paper:{meta["experiment_id"]}'
            self.assertIn(sid, scopes)
            self.assertEqual(scopes[sid]['forward_start_session'], arms['start_session'])


class FrozenHashPinTests(unittest.TestCase):
    """cherry-pick 到运行 checkout 时不能碰这 12 个冻结文件 —— 在这里先拦住。"""

    def test_frozen_hashes_match_runtime_pin(self):
        import hashlib
        pin = Path('/Users/wh1817w/quant-runtime-main/quant_us-main')
        if not pin.exists():
            self.skipTest('运行 checkout 不在本机')
        arms = json.loads((ROOT / 'data' / 'strategy_diagnostics' / 'forward' /
                           'arms.json').read_text(encoding='utf-8'))
        bad = []
        for rel, want in arms['frozen_code'].items():
            p = pin / rel
            got = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else 'MISSING'
            if got != want:
                bad.append(rel)
        self.assertEqual(bad, [], f'运行 checkout 的冻结文件被改过：{bad}')


class WriterTests(unittest.TestCase):
    def test_generation_is_atomic_and_pruned_with_guard(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'web_snapshots'
            for i in range(W.KEEP_GENERATIONS + 3):
                W.write_generation(root, f'g{i:02d}', {'index.json': {'i': i}})
            self.assertIsNotNone(W.read_latest(root))
            W.prune(root)
            left = sorted(p.name for p in (root / 'generation').glob('*'))
            self.assertEqual(len(left), W.KEEP_GENERATIONS)
            self.assertTrue((root / 'latest').is_symlink())
            # 守卫必须**精确**：根目录名不对就拒绝（子串匹配会被
            # `.../not_web_snapshots/generation/` 骗过 —— `data/` 是 symlink，删错就是打穿运行环境）
            outsider = Path(d) / 'other_root' / 'generation' / 'g0'
            outsider.mkdir(parents=True)
            with self.assertRaises(ValueError):
                W.prune(Path(d) / 'other_root', keep=0)
            self.assertTrue(outsider.exists(), '守卫拒绝时不得真的删掉')


class StrategyComparisonTests(unittest.TestCase):
    """第二批核心：四份研究归一成同一张表。

    这组测试的价值在**字段路径**：第一次实现时我把基线胜率写成
    `statistics.exits.realized_win_rate`，而它其实在 `comparison.exits` 里 ——
    结果是页面上静默显示「未采集」。下面第一条就是钉死这个的。
    """
    BASE = ROOT / 'data'

    @classmethod
    def setUpClass(cls):
        r = reg.load_registry()
        cls.rows = {}
        for e in reg.expand(cls.BASE, r):
            if e['kind'] != 'research':
                continue
            secs = S.research_sections(cls.BASE, e)
            cls.rows[e['scope_id']] = S.normalize_study(cls.BASE, e, secs['research'])

    def test_every_registered_study_is_recognized(self):
        kinds = {sid: row['kind'] for sid, row in self.rows.items()}
        self.assertEqual(sorted(kinds), sorted(self.rows))
        unknown = [sid for sid, k in kinds.items() if k is None]
        self.assertEqual(unknown, [], f'产物形状没被识别 —— 新产物要么映射、要么显式说明：{unknown}')
        self.assertEqual(sorted(set(kinds.values())),
                         ['baseline', 'entry_replacement', 'entry_sleeve', 'exit_protection'])

    def test_baseline_win_rate_comes_from_comparison_exits(self):
        row = self.rows['research:SD-P0P1-20260921-012']
        m = row['metrics']
        self.assertEqual(m['win_rate_pct']['status'], C.OK,
                         '胜率在 comparison.exits.realized_win_rate，不在 statistics 里')
        self.assertAlmostEqual(m['win_rate_pct']['value'], 52.79, places=1)
        self.assertAlmostEqual(m['return_pct']['value'], 266.15, places=1)
        self.assertAlmostEqual(m['mdd_pct']['value'], -13.87, places=1)

    def test_exit_protection_reports_both_arms_and_the_tradeoff(self):
        row = self.rows['research:SR-EXIT-PROTECT-20260921-001']
        self.assertIn('A_规则基线', row['arms'])
        self.assertIn('B_加利润保护', row['arms'])
        a = row['arms']['A_规则基线']
        b = row['arms']['B_加利润保护']
        # 胜率升（104→113 笔盈利）而净 R 降（161.9→90.8）—— 两件事必须同时可见
        self.assertGreater(b['profitable']['value'], a['profitable']['value'])
        self.assertLess(b['sum_net_r']['value'], a['sum_net_r']['value'])

    def test_delta_only_artifact_does_not_invent_absolute_numbers(self):
        """差额类产物只给差值 ⇒ 绝对值必须「未采集」，不得填 0 或替它合成。"""
        row = self.rows['research:SR-BOTTOM-20260921-001']
        self.assertEqual(row['kind'], 'entry_replacement')
        for key in ('return_pct', 'mdd_pct', 'win_rate_pct'):
            self.assertEqual(row['metrics'][key]['status'], C.NOT_COLLECTED, key)
            self.assertIsNone(row['metrics'][key]['value'], key)
        self.assertEqual(row['deltas']['terminal_return_pp']['status'], C.OK)
        self.assertAlmostEqual(row['deltas']['terminal_return_pp']['value'], -75.8, places=1)

    def test_every_row_carries_a_verifiable_source(self):
        for sid, row in self.rows.items():
            self.assertTrue(row['source'].get('file'), f'{sid} 缺来源文件')
            self.assertTrue(row['source'].get('sha256'), f'{sid} 缺产物哈希（结果要可溯源）')


if __name__ == '__main__':
    unittest.main()
