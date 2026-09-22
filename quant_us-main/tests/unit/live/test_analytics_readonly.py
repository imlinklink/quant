"""决策可视化页面的**只读证明**（需求场景 12）。

这是本项目第一条「GET 不写」的测试。此前没有任何测试断言过「一个 GET 路由不产生写入」，
而这条保证恰恰是需求 `decision-visibility-product-requirements-2026-09-22.md` 的硬要求。

**为什么必须这样测**：web 进程里有若干「看起来是读、其实是写」的路径 ——
`PositionRegistry.transaction()` 退出时**无条件** `INSERT OR REPLACE INTO books` + `commit`
（`position_registry.py`），`EventStore.transaction()` 每次读都跑 `migrate()` 并
`BEGIN IMMEDIATE`。所以「路由叫 GET」证明不了任何事，只能靠**断言连接与文件都没变**。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import web.analytics as analytics          # noqa: E402
import web.app as webapp                   # noqa: E402


def _snapshot_files(gen='2026-09-22T000000+0800'):
    env = {
        'envelope_version': 1, 'generation_id': gen, 'scope_id': 'paper:EXP-A',
        'scope_kind': 'paper', 'account_id': None, 'experiment_id': 'EXP-A',
        'strategy_version': {'parent_strategy_id': 'B3', 'parent_version': '1'},
        'as_of': '2026-09-18', 'generated_at': '2026-09-22T00:00:00+08:00',
        'source_ref': {'kind': 'paper', 'ledger': 'x.sqlite3', 'ledger_schema_on_disk': 9},
        'data_status': 'OK', 'missing_reasons': [],
        'sections': {
            'overview': {'accounts': [], 'period_activity': {}, 'in_progress': [], 'todo': []},
            'positions': {'positions': []},
            'opportunities': {'funnel': {'stages': []}, 'entry_metrics': {}},
            'llm_impact': {'roles': [], 'decision_table': [], 'paired': {}},
            'boundary': {'nav_replay_sessions': 0, 'nav_forward_sessions': 0},
        },
    }
    index = {'generation_id': gen, 'generated_at': '2026-09-22T00:00:00+08:00',
             'registry_version': 1, 'unregistered_run_dirs': [], 'failed': [],
             'scopes': [{'scope_id': 'paper:EXP-A', 'label': 'A', 'kind': 'paper',
                         'file': 'scope__paper__EXP-A.json', 'data_status': 'OK',
                         'as_of': '2026-09-18'}]}
    return {'index.json': index,
            'scope__paper__EXP-A.json': env,
            'experiments.json': {'generated_at': '2026-09-22T00:00:00+08:00',
                                 'research_cards': [], 'note': ''},
            'decisions/paper__EXP-A@opportunity_1.json': {'decision_id': 'x',
                                                          'opportunity_id': 'opportunity_1'}}


class AnalyticsReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        latest = self.tmp / 'latest'
        latest.mkdir(parents=True)
        for rel, payload in _snapshot_files().items():
            p = latest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        self._patch = mock.patch.object(analytics, 'SNAPSHOT_DIR', self.tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.client = webapp.app.test_client()
        # 造一个真的 sqlite 文件，用来证明「没有任何非只读连接碰过它」
        self.db = self.tmp / 'ledger.sqlite3'
        con = sqlite3.connect(self.db)
        con.execute('CREATE TABLE t (x INTEGER)')
        con.commit()
        con.close()

    def _fingerprint(self):
        st = self.db.stat()
        return (st.st_mtime_ns, st.st_size)

    def test_all_get_routes_perform_no_writes(self):
        """六个页面 + 四个接口：全 200/404、无网络、无非只读连接、文件未变。"""
        connections = []
        real_connect = sqlite3.connect

        def guard(*a, **kw):
            uri = str(a[0] if a else kw.get('database'))
            connections.append(uri)
            if 'mode=ro' not in uri:
                raise AssertionError(f'非只读 sqlite 连接: {uri}')
            return real_connect(*a, **kw)

        before_fp = self._fingerprint()
        before_paths = {p for p in self.tmp.rglob('*')}
        paths = ['/overview', '/positions', '/opportunities', '/llm-impact', '/experiments',
                 '/decisions/paper__EXP-A@opportunity_1',
                 '/api/analytics/scopes', '/api/analytics/experiments',
                 '/api/analytics/paper:EXP-A', '/api/analytics/paper:EXP-A?sort=giveback']
        with mock.patch.object(webapp, '_create_ctx',
                               side_effect=AssertionError('no network')), \
             mock.patch('sqlite3.connect', side_effect=guard):
            for p in paths:
                r = self.client.get(p)
                self.assertEqual(r.status_code, 200, f'{p} → {r.status_code}')
                self.assertNotIn(b'Traceback', r.data)
        self.assertEqual(connections, [], f'页面不该打开任何 sqlite 连接：{connections}')
        self.assertEqual(self._fingerprint(), before_fp, '账本文件被改写了')
        self.assertEqual({p for p in self.tmp.rglob('*')}, before_paths, '快照目录被增删了')

    def test_unknown_scope_is_404_not_fabricated(self):
        """需求场景 10：找不到来源要如实报错，**不返回伪造的空账户**。"""
        r = self.client.get('/api/analytics/nope:404')
        self.assertEqual(r.status_code, 404)
        body = r.get_json()
        self.assertFalse(body['ok'])
        self.assertIn('UNKNOWN_SCOPE', body['error'])

    def test_scope_without_file_reports_source_failure(self):
        """某范围本次导出失败 ⇒ 该卡是「读取失败 + 原因」，不是空数据。"""
        idx = json.loads((self.tmp / 'latest' / 'index.json').read_text(encoding='utf-8'))
        idx['scopes'][0]['file'] = None
        idx['scopes'][0]['error'] = "OperationalError('disk I/O error')"
        idx['scopes'][0]['data_status'] = 'READ_FAILED'
        (self.tmp / 'latest' / 'index.json').write_text(
            json.dumps(idx, ensure_ascii=False), encoding='utf-8')
        r = self.client.get('/api/analytics/paper:EXP-A')
        self.assertEqual(r.status_code, 503)
        self.assertIn('SOURCE_READ_FAILED', r.get_json()['error'])

    def test_path_traversal_is_refused(self):
        r = self.client.get('/api/analytics/decisions/..%2f..%2findex.json')
        self.assertIn(r.status_code, (404, 400, 308))
        self.assertNotIn(b'"scopes"', r.data)

    def test_missing_generation_is_empty_not_500(self):
        """首跑前没有 `latest`：页面 200 + 明确「还没有快照」，不是 500（部署当天必见）。"""
        with mock.patch.object(analytics, 'SNAPSHOT_DIR', self.tmp / 'nonexistent'):
            for p in ['/api/analytics/scopes']:
                r = self.client.get(p)
                self.assertEqual(r.status_code, 200)
            r = self.client.get('/api/analytics/experiments')
            self.assertEqual(r.status_code, 404)
            body = r.get_json()
            self.assertFalse(body['ok'], '没有快照时必须报错，不能返回伪造的空载荷')
            self.assertIn('NOT_FOUND', body['error'])


class TemplateSingleSourceTests(unittest.TestCase):
    """导航与状态标签**各只有一份定义**（本仓库因「同一件事两份定义」出过多次真 bug）。"""

    def setUp(self):
        self.tpl = ROOT / 'web' / 'templates'

    def test_nav_tabs_markup_lives_only_in_nav_partial(self):
        offenders = []
        for f in sorted(self.tpl.glob('*.html')):
            text = f.read_text(encoding='utf-8')
            if 'class="nav-tabs"' in text and f.name != '_nav.html':
                offenders.append(f.name)
        self.assertEqual(offenders, [], f'这些模板复制了 nav：{offenders}')

    def test_old_templates_include_the_shared_nav(self):
        for name in ('approvals.html', 'suggestions.html'):
            text = (self.tpl / name).read_text(encoding='utf-8')
            self.assertIn("{% include '_nav.html' %}", text, f'{name} 没有用共享 nav')

    def test_index_top_links_do_not_drift_from_nav(self):
        import re
        nav = (self.tpl / '_nav.html').read_text(encoding='utf-8')
        idx = (self.tpl / 'index.html').read_text(encoding='utf-8')
        block = re.search(r'<div class="top-links">(.*?)</div>', idx, re.S).group(1)
        idx_hrefs = set(re.findall(r'href="([^"]+)"', block))
        nav_hrefs = set(re.findall(r'href="([^"]+)"', nav))
        self.assertEqual(idx_hrefs - nav_hrefs, set(),
                         'index 的顶部链接出现了 nav 里没有的页面（两份定义已漂移）')

    def test_status_labels_defined_once(self):
        files = [f for f in self.tpl.glob('*.html')
                 if 'STATUS_LABELS' in f.read_text(encoding='utf-8')]
        self.assertEqual([f.name for f in files], ['_base.html'],
                         '九态中文映射必须只在 _base.html 里定义一份')


if __name__ == '__main__':
    unittest.main()
