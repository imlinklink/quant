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

_STATIC_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       margin: 0; background: #f4f5f7; color: #222; }
header { background: #1f2937; color: #fff; padding: 14px 22px;
         display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.badge { padding: 3px 10px; border-radius: 12px; font-size: 13px; font-weight: 600; }
.badge.real { background: #dc2626; color: #fff; }
.badge.sim { background: #2563eb; color: #fff; }
.badge.llm-on { background: #059669; color: #fff; }
.badge.llm-off { background: #6b7280; color: #fff; }
.muted { color: #6b7280; font-size: 13px; }
nav.tabs { display: flex; gap: 4px; align-items: center; margin-right: 12px; flex-wrap: wrap; }
nav.tabs a.tab { color: #cbd5e1; text-decoration: none; font-size: 13px;
                 padding: 4px 10px; border-radius: 8px; white-space: nowrap; }
nav.tabs a.tab:hover { background: #334155; color: #fff; }
nav.tabs a.tab.active { background: #2563eb; color: #fff; font-weight: 600; }
"""

_STATIC_JS = """
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
"""


def _nav_html(active: str) -> str:
    """统一顶部导航（确认台 / 持仓监控 / LLM建议），active 传当前路径。"""
    def _tab(path: str, label: str) -> str:
        cls = ' tab active' if active == path else ' tab'
        return f'<a class="{cls.strip()}" href="{path}">{label}</a>'
    return ('<nav class="tabs">'
            + _tab('/', '确认台')
            + _tab('/positions', '持仓监控')
            + _tab('/suggestions', 'LLM建议')
            + '</nav>')


def _inject_nav(page: str, active: str) -> str:
    """给单页 HTML 注入共享静态文件与导航 HTML，并移除旧的单链接/重复 esc。"""
    page = page.replace(
        '<a href="/positions" style="color:#38bdf8;text-decoration:none;font-size:13px;">持仓监控 →</a>',
        '')
    page = page.replace('<a class="link" href="/">← 返回买入确认台</a>', '')
    page = page.replace('<a href="/">买入确认台 →</a>', '')
    page = page.replace('<head>', '<head>\n'
                                   '<link rel="stylesheet" href="/static/style.css">', 1)
    page = page.replace('<script>', '<script src="/static/app.js"></script>\n<script>', 1)
    # 移除三页各自的 esc 定义（统一由 /static/app.js 提供）
    esc_block = """function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
"""
    page = page.replace(esc_block, '')
    esc_oneline = """function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
"""
    page = page.replace(esc_oneline, '')
    return page.replace('<header>', '<header>\n  ' + _nav_html(active), 1)


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
  .brief { max-width: 1080px; margin: 14px auto -4px; padding: 10px 18px;
           border-radius: 8px; font-size: 13px; line-height: 1.5;
           background: #fff; border: 1px solid #e5e7eb; }
  .brief.cautious { border-left: 4px solid #f59e0b; }
  .brief.defensive { border-left: 4px solid #ef4444; }
  .brief.normal { border-left: 4px solid #059669; }
</style>
</head>
<body>
<header>
  <h1>交易确认台</h1>
  <span id="envBadge" class="badge">-</span>
  <span id="llmBadge" class="badge">-</span>
  <a href="/positions" style="color:#38bdf8;text-decoration:none;font-size:13px;">持仓监控 →</a>
  <span id="clock" class="muted"></span>
</header>
<div id="brief" class="brief" style="display:none"></div>
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
function llmHtml(llm, side) {
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
  const buyLabels = {allow:'允许买入', pass:'通过', watch:'观望', delay:'暂缓(轻仓)', block:'建议否决', veto:'建议否决'};
  const sellLabels = {allow:'建议卖出', watch:'可分批/观望', delay:'可分批兑现', block:'建议继续持有', veto:'建议继续持有'};
  const label = (side === 'sell' ? sellLabels : buyLabels)[v] || v;
  return '<div class="llm-box"><div>大模型判定：<span class="llm-verdict ' + cls + '">' +
         esc(label) +
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
  const isSell = item.side === 'sell';
  const countdown = item.status === 'pending'
    ? '<span class="countdown">' + Math.ceil(left / 1000) + 's 后自动' +
      (isSell ? '卖出' : '过期') + '</span>' : '';
  const buttons = item.status === 'pending'
    ? '<div class="actions"><button class="buy" data-id="' + id + '" data-act="approve">' +
      (isSell ? '卖出' : '下单') + '</button>' +
      '<button class="reject" data-id="' + id + '" data-act="reject">' +
      (isSell ? '继续持有' : '拒绝') + '</button></div>' : '';
  const kline = item.kline_score != null
    ? '<div>日内K线评分：<b>' + esc(item.kline_score) + '</b>（' + esc(item.kline_signal || '-') + '）</div>' : '';
  return '<div class="card ' + esc(item.status) + '">' +
    '<div class="row"><span class="code">' + esc(item.stock_code) + '</span>' +
    '<span class="name">' + esc(item.stock_name || '') + '</span>' +
    '<span class="status">' + esc(STATUS_TEXT[item.status] || item.status) + '</span></div>' +
    '<div class="kv"><span>价格 <b>' + esc(item.price) + '</b></span>' +
    '<span>数量 <b>' + esc(item.quantity) + '</b> 股</span>' +
    '<span>预计金额 <b>' + esc(item.estimated_cost) + '</b></span>' +
    '<span>方向 <b>' + (isSell ? '卖出' : '买入') + '</b></span>' +
    (item.entry_mode ? '<span>入场方式 <b>' + esc(item.entry_mode) + '</b></span>' : '') +
    '<span>信号时间 ' + fmtTs(item.created_at) + '</span>' + countdown + '</div>' +
    (kline ? '<div class="kv">' + kline + '</div>' : '') +
    '<div class="section-title">规则触发理由</div>' +
    '<div class="reason-box">' + esc(item.reason || '无') + '</div>' +
    (item.context ? '<div class="section-title">消息面上下文</div>' +
      '<div class="reason-box" style="white-space:pre-line">' + esc(item.context) + '</div>' : '') +
    '<div class="section-title">大模型判定</div>' + llmHtml(item.llm, item.side) +
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
async function loadBrief() {
  try {
    const r = await fetch('/api/market-brief');
    const d = await r.json();
    const b = d.brief || {};
    const el = document.getElementById('brief');
    if (!b.generated_at) { el.style.display = 'none'; return; }
    const riskLabel = {normal:'正常', cautious:'谨慎', defensive:'防御'}[b.risk_level] || b.risk_level;
    const freqLabel = {normal:'按计划买入', reduce:'减少买入', avoid:'今日不新增买入'}[b.buy_frequency] || b.buy_frequency;
    el.className = 'brief ' + (b.risk_level || 'normal');
    el.style.display = 'block';
    el.innerHTML = '📋 盘前市场状态：<b>' + esc(riskLabel) + '</b> · 买入策略：<b>' + esc(freqLabel) +
      '</b>' + (b.risk_note ? '<br>' + esc(b.risk_note) : '');
  } catch (e) { }
}
load();
loadBrief();
setInterval(load, 2000);
setInterval(loadBrief, 30000);
</script>
</body>
</html>
"""


PAGE_HTML = _inject_nav(PAGE_HTML, '/')

POSITIONS_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>港股持仓监控</title>
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
  .muted { color: #9ca3af; font-size: 13px; }
  a.link { color: #38bdf8; text-decoration: none; font-size: 13px; }
  .right { margin-left: auto; }
  main { max-width: 1100px; margin: 20px auto; padding: 0 16px; }
  .summary { background: #fff; border-radius: 10px; padding: 12px 18px; margin-bottom: 14px;
             box-shadow: 0 1px 3px rgba(0,0,0,.08); font-size: 14px; }
  .empty { text-align: center; color: #6b7280; padding: 60px 0; font-size: 15px; }
  table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px;
          overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); font-size: 14px; }
  th, td { padding: 10px 12px; text-align: right; border-bottom: 1px solid #eef0f3;
           white-space: nowrap; }
  th { background: #f8fafc; color: #6b7280; font-weight: 600; font-size: 12px; }
  td:first-child, th:first-child { text-align: left; }
  .code { font-weight: 700; }
  .name { color: #6b7280; font-size: 13px; }
  .pos { color: #dc2626; }
  .neg { color: #059669; }
  .status { font-size: 12px; padding: 2px 8px; border-radius: 10px; font-weight: 600; }
  .st-trigger { background: #fee2e2; color: #b91c1c; }
  .st-watch { background: #dcfce7; color: #15803d; }
  .st-manual { background: #e0f2fe; color: #0369a1; }
  .updated { font-size: 12px; color: #9ca3af; }
</style>
</head>
<body>
<header>
  <h1>港股持仓监控</h1>
  <span id="envBadge" class="badge">-</span>
  <a class="link" href="/">← 返回买入确认台</a>
  <span id="updated" class="muted right"></span>
</header>
<main>
  <div class="summary" id="summary">加载中…</div>
  <div id="content"></div>
</main>
<script>
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function pct(v) {
  return (v == null) ? '-' : (v * 100 >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%';
}
function fmtPrice(v) {
  return (v == null || v === 0) ? '-' : Number(v).toFixed(3);
}
function statusHtml(item) {
  const text = item.status_text || (item.should_exit ? '🔴 触发退出' : '🟢 观察中');
  const cls = item.should_exit ? 'st-trigger' : (item.manual ? 'st-manual' : 'st-watch');
  return '<span class="status ' + cls + '">' + esc(text) + '</span>';
}
function rows(positions) {
  return positions.map(function (p) {
    const pnlCls = (p.return_pct || 0) >= 0 ? 'neg' : 'pos';
    const reason = p.reason ? '<div class="updated">' + esc(p.reason) + '</div>' : '';
    return '<tr>' +
      '<td><div class="code">' + esc(p.stock_code) + '</div>' +
           '<div class="name">' + esc(p.stock_name || '') +
           (p.manual ? ' · 手动' : ' · 策略') + '</div></td>' +
      '<td>' + esc(p.quantity) + '</td>' +
      '<td>' + fmtPrice(p.cost_price) + '</td>' +
      '<td><b>' + fmtPrice(p.price) + '</b></td>' +
      '<td class="' + pnlCls + '"><b>' + pct(p.return_pct) + '</b><div class="updated">' +
           (p.profit_amount != null ? (p.profit_amount >= 0 ? '+' : '') + Number(p.profit_amount).toFixed(0) : '') +
           '</div></td>' +
      '<td>' + fmtPrice(p.highest_price) + '</td>' +
      '<td>' + fmtPrice(p.atr) + '</td>' +
      '<td>' + fmtPrice(p.take_profit_price) + '</td>' +
      '<td>' + fmtPrice(p.stop_loss_price) + '</td>' +
      '<td>' + statusHtml(p) + reason + '</td>' +
      '</tr>';
  }).join('');
}
async function load() {
  try {
    const r = await fetch('/api/positions');
    const data = await r.json();
    const envBadge = document.getElementById('envBadge');
    envBadge.textContent = data.env || '-';
    envBadge.className = 'badge ' + (String(data.env || '').toUpperCase() === 'REAL' ? 'real' : 'sim');
    const positions = data.positions || [];
    const last = positions.reduce(function (m, p) { return Math.max(m, p.updated_at || 0); }, 0);
    document.getElementById('updated').textContent = '最近更新: ' + (last
      ? new Date(last * 1000).toLocaleString('zh-CN', { hour12: false }) : '-') +
      ' · 3s 自动刷新';
    const sum = document.getElementById('summary');
    if (data.ok === false) {
      sum.textContent = '⚠️ ' + (data.error || '监控数据未就绪');
    } else {
      sum.textContent = '当前持仓 ' + positions.length + ' 只' +
        (String(data.env || '').toUpperCase() === 'REAL'
          ? '（实盘：触发止盈止损会自动卖出）' : '（模拟/观察模式）');
    }
    const el = document.getElementById('content');
    if (!positions.length) {
      el.innerHTML = '<div class="empty">暂无持仓<br><span class="muted">账户有持仓且持仓检查循环运行后，这里会显示实时状态。</span></div>';
      return;
    }
    el.innerHTML = '<table><thead><tr>' +
      '<th>股票</th><th>持仓</th><th>成本价</th><th>现价</th><th>盈亏</th>' +
      '<th>最高价</th><th>ATR</th><th>止盈价</th><th>止损价</th><th>状态</th>' +
      '</tr></thead><tbody>' + rows(positions) + '</tbody></table>';
  } catch (e) {
    document.getElementById('content').innerHTML =
      '<div class="empty">连接失败：' + esc(e.message || e) + '</div>';
  }
}
load();
setInterval(load, 3000);
</script>
</body>
</html>
"""


POSITIONS_HTML = _inject_nav(POSITIONS_HTML, '/positions')

SUGGESTIONS_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>大模型选股建议 - 港股</title>
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; background: #f4f5f7; color: #222; }
  header { background: #1f2937; color: #fff; padding: 14px 22px;
           display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  header h1 { font-size: 18px; margin: 0; }
  a { color: #38bdf8; text-decoration: none; font-size: 13px; }
  .muted { color: #9ca3af; font-size: 13px; margin-left: auto; }
  main { max-width: 960px; margin: 20px auto; padding: 0 16px; }
  .empty { text-align: center; color: #6b7280; padding: 60px 0; }
  .summary { background: #fff; border-radius: 10px; padding: 14px 18px; margin-bottom: 14px;
             box-shadow: 0 1px 3px rgba(0,0,0,.08); font-size: 14px; line-height: 1.6; }
  .card { background: #fff; border-radius: 10px; padding: 14px 18px; margin-bottom: 12px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); border-left: 5px solid #d97706; }
  .card.US { border-left-color: #2563eb; }
  .card.added, .card.ignored { opacity: .55; }
  .row { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
  .mkt { font-size: 12px; padding: 2px 8px; border-radius: 10px; font-weight: 700;
         background: #e0f2fe; color: #0369a1; }
  .code { font-weight: 700; font-size: 16px; }
  .status { margin-left: auto; font-size: 13px; color: #6b7280; }
  .box { background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px;
         padding: 10px 12px; font-size: 13px; margin: 8px 0; line-height: 1.55; }
  .actions { display: flex; gap: 10px; }
  button { border: 0; border-radius: 8px; padding: 8px 20px; font-size: 14px;
           font-weight: 600; cursor: pointer; }
  button.add { background: #059669; color: #fff; }
  button.ignore { background: #6b7280; color: #fff; }
  .tips { font-size: 12px; color: #6b7280; margin-top: 14px; line-height: 1.7; }
</style>
</head>
<body>
<header>
  <h1>大模型选股建议</h1>
  <a href="/">买入确认台 →</a>
  <span class="muted" id="meta"></span>
</header>
<main>
  <div class="summary" id="summary">加载中…</div>
  <div id="content"></div>
  <div class="tips">建议来源：宏观日报（盘前+盘后）→ 大模型（美股+港股候选混排）。
  点「加入观察池」会写入对应市场的观察池：美股 → dip_buy.watch_list，
  港股 → hk.watch_list（港股运行中会自动补拉K线参与选股，无需重启）；
  之后仍需到买入确认台人工点单。</div>
</main>
<script>
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function displayCode(c){
  const m=String(c.market||'').toUpperCase();
  const code=String(c.code||'');
  return (m && code.toUpperCase().indexOf(m+'.')===0) ? code.slice(m.length+1) : code;
}
function card(c){
  const st=c.status||'pending';
  const btns=st==='pending'
    ?'<div class="actions"><button class="add" data-id="'+esc(c.id)+'">加入观察池</button>'+
     '<button class="ignore" data-id="'+esc(c.id)+'">忽略</button></div>':'';
  return '<div class="card '+esc(c.market)+' '+esc(st)+'">'+
    '<div class="row"><span class="mkt">'+esc(c.market)+'</span>'+
    '<span class="code">'+esc(displayCode(c))+'</span><span>'+esc(c.name||'')+'</span>'+
    '<span>'+esc(c.direction||'-')+' · conf '+esc(((Number(c.confidence||0)*100).toFixed(0)+'%'))+
    ' · '+esc(c.horizon||'-')+'</span>'+
    '<span class="status">'+esc(st==='added'?'已加入':st==='ignored'?'已忽略':'')+'</span></div>'+
    '<div class="box">'+esc(c.rationale||'-')+'</div>'+
    (c.risks?'<div class="box">风险：'+esc(c.risks)+'</div>':'')+btns+'</div>';
}
async function load(){
  try{
    const r=await fetch('/api/suggestions');const d=await r.json();
    const data=d.data||{};const cs=data.candidates||[];
    document.getElementById('meta').textContent=new Date().toLocaleString('zh-CN',{hour12:false})+
      ' · '+cs.length+' 条 · 港股池 '+(d.hk_watch||[]).length+' 只';
    const s=document.getElementById('summary');
    if(!data.generated_at){s.innerHTML='还没有生成建议。先运行：<b>python scripts/live_trading/llm_suggestions/run_suggestions.py</b>';}
    else{s.innerHTML='<b>宏观结论：</b>'+esc(data.summary||'-')+
      '<br><span class="muted">生成于 '+esc(data.generated_at)+' · 盘前 '+
      esc((data.reports||{}).pre?'✓':'无')+' · 盘后 '+esc((data.reports||{}).post?'✓':'无')+'</span>';}
    const el=document.getElementById('content');
    el.innerHTML=cs.length?cs.map(card).join(''):'<div class="empty">暂无建议候选</div>';
  }catch(e){document.getElementById('content').innerHTML='<div class="empty">连接失败：'+esc(e.message||e)+'</div>';}
}
document.addEventListener('click',async function(e){
  const btn=e.target.closest('button[data-id]');if(!btn)return;btn.disabled=true;
  const action=btn.classList.contains('add')?'add':'ignore';
  try{
    const r=await fetch('/api/suggestions/'+encodeURIComponent(btn.dataset.id)+'/'+action,{method:'POST'});
    const res=await r.json();
    if(res.message)alert(res.message);
    if(!res.ok&&res.error)alert(res.error);
  }catch(err){alert('网络错误：'+err.message);}finally{load();}
});
load();setInterval(load,5000);
</script>
</body>
</html>
"""


SUGGESTIONS_HTML = _inject_nav(SUGGESTIONS_HTML, '/suggestions')


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
        positions_provider=None,
        on_approved=None,
        on_demo_proposal=None,
        on_demo_exit=None,
        on_demo_sell=None,
        on_watch_added=None,
    ):
        self.store = store
        self.host = host
        self.port = int(port)
        self.env = env
        self.market_type = market_type
        self.llm_enabled = bool(llm_enabled)
        self.positions_provider = positions_provider
        self.on_approved = on_approved
        self.on_demo_proposal = on_demo_proposal
        self.on_demo_exit = on_demo_exit
        self.on_demo_sell = on_demo_sell
        self.on_watch_added = on_watch_added
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
                elif path == '/positions':
                    self._send_text(POSITIONS_HTML, 'text/html; charset=utf-8')
                elif path == '/suggestions':
                    self._send_text(SUGGESTIONS_HTML, 'text/html; charset=utf-8')
                elif path == '/api/suggestions':
                    try:
                        from scripts.live_trading.llm_suggestions import load_latest
                        from scripts.live_trading.llm_suggestions import watchlist
                        payload = {
                            'ok': True,
                            'data': load_latest(),
                            'hk_watch': watchlist.current_hk_watch(),
                            'us_watch': watchlist.current_us_watch(),
                        }
                    except Exception as e:
                        payload = {'ok': False, 'data': {'candidates': []}, 'error': str(e)}
                    payload['server_time'] = time.time()
                    self._send_json(payload)
                elif path == '/api/market-brief':
                    try:
                        from scripts.live_trading import market_brief
                        payload = {'ok': True, 'brief': market_brief.load_brief()}
                    except Exception as e:
                        payload = {'ok': False, 'brief': {}, 'error': str(e)}
                    payload['server_time'] = time.time()
                    self._send_json(payload)
                elif path == '/api/positions':
                    payload = {}
                    if server_ref.positions_provider is not None:
                        try:
                            payload = server_ref.positions_provider() or {}
                        except Exception as e:
                            payload = {'ok': False, 'positions': [], 'error': str(e)}
                    else:
                        payload = {
                            'ok': False,
                            'positions': [],
                            'error': '持仓快照未就绪（数据源未接入，需先启动实盘管理器）',
                        }
                    positions = payload.get('positions') or []
                    payload['positions'] = positions
                    payload.setdefault('count', len(positions))
                    payload['server_time'] = time.time()
                    self._send_json(payload)
                elif path == '/static/style.css':
                    self._send_text(_STATIC_CSS, 'text/css; charset=utf-8')
                elif path == '/static/app.js':
                    self._send_text(_STATIC_JS, 'text/javascript; charset=utf-8')
                elif path == '/favicon.ico':
                    self.send_response(204)
                    self.end_headers()
                else:
                    self._send_json({'ok': False, 'error': 'not found'}, status=404)

            def do_POST(self):
                parts = [p for p in self.path.split('/') if p]
                if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'suggestions':
                    pid, action = parts[2], parts[3]
                    try:
                        from scripts.live_trading.llm_suggestions import load_latest, update_item_status
                        from scripts.live_trading.llm_suggestions import watchlist
                        data = load_latest()
                        item = next(
                            (c for c in data.get('candidates', []) if c.get('id') == pid), None
                        )
                        if item is None:
                            self._send_json({'ok': False, 'error': '建议不存在'}, status=404)
                            return
                        if action == 'add':
                            valid, vmsg = watchlist.validate_futu_symbol(item.get('code', ''))
                            if not valid:
                                self._send_json({'ok': False, 'error': vmsg}, status=400)
                                return
                            if item.get('market') == 'HK':
                                added = watchlist.add_hk_watch(item.get('code', ''))
                            else:
                                added = watchlist.add_us_watch(item.get('code', ''))
                            if added and server_ref.on_watch_added is not None:
                                try:
                                    # 同步进运行中内存观察池，下一轮热加入自动补K线，
                                    # 无需重启港股服务
                                    server_ref.on_watch_added(
                                        str(item.get('code', '')).upper(),
                                        str(item.get('market', '')).upper(),
                                    )
                                except Exception as e:
                                    logger.warning(f'[LLM选股] 内存观察池同步失败: {e}')
                            update_item_status(pid, 'added')
                            self._send_json({
                                'ok': True,
                                'message': '已加入观察池' if added else '已在观察池中或格式不正确',
                                'added': added,
                            })
                            return
                        if action == 'ignore':
                            update_item_status(pid, 'ignored')
                            self._send_json({'ok': True, 'message': '已忽略'})
                            return
                        self._send_json({'ok': False, 'error': 'unknown action'}, status=400)
                    except Exception as e:
                        self._send_json({'ok': False, 'error': str(e)}, status=500)
                    return
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
                    # 点「下单」后的可选回调（周末演示模式由 manager 注入）
                    if action == 'approve' and server_ref.on_approved is not None:
                        try:
                            server_ref.on_approved(pid)
                        except Exception as e:
                            logger.warning(f'[Approval] 下单后回调异常: {e}')
                    item = server_ref.store.get(pid)
                    self._send_json({'ok': True, 'status': item['status'] if item else action})
                    return
                # ── 周末演示模式（仅本机；manager 回调内部会校验 SIMULATE）──
                if len(parts) == 3 and parts[0] == 'api' and parts[1] == 'dev':
                    try:
                        length = int(self.headers.get('Content-Length', 0) or 0)
                        body = {}
                        if length > 0:
                            body = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
                    except Exception:
                        body = {}
                    if parts[2] == 'simulate-proposal':
                        if server_ref.on_demo_proposal is None:
                            self._send_json({'ok': False, 'error': '演示钩子未注入'}, status=400)
                            return
                        code = str(body.get('code', '')).strip().upper()
                        try:
                            item = server_ref.on_demo_proposal(code)
                            self._send_json({'ok': True, 'item': item})
                        except Exception as e:
                            self._send_json({'ok': False, 'error': str(e)}, status=400)
                        return
                    if parts[2] == 'simulate-exit':
                        if server_ref.on_demo_exit is None:
                            self._send_json({'ok': False, 'error': '演示钩子未注入'}, status=400)
                            return
                        code = str(body.get('code', '')).strip().upper()
                        try:
                            price = float(body.get('exit_price') or 0)
                        except (TypeError, ValueError):
                            price = 0.0
                        try:
                            ok = server_ref.on_demo_exit(code, price)
                            self._send_json({'ok': bool(ok), 'code': code,
                                             'exit_price': price if ok else None})
                        except Exception as e:
                            self._send_json({'ok': False, 'error': str(e)}, status=400)
                        return
                    if parts[2] == 'simulate-sell':
                        if server_ref.on_demo_sell is None:
                            self._send_json({'ok': False, 'error': '演示钩子未注入'}, status=400)
                            return
                        code = str(body.get('code', '')).strip().upper()
                        reason = str(body.get('reason', '模拟止盈触发（演示）'))
                        try:
                            try:
                                pnl_pct = float(body.get('pnl_pct')) \
                                    if body.get('pnl_pct') is not None else None
                            except (TypeError, ValueError):
                                pnl_pct = None
                            item = server_ref.on_demo_sell(code, reason, pnl_pct)
                            self._send_json({'ok': True, 'item': item})
                        except Exception as e:
                            self._send_json({'ok': False, 'error': str(e)}, status=400)
                        return
                    self._send_json({'ok': False, 'error': 'unknown dev action'}, status=400)
                    return
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
