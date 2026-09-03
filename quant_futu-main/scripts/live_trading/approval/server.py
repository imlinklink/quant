"""
人工确认下单 - 本地确认页 + JSON API

只绑定 127.0.0.1，仅供本机使用，不对外网开放。
"""
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import urlparse

logger = logging.getLogger('approval')


PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>交易确认台</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; background: #f4f5f7; color: #222; }
  header { background: #1f2937; color: #fff; padding: 14px 22px;
           display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  header h1 { font-size: 18px; margin: 0; }
  .badge { padding: 3px 10px; border-radius: 12px; font-size: 13px; font-weight: 600; }
  .badge.real { background: #dc2626; color: #fff; }
  .badge.sim { background: #2563eb; color: #fff; }
  .badge.llm-on { background: #059669; color: #fff; }
  .badge.llm-off { background: #6b7280; color: #fff; }
  .muted { color: #9ca3af; font-size: 13px; margin-left: auto; }
  main { max-width: 1080px; margin: 20px auto; padding: 0 16px; }
  .empty { text-align: center; color: #6b7280; padding: 60px 0; font-size: 15px; }
  .card { background: #fff; border-radius: 10px; padding: 16px 18px; margin-bottom: 14px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); border-left: 5px solid #d1d5db; }
  .card.pending { border-left-color: #f59e0b; }
  .card.approved, .card.executing { border-left-color: #2563eb; }
  .card.executed { border-left-color: #059669; }
  .card.rejected, .card.expired, .card.failed, .card.skipped { opacity: .72; border-left-color: #9ca3af; }
  .row { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
  .code { font-size: 17px; font-weight: 700; }
  .name { font-size: 15px; color: #4b5563; }
  .status { margin-left: auto; font-size: 13px; padding: 2px 10px; border-radius: 10px;
            background: #f3f4f6; font-weight: 600; }
  .kv { font-size: 13px; color: #374151; margin: 6px 0; display: flex; gap: 18px; flex-wrap: wrap; }
  .kv b { color: #111827; }
  .section-title { font-size: 13px; font-weight: 700; color: #6b7280; margin: 10px 0 4px;
                   text-transform: uppercase; letter-spacing: .03em; }
  .reason-box, .llm-box { background: #f9fafb; border-radius: 8px; padding: 10px 12px;
                           font-size: 13px; line-height: 1.55; }
  .llm-box { border: 1px solid #e5e7eb; }
  .llm-verdict { font-weight: 700; }
  .verdict-allow, .verdict-pass { color: #059669; }
  .verdict-block, .verdict-veto { color: #dc2626; }
  .verdict-watch, .verdict-delay { color: #d97706; }
  .actions { display: flex; gap: 10px; margin-top: 10px; }
  button { border: 0; border-radius: 8px; padding: 8px 22px; font-size: 14px;
           font-weight: 600; cursor: pointer; }
  button.buy { background: #059669; color: #fff; }
  button.buy:hover { background: #047857; }
  button.reject { background: #ef4444; color: #fff; }
  button.reject:hover { background: #dc2626; }
  button:disabled { background: #d1d5db; cursor: not-allowed; }
  .countdown { color: #dc2626; font-size: 13px; }
  .note { font-size: 12px; color: #6b7280; margin-top: 6px; }
</style>
</head>
<body>
<header>
  <h1>交易确认台</h1>
  <span id="envBadge" class="badge">-</span>
  <span id="llmBadge" class="badge">-</span>
  <span id="clock" class="muted"></span>
</header>
<main>
  <div id="content"><div class="empty">加载中…</div></div>
</main>
<script>
const STATUS_TEXT = {
  pending: '待确认', approved: '已确认，准备下单', executing: '正在下单',
  executed: '已成交', rejected: '已拒绝', expired: '已过期', failed: '执行失败', skipped: '已跳过'
};
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtTs(sec) {
  if (!sec) return '-';
  const d = new Date(sec * 1000);
  return d.toLocaleString('zh-CN', { hour12: false });
}
function llmHtml(llm) {
  if (!llm) {
    return '<div class="llm-box"><span class="muted">大模型未启用或本轮无判定（纯规则信号）</span></div>';
  }
  if (!llm.verdict) {
    return '<div class="llm-box"><span class="muted">本轮大模型无判定</span>'
         + (llm.reason ? '<div>说明：' + esc(llm.reason) + '</div>' : '')
         + '</div>';
  }
  const v = esc(llm.verdict || 'allow');
  const cls = ('allow' === v || 'pass' === v) ? 'verdict-allow'
            : ('block' === v || 'veto' === v) ? 'verdict-block'
            : 'verdict-watch';
  const conf = llm.confidence == null ? '-' : (Number(llm.confidence) * 100).toFixed(0) + '%';
  return '<div class="llm-box"><div>大模型判定：<span class="llm-verdict ' + cls + '">' +
         esc(({allow:'允许买入',pass:'通过',watch:'观望',delay:'暂缓(轻仓)',block:'建议否决',veto:'建议否决'})[v] || v) +
         '</span> <span class="muted">(conf ' + conf + ', ' +
         esc({shadow:'影子模式', real_veto:'真实否决'}[llm.mode] || llm.mode || '') + ')</span></div>' +
         (llm.risk_level ? '<div>市场风险：' + esc(llm.risk_level) + '</div>' : '') +
         (llm.reason ? '<div>理由：' + esc(llm.reason) + '</div>' : '') +
         (llm.model ? '<div class="note">model: ' + esc(llm.model) + '</div>' : '') +
         '</div>';
}
function card(item, now) {
  const id = esc(item.id);
  const left = Math.max(0, (item.expires_at || 0) - now);
  const countdown = item.status === 'pending'
    ? '<span class="countdown">' + Math.ceil(left / 1000) + 's 后自动过期</span>' : '';
  const buttons = item.status === 'pending'
    ? '<div class="actions"><button class="buy" data-id="' + id + '" data-act="approve">下单</button>' +
      '<button class="reject" data-id="' + id + '" data-act="reject">拒绝</button></div>' : '';
  const kline = item.kline_score != null
    ? '<div>日内K线评分：<b>' + esc(item.kline_score) + '</b>（' + esc(item.kline_signal || '-') + '）</div>' : '';
  return '<div class="card ' + esc(item.status) + '">' +
    '<div class="row"><span class="code">' + esc(item.stock_code) + '</span>' +
    '<span class="name">' + esc(item.stock_name || '') + '</span>' +
    '<span class="status">' + esc(STATUS_TEXT[item.status] || item.status) + '</span></div>' +
    '<div class="kv"><span>价格 <b>' + esc(item.price) + '</b></span>' +
    '<span>数量 <b>' + esc(item.quantity) + '</b> 股</span>' +
    '<span>预计金额 <b>' + esc(item.estimated_cost) + '</b></span>' +
    (item.entry_mode ? '<span>入场方式 <b>' + esc(item.entry_mode) + '</b></span>' : '') +
    '<span>信号时间 ' + fmtTs(item.created_at) + '</span>' + countdown + '</div>' +
    (kline ? '<div class="kv">' + kline + '</div>' : '') +
    '<div class="section-title">规则触发理由</div>' +
    '<div class="reason-box">' + esc(item.reason || '无') + '</div>' +
    '<div class="section-title">大模型判定</div>' + llmHtml(item.llm) +
    (item.note ? '<div class="note">备注：' + esc(item.note) + '</div>' : '') +
    buttons + '</div>';
}
async function load() {
  try {
    const [statusR, propR] = await Promise.all([
      fetch('/api/status'), fetch('/api/proposals')
    ]);
    const st = await statusR.json();
    const data = await propR.json();
    const now = (data.server_time || Date.now() / 1000) * 1000;
    const envBadge = document.getElementById('envBadge');
    envBadge.textContent = st.env || '-';
    envBadge.className = 'badge ' + ((st.env || '').toUpperCase() === 'REAL' ? 'real' : 'sim');
    const llmBadge = document.getElementById('llmBadge');
    llmBadge.textContent = st.llm_enabled ? 'LLM 已接入' : 'LLM 未启用';
    llmBadge.className = 'badge ' + (st.llm_enabled ? 'llm-on' : 'llm-off');
    document.getElementById('clock').textContent =
      new Date(Date.now()).toLocaleString('zh-CN', { hour12: false }) +
      ' · 每 2s 自动刷新';
    const order = { pending: 0, approved: 1, executing: 2 };
    const items = (data.items || []).sort((a, b) =>
      (order[a.status] == null ? 9 : order[a.status]) - (order[b.status] == null ? 9 : order[b.status]) ||
      (b.created_at || 0) - (a.created_at || 0));
    const el = document.getElementById('content');
    if (!items.length) {
      el.innerHTML = '<div class="empty">暂无待确认的交易信号<br><span class="note">有买入信号时会自动出现在这里，等你点「下单」才真正执行。</span></div>';
      return;
    }
    el.innerHTML = items.map(i => card(i, now)).join('');
  } catch (e) {
    document.getElementById('content').innerHTML =
      '<div class="empty">连接失败：' + esc(e.message || e) + '</div>';
  }
}
document.addEventListener('click', async function (e) {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  btn.disabled = true;
  const act = btn.dataset.act;
  try {
    const r = await fetch('/api/proposals/' + encodeURIComponent(btn.dataset.id) + '/' + act,
      { method: 'POST' });
    const res = await r.json();
    if (!res.ok) alert(res.error || '操作失败');
  } catch (err) {
    alert('网络错误：' + err.message);
  } finally {
    load();
  }
});
load();
setInterval(load, 2000);
</script>
</body>
</html>
"""


class ApprovalServer:
    """本地确认页 HTTP 服务（只监听 127.0.0.1）。"""

    def __init__(
        self,
        store,
        host: str = '127.0.0.1',
        port: int = 8899,
        env: str = 'SIMULATE',
        market_type: str = 'HK',
        llm_enabled: bool = False,
    ):
        self.store = store
        self.host = host
        self.port = int(port)
        self.env = env
        self.market_type = market_type
        self.llm_enabled = bool(llm_enabled)
        self.httpd: Any = None
        self._thread: Any = None

    @property
    def bound_port(self) -> int:
        if self.httpd:
            return int(self.httpd.server_address[1])
        return self.port

    def start(self) -> bool:
        if self.httpd:
            return True

        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # 静默默认访问日志
                pass

            def do_GET(self):
                path = urlparse(self.path).path
                if path in ('/', '/index.html'):
                    self._send_text(PAGE_HTML, 'text/html; charset=utf-8')
                elif path == '/api/status':
                    self._send_json({
                        'ok': True,
                        'env': server_ref.env,
                        'market_type': server_ref.market_type,
                        'llm_enabled': server_ref.llm_enabled,
                        'port': server_ref.bound_port,
                        'server_time': time.time(),
                    })
                elif path == '/api/proposals':
                    items = server_ref.store.get_all()
                    self._send_json({'ok': True, 'server_time': time.time(), 'items': items})
                elif path == '/favicon.ico':
                    self.send_response(204)
                    self.end_headers()
                else:
                    self._send_json({'ok': False, 'error': 'not found'}, status=404)

            def do_POST(self):
                parts = [p for p in self.path.split('/') if p]
                if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'proposals':
                    pid, action = parts[2], parts[3]
                    note = ''
                    try:
                        length = int(self.headers.get('Content-Length', 0) or 0)
                        if length > 0:
                            body = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
                            note = body.get('note', '')
                    except Exception:
                        note = ''

                    if action == 'approve':
                        ok = server_ref.store.approve(pid)
                    elif action == 'reject':
                        ok = server_ref.store.reject(pid, note or '用户点击拒绝')
                    else:
                        self._send_json({'ok': False, 'error': 'unknown action'}, status=400)
                        return

                    if not ok:
                        item = server_ref.store.get(pid)
                        state = item['status'] if item else 'not_found'
                        self._send_json({
                            'ok': False,
                            'error': f'当前状态 {state} 不允许该操作',
                            'status': state,
                        }, status=409)
                        return
                    item = server_ref.store.get(pid)
                    self._send_json({'ok': True, 'status': item['status'] if item else action})
                else:
                    self._send_json({'ok': False, 'error': 'not found'}, status=404)

            def _send_text(self, text: str, ctype: str):
                data = text.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_json(self, payload: Dict, status: int = 200):
                data = json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        try:
            self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
            self.httpd.daemon_threads = True
            self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self._thread.start()
            logger.info(
                f"[Approval] 确认页已启动: http://{self.host}:{self.bound_port} "
                f"(env={self.env}, market={self.market_type}, llm={'on' if self.llm_enabled else 'off'})"
            )
            return True
        except Exception as e:
            logger.error(f"[Approval] 服务启动失败: {e}")
            self.httpd = None
            raise

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
            logger.info("[Approval] 确认页已停止")
