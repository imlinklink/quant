"""页面渲染的**真实路径**验证：接口里的持仓/动作/金额，确实出现在页面上。

用户 review 的原话：「验证『接口里的持仓、动作和金额确实出现在页面上』」。
只断言「200 + 字节数」是抓不到字段路径错的 —— 实测就这样漏过一个 P1：
接口返回顶层 `sections`，而四个页面读 `env.sections` ⇒ **有数据也显示成假空白**，
而所有 Python 侧的断言全绿。

做法：把页面渲染出的 `<script>` 抽出来，在 node 里用一个最小 DOM/fetch 桩跑
`window.__render(真实接口载荷)`，断言产出的 HTML 里真有那些值。**跳过**当 node 不存在
（不让 Python 测试套件硬依赖 node）。
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import web.app as webapp          # noqa: E402

NODE = shutil.which('node')

HARNESS = r'''
const fs = require('fs');
// argv[0]=node, [1]=本文件, [2]=页面脚本, [3]=接口载荷
const payload = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const els = {};
function el() {
  return {innerHTML: '', textContent: '', style: {}, dataset: {},
          querySelectorAll: () => [], addEventListener() {}, querySelector: () => null};
}
global.document = {
  getElementById: id => (els[id] = els[id] || el()),
  querySelectorAll: () => [],
};
global.window = global;
global.location = {pathname: '/', search: ''};
global.fetch = async () => ({ok: true, status: 200, json: async () => payload});
const src = fs.readFileSync(process.argv[2], 'utf8');
eval(src);
const out = global.window.__render(payload);
process.stdout.write(typeof out === 'string' ? out : '');
process.exit(0);   // 不等异步的 boot()，只验同步渲染结果
'''


@unittest.skipIf(NODE is None, 'node 不可用')
class PageRenderTests(unittest.TestCase):
    """跑真实渲染路径：接口载荷 → 页面 HTML。"""

    def _render(self, path, payload_path):
        client = webapp.app.test_client()
        html = client.get(path).data.decode('utf-8')
        scripts = re.findall(r'<script\b[^>]*>(.*?)</script>', html, flags=re.S | re.I)
        self.assertTrue(scripts, f'{path} 没有任何脚本')
        with tempfile.TemporaryDirectory() as d:
            js = Path(d) / 'page.js'
            js.write_text('\n'.join(scripts), encoding='utf-8')
            hz = Path(d) / 'harness.js'
            hz.write_text(HARNESS, encoding='utf-8')
            r = subprocess.run([NODE, str(hz), str(js), str(payload_path)],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, f'node 渲染失败：{r.stderr[-600:]}')
        return r.stdout

    def _payload(self, scope_id):
        client = webapp.app.test_client()
        resp = client.get(f'/api/analytics/{scope_id}')
        self.assertEqual(resp.status_code, 200, f'{scope_id} 接口不可用')
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(resp.get_json(), f, ensure_ascii=False)
            return f.name

    def test_api_scope_envelope_carries_sections(self):
        """接口必须把 sections 放在**页面读的那个路径**上。

        这条是那次「假空白」事故的直接回归：返回 `{'envelope': <无 sections>, 'sections': ...}`
        时，页面读 `env.sections` 拿到 undefined，而接口明明有数据。
        """
        client = webapp.app.test_client()
        body = client.get('/api/analytics/paper:M1-FORWARD-S-20260917').get_json()
        self.assertIn('envelope', body)
        self.assertIn('sections', body['envelope'],
                      '页面读 env.sections —— 接口必须就在那里给')
        sections = body['envelope']['sections']
        self.assertTrue(sections, 'sections 不能是空的')
        for key in ('overview', 'positions', 'opportunities', 'llm_impact'):
            self.assertIn(key, sections)

    def test_positions_page_shows_the_actual_holding(self):
        """M1 的 LITE 持仓（股数/入场价/止损）必须真的渲染到页面上。"""
        import os
        payload = self._payload('paper:M1-FORWARD-S-20260917')
        try:
            html = self._render('/positions', payload)
        finally:
            os.unlink(payload)
        self.assertIn('SEC-US-LITE', html, '持仓证券没出现在页面上')
        self.assertIn('934.88', html, '入场价没出现在页面上')
        self.assertIn('783.17', html, '止损没出现在页面上')
        # 「未采集」的保护位也要显示成未采集，而不是 0
        self.assertIn('未采集', html)

    def test_overview_page_shows_accounts_and_activity(self):
        import os
        payload = self._payload('paper:M1-FORWARD-S-20260917')
        try:
            html = self._render('/overview', payload)
        finally:
            os.unlink(payload)
        self.assertIn('SHADOW:M1-FORWARD-S-20260917:R', html, '账户没出现在页面上')
        self.assertIn('99,', html, '净值没出现在页面上')          # $99,970.57
        self.assertIn('检查', html, '「需要处理」没有列出做过哪些检查')

    def test_llm_impact_page_shows_the_decision_row(self):
        import os
        payload = self._payload('paper:M1-FORWARD-S-20260917')
        try:
            html = self._render('/llm-impact', payload)
        finally:
            os.unlink(payload)
        self.assertIn('ABSTAIN', html, '决策清单里的动作没出现')
        self.assertIn('DECISION_DEADLINE_MISSED', html, '原因码没出现')

    def test_nav_chart_marks_the_replay_forward_boundary(self):
        """需求场景 7：净值图必须把初始化回放与前向分开，不能画成一条。

        L1 的全部净值都落在前向起点之前 ⇒ 图必须说「全部是初始化回放，还没有前向数据」。
        （第一版把方向搞反了：这种情况下写成「全部为前向」—— 正是场景 7 要防的误读，
        而这条测试当场抓到了它。）
        """
        import os
        payload = self._payload('paper:L1-POSITION-20260922')
        try:
            html = self._render('/llm-impact', payload)
        finally:
            os.unlink(payload)
        self.assertIn('初始化回放', html, '图上没有区分回放与前向')
        self.assertIn('还没有前向数据', html, '没有前向数据时必须明说，不能写成"全部为前向"')
        self.assertIn('2026-09-21', html, '图上没有标出前向起点')
        self.assertNotIn('全部为前向', html, '有回放数据时不得声称"全部为前向"')

    def test_nav_chart_says_all_forward_when_boundary_precedes_data(self):
        """M1 的净值全在前向起点之后 ⇒ 应显示「全部为前向」，且不画边界线。"""
        import os
        payload = self._payload('paper:M1-FORWARD-S-20260917')
        try:
            html = self._render('/llm-impact', payload)
        finally:
            os.unlink(payload)
        self.assertIn('全部为前向', html)
        self.assertNotIn('初始化回放（非前向业绩）', html)


if __name__ == '__main__':
    unittest.main()
