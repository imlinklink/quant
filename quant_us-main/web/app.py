"""
quant_us Web 服务
包含：
- K线分析（支持盘前/盘中/盘后/夜盘）
- 实时行情监控
"""
import os
import sys
import logging
import time
import yaml
from typing import Tuple
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify, render_template

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from futu import OpenQuoteContext, KLType, RET_OK, Session
import threading
from contextlib import contextmanager
from queue import Queue, Empty


# ─── 连接池 ───────────────────────────────────────────────
_POOL_SIZE = 4
_quote_pool: Queue = Queue(maxsize=_POOL_SIZE)
_pool_lock = threading.Lock()
_pool_init_done = False


def _create_ctx() -> OpenQuoteContext:
    """创建新的 quote context（线程安全）"""
    return OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)


def _init_pool():
    """初始化连接池（首次调用时执行）"""
    global _pool_init_done
    with _pool_lock:
        if _pool_init_done:
            return
        for _ in range(_POOL_SIZE):
            try:
                _quote_pool.put_nowait(_create_ctx())
            except Exception as e:
                logger.warning(f'连接池初始化失败: {e}')
        _pool_init_done = True


@contextmanager
def quote_ctx():
    """线程安全的 quote context 上下文管理器（从池中借出/归还）"""
    _init_pool()
    ctx = None
    try:
        try:
            ctx = _quote_pool.get(timeout=5)
        except Empty:
            ctx = _create_ctx()
            logger.debug('连接池为空，新建临时连接')
        yield ctx
    finally:
        if ctx is not None:
            try:
                _quote_pool.put_nowait(ctx)
            except Exception:
                try:
                    ctx.close()
                except Exception:
                    pass

app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['JSON_AS_ASCII'] = False

logger = logging.getLogger('web')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)

# ─── 人工确认下单（Human-in-the-loop）───────────────────────────
# run_all.py 会把与 DipBuyMonitor 共用的提案存储注入到这里；
# web_only / 独立运行时也会在 load_config() 里自建（没有监控器写入则为空列表）。
approval_store = None
approval_enabled = False
approval_env = 'DRY-RUN'
approval_llm_enabled = False
dip_monitor_ref = None
trend_monitor_ref = None
exit_manager_ref = None

# ─── 配置加载 ──────────────────────────────────────────────────────
APP_CONFIG = {
    'buy_threshold': 10,  # 运行时可通过 /api/kline-analysis?threshold= 覆盖
    'decision_account_scope': 'DRY-RUN',
}

def load_config():
    """从 config.yaml 加载默认阈值（启动时一次性读）"""
    global approval_store, approval_enabled, approval_env, approval_llm_enabled
    config_path = os.path.join(BASE_DIR, 'config.yaml')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
        dip = cfg.get('dip_buy', {})
        # 兼容老的 strong/normal 两档配置
        APP_CONFIG['buy_threshold'] = dip.get('buy_threshold', dip.get('strong_buy_threshold', 10))
        ha = cfg.get('trading', {}).get('live_trading', {}).get('human_approval', {})
        approval_enabled = True
        approval_env = str(cfg.get('live_manager', {}).get('trd_env', 'SIMULATE'))
        approval_llm_enabled = bool(cfg.get('llm', {}).get('enabled', False))
        APP_CONFIG['decision_account_scope'] = str(
            (((cfg.get('llm_decision') or {}).get('engine_v2') or {})
             .get('account_scope') or 'DRY-RUN'))
        if approval_enabled and approval_store is None:
            from scripts.live_trading.approval.proposal_store import ProposalStore
            approval_store = ProposalStore(
                ttl_seconds=float(ha.get('proposal_ttl_seconds', 180))
            )
    except Exception as e:
        logger.warning(f'加载 config.yaml 失败，使用默认值: {e}')

load_config()

# ─── Futu 连接配置 ───────────────────────────────────────────────
FUTU_HOST = '127.0.0.1'
FUTU_PORT = 11111

# ─── 时区工具 ────────────────────────────────────────────────────
def get_et_now():
    """获取美东时间（自动处理夏令时）—— 向后兼容"""
    return datetime.now().astimezone(ZoneInfo("America/New_York"))

def get_market_timezone(code: str):
    """根据股票代码返回对应时区
    - US.* → 美东（America/New_York）
    - HK.* → 香港（Asia/Hong_Kong）
    - SH.*/SZ.* → 北京（Asia/Shanghai）
    """
    code = code.upper()
    if code.startswith('HK.'):
        return ZoneInfo('Asia/Hong_Kong')
    elif code.startswith('SH.') or code.startswith('SZ.'):
        return ZoneInfo('Asia/Shanghai')
    else:
        return ZoneInfo('America/New_York')

def get_market_now(code: str):
    """获取指定市场当前时间"""
    return datetime.now().astimezone(get_market_timezone(code))

def get_market_session(code: str = 'US.SOXL'):
    """返回指定市场当前时段
    - 美股: pre_market / regular / after_hours / overnight / closed
    - 港股: pre_market / morning / lunch / afternoon / closed
    - A股: pre_market / morning / lunch / afternoon / closed
    """
    now = get_market_now(code)
    mkt_time = now.hour * 60 + now.minute
    code = code.upper()
    
    if code.startswith('HK.') or code.startswith('SH.') or code.startswith('SZ.'):
        # 港股/A股：09:30-12:00 上午, 13:00-16:00 下午
        # 盘前集合竞价 09:00-09:30
        if 9 * 60 <= mkt_time < 9 * 60 + 30:
            return 'pre_market'
        elif 9 * 60 + 30 <= mkt_time < 12 * 60:
            return 'regular'  # 上午盘
        elif 12 * 60 <= mkt_time < 13 * 60:
            return 'closed'  # 午休
        elif 13 * 60 <= mkt_time < 16 * 60:
            return 'regular'  # 下午盘
        else:
            return 'closed'
    else:
        # 美股：全时段
        if 4 * 60 <= mkt_time < 9 * 60 + 30:   # 04:00-09:30 ET
            return 'pre_market'
        elif 9 * 60 + 30 <= mkt_time < 16 * 60:  # 09:30-16:00 ET
            return 'regular'
        elif 16 * 60 <= mkt_time < 20 * 60:     # 16:00-20:00 ET
            return 'after_hours'
        elif mkt_time >= 20 * 60 or mkt_time < 4 * 60:  # 20:00-04:00 ET 夜盘
            return 'overnight'
        else:
            return 'closed'

SESSION_LABELS = {
    'pre_market': '🌅 盘前',
    'regular': '📈 盘中',
    'after_hours': '📉 盘后',
    'overnight': '🌙 夜盘',
    'closed': '🌙 已休市',
}

# ─── K线数据获取 ──────────────────────────────────────────────────
def get_kline_5m(code: str, days: int = 2) -> tuple:
    """
    获取5分钟K线（支持 US/HK）
    - US: 美东时间，全时段（盘前/盘中/盘后/夜盘）
    - HK: 香港时间，正常交易时段
    返回 (DataFrame, error_msg)
    """
    with quote_ctx() as ctx:
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        
        # 港股用普通时段（无盘前盘后）
        if code.upper().startswith('HK.'):
            ret, data, _ = ctx.request_history_kline(
                code,
                start=start_date,
                end=end_date,
                ktype=KLType.K_5M,
                extended_time=False,  # 港股无盘前盘后
            )
        else:
            # 美股用全时段
            ret, data, _ = ctx.request_history_kline(
                code,
                start=start_date,
                end=end_date,
                ktype=KLType.K_5M,
                extended_time=True,
                session=Session.ALL,
            )
        
        if ret != RET_OK:
            return None, f"获取K线失败: {data}"
        
        if data is None or len(data) == 0:
            return None, "无K线数据"
        
        # 转换时间列
        if 'time_key' in data.columns:
            data['time_key'] = pd.to_datetime(data['time_key'])
        
        return data, None

def get_market_snapshot_price(code: str) -> float:
    """获取当前价（根据时段选择正确字段）"""
    with quote_ctx() as ctx:
        ret, snap = ctx.get_market_snapshot([code])
        if ret != RET_OK or snap.empty:
            return 0

        r = snap.iloc[0]
        session = get_market_session(code)

        if session == 'overnight':
            # 夜盘：优先用 overnight_price
            return r.get('overnight_price', 0) or r.get('last_price', 0)
        elif session == 'pre_market':
            return r.get('pre_price', 0) or r.get('last_price', 0)
        elif session == 'regular':
            return r.get('last_price', 0)
        elif session == 'after_hours':
            return r.get('after_price', 0) or r.get('overnight_price', 0) or r.get('last_price', 0)
        else:
            # closed: 兜底用昨收
            return r.get('last_price', 0)

def get_prev_close(code: str) -> float:
    """获取昨收价"""
    with quote_ctx() as ctx:
        ret, data, _ = ctx.request_history_kline(
            code,
            start=(datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d'),
            end=datetime.now().strftime('%Y-%m-%d'),
            ktype=KLType.K_5M,
            extended_time=True,
            session=Session.ALL,
        )
        if ret == RET_OK and data is not None and len(data) > 0:
            return float(data.iloc[-1].get('last_close', 0))
        return 0

# ─── 指标计算 ────────────────────────────────────────────────────
import numpy as np
import pandas as pd

def analyze_bars(df: pd.DataFrame, current_price: float) -> dict:
    """
    分析K线，返回评分和指标（薄封装，委托给 canonical analyze_score）
    阈值从 APP_CONFIG['buy_threshold'] 读取
    """
    from mutifactor.utils.intraday_scoring import analyze_score
    return analyze_score(
        df=df,
        current_price=current_price,
        buy_threshold=APP_CONFIG['buy_threshold'],
    )


# ─── API 路由 ────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')



@app.route('/api/market-status')
def api_market_status():
    """市场状态（兼容：默认查美股，但返回三大市场时间）"""
    et = get_et_now()
    session = get_market_session('US.SOXL')
    label = SESSION_LABELS.get(session, '未知')
    
    # 三个市场的当前时间
    now_bj = datetime.now().astimezone(ZoneInfo('Asia/Shanghai'))
    now_hk = datetime.now().astimezone(ZoneInfo('Asia/Hong_Kong'))
    
    return jsonify({
        'success': True,
        'et_time': et.strftime('%Y-%m-%d %H:%M:%S'),
        'bj_time': now_bj.strftime('%Y-%m-%d %H:%M:%S'),
        'hk_time': now_hk.strftime('%Y-%m-%d %H:%M:%S'),
        'session': session,
        'session_label': label,
        'bj_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })

@app.route('/api/kline-analysis')
def api_kline_analysis():
    """
    K线分析接口
    参数: stock_code, date (可选，默认今天)
    改为滚动窗口：返回最近2天K线（夜盘刚开时也能正常显示）
    """
    stock_code = request.args.get('stock_code', '').strip().upper()
    date_str = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    # 阈值：请求参数优先于配置默认值（范围1-15）
    threshold_arg = request.args.get('threshold')
    try:
        threshold = int(threshold_arg) if threshold_arg else None
        if threshold is not None and 1 <= threshold <= 15:
            APP_CONFIG['buy_threshold'] = threshold
    except (TypeError, ValueError):
        pass

    if not stock_code:
        return jsonify({'success': False, 'error': '请填写股票代码'})
    
    # 1. 获取K线（含盘前盘后夜盘，过去5天）
    df, err = get_kline_5m(stock_code, days=5)
    if err:
        return jsonify({'success': False, 'error': err})
    
    if len(df) == 0:
        return jsonify({'success': False, 'error': '无K线数据'})
    
    # 2. 不再过滤当日，直接用滚动窗口（最近2天）
    # 3. 获取昨收价
    prev_close = float(df.iloc[0].get('last_close', 0)) or get_prev_close(stock_code)
    
    # 4. 获取当前价
    current_price = get_market_snapshot_price(stock_code)
    
    # 5. 分析（用全部数据）
    analysis = analyze_bars(df, current_price)
    
    # 6. 准备返回数据
    session = get_market_session(stock_code)
    session_labels = {
        'pre_market': '盘前',
        'regular': '盘中',
        'after_hours': '盘后',
        'closed': '已休市',
    }
    
    # 标记每根K线的时段（按市场时区判断：A股/港股/美股）
    mkt_tz = get_market_timezone(stock_code)
    is_cn = stock_code.upper().startswith(('SH.', 'SZ.'))
    is_hk = stock_code.upper().startswith('HK.')
    bars_data = []
    for _, row in df.iterrows():
        tk = row['time_key']
        
        # time_key 无 tzinfo，按对应市场本地时间解析
        tk_mkt = tk.replace(tzinfo=mkt_tz)
        mkt_hour = tk_mkt.hour
        mkt_min = tk_mkt.minute
        mkt_time = mkt_hour * 60 + mkt_min
        
        if is_cn or is_hk:
            # A股/港股：09:30-12:00 上午, 13:00-16:00 下午
            if 9 * 60 + 30 <= mkt_time < 12 * 60:
                period = 'regular'
                period_label = '📈 上午盘'
            elif 13 * 60 <= mkt_time < 16 * 60:
                period = 'regular'
                period_label = '📉 下午盘'
            else:
                period = 'closed'
                period_label = '🌙 休市'
        else:
            # 美股：全时段
            if 0 <= mkt_time < 4 * 60:
                period = 'overnight'
                period_label = '🌙 夜盘'
            elif 4 * 60 <= mkt_time < 9 * 60 + 30:
                period = 'pre_market'
                period_label = '🌅 盘前'
            elif 9 * 60 + 30 <= mkt_time < 16 * 60:
                period = 'regular'
                period_label = '📈 盘中'
            else:
                period = 'after_hours'
                period_label = '📉 盘后'
        
        # 转北京时间展示
        tk_bj = tk_mkt.astimezone(ZoneInfo('Asia/Shanghai'))
        tk_mkt_str = tk_mkt.strftime('%Y-%m-%d %H:%M')
        
        bars_data.append({
            'time': tk_bj.strftime('%Y-%m-%d %H:%M'),  # 默认北京时间
            'time_et': tk_mkt_str,  # 市场本地时间（美东/香港）
            'time_et_short': tk_mkt.strftime('%H:%M'),
            'period': period,
            'period_label': period_label,
            'open': float(row['open']),
            'high': float(row['high']),
            'low': float(row['low']),
            'close': float(row['close']),
            'volume': float(row['volume']),
        })
    
    # 7. 信号列表（遍历每根K线，对每根bar用截至该bar的所有数据评分）
    signals = []
    cumulative_df = pd.DataFrame()
    for bar in bars_data:
        # 累加K线（用市场本地时间匹配，因为 df['time_key'] 是市场时区）
        row_bar = df[df['time_key'].dt.strftime('%Y-%m-%d %H:%M') == bar['time_et']]
        cumulative_df = pd.concat([cumulative_df, row_bar], ignore_index=True)
        
        if len(cumulative_df) < 20:
            continue  # 数据太少不评分
        
        bar_analysis = analyze_bars(cumulative_df, bar['close'])
        
        if bar_analysis['signal'] == 'buy':
            signals.append({
                'time': bar['time'],
                'period_label': bar['period_label'],
                'price': bar['close'],
                'score': bar_analysis['score'],
                'signal': bar_analysis['signal'],
                'rsi': bar_analysis['rsi'],
                'bb_position': bar_analysis['bb_position'],
                'volume_score': bar_analysis['volume_score'],
                'volume_divergence_score': bar_analysis['volume_divergence_score'],
                'drawdown': bar_analysis.get('drawdown', {}),
                'atr_pct': bar_analysis.get('atr_pct', 0),
            })
    
    return jsonify({
        'success': True,
        'stock_code': stock_code,
        'prev_close': prev_close,
        'current_price': current_price,
        'session': session,
        'session_label': SESSION_LABELS.get(session, '未知'),
        'bars': bars_data,
        'analysis': analysis,
        'signals': signals,
        'total_bars': len(bars_data),
        'total_signals': len(signals),
    })


# ─── 人工确认下单 API ────────────────────────────────────────────

@app.route('/approvals')
def approvals_page():
    """人工确认页：显示待确认买入（规则理由 + 大模型判定），点单后才下单"""
    return render_template('approvals.html')


@app.route('/api/approvals')
def api_approvals():
    items = approval_store.get_all() if approval_store is not None else []
    from mutifactor.llm.trade_review import approval_binding
    from scripts.live_trading.decision_ledger.decision_health import unapprovable_reason
    for item in items:
        item['llm_ready'] = approval_store.llm_ready(item)
        item['approval_binding'] = approval_binding(item) if item.get('plan_id') else None
        code, label = unapprovable_reason(item)
        item['unapprovable_reason'] = code
        item['unapprovable_reason_label'] = label
    return jsonify({
        'ok': True,
        'enabled': approval_enabled,
        'env': approval_env,
        'llm_enabled': approval_llm_enabled,
        'server_time': datetime.now().timestamp(),
        'items': items,
    })


@app.route('/api/decision-health')
def api_decision_health():
    """只读决策健康摘要：不触发 LLM 请求、批准或下单。"""
    if approval_store is None:
        return jsonify({'ok': False, 'error': '审批未启用'}), 400
    from scripts.live_trading.decision_ledger.decision_health import build_health
    health = build_health(
        approval_store.events.events(),
        llm_enabled=approval_llm_enabled,
        proposals=approval_store.get_all(),
        scope=approval_store.events.scope,
    )
    from scripts.live_trading.project_decision_metrics import ProjectDecisionMetrics
    metrics = ProjectDecisionMetrics(_decision_registry())
    health['decision_engine'] = metrics.health()
    health['selection_counterfactual'] = metrics.selection_metrics().get('counterfactual', {})
    health['entry_counterfactual'] = metrics.entry_metrics().get('counterfactual', {})
    health['position_counterfactual'] = metrics.position_metrics().get('counterfactual', {})
    return jsonify({'ok': True, 'health': health, 'server_time': datetime.now().timestamp()})


@app.route('/api/market-brief')
def api_market_brief():
    from scripts.live_trading import market_brief
    brief = market_brief.load_brief()
    return jsonify({
        'ok': True,
        'brief': brief,
        'server_time': datetime.now().timestamp(),
    })


@app.route('/api/approvals/<proposal_id>/<action>', methods=['POST'])
def api_approval_action(proposal_id: str, action: str):
    """用户点击「下单 / 拒绝」"""
    if approval_store is None:
        return jsonify({'ok': False, 'error': '人工确认未启用'}), 400

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({'ok': False, 'error': '请求必须为JSON对象'}), 400
    note = str(body.get('note') or '').strip()

    if action == 'approve':
        try:
            ok = approval_store.approve(proposal_id, note, binding=body.get('binding'))
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 409
    elif action == 'reject':
        ok = approval_store.reject(proposal_id, note or '用户点击拒绝')
    else:
        return jsonify({'ok': False, 'error': 'unknown action'}), 400

    if not ok:
        item = approval_store.get(proposal_id)
        state = item['status'] if item else 'not_found'
        return jsonify({
            'ok': False,
            'error': f'当前状态 {state} 不允许操作；请检查计划版本、评估有效期，反对/暂缓建议需填写覆盖理由',
            'status': state,
        }), 409

    item = approval_store.get(proposal_id)
    return jsonify({'ok': True, 'status': item['status'] if item else action})


@app.route('/api/approvals/<proposal_id>/revise-plan', methods=['POST'])
def api_revise_plan(proposal_id):
    if approval_store is None:
        return jsonify({'ok': False, 'error': '审批未启用'}), 400
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or not isinstance(body.get('changes',{}), dict):
        return jsonify({'ok': False, 'error': '修订必须为JSON对象'}), 400
    try:
        item = approval_store.revise_plan(proposal_id, body.get('changes') or {}, str(body.get('reason') or ''))
        owner = exit_manager_ref if item.get('side') == 'sell' else (
            dip_monitor_ref if item.get('entry_mode') == 'dip_buy' else trend_monitor_ref)
        if owner is not None:
            from scripts.live_trading.decision_ledger.workflow import start_review
            start_review(owner, item)
        return jsonify({'ok': True, 'item': item})
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 409


@app.route('/api/approvals/<proposal_id>/input')
def api_approval_input(proposal_id):
    item = approval_store.get(proposal_id) if approval_store else None
    if not item or not item.get('input_snapshot_id'):
        return jsonify({'ok': False, 'error': '无结构化输入快照'}), 404
    return jsonify({'ok': True, 'snapshot': approval_store.events.get_snapshot('input', item['input_snapshot_id'])})


@app.route('/api/decision-report')
def api_decision_report():
    if approval_store is None:
        return jsonify({'ok': False, 'error': '审批未启用'}), 400
    from scripts.live_trading.decision_ledger.funnel_report import build_funnel
    return jsonify({'ok': True, 'report': build_funnel(approval_store.events.events())})


# ─── 临时开发接口：模拟提案（仅 DRY-RUN + 本机）────────────────────────

@app.route('/api/dev/simulate-proposal', methods=['POST'])
def api_dev_simulate_proposal():
    """生成一条待确认的“模拟提案”，用于演示 人工点单→登记簿→吊灯出场 全流程。
    仅允许本机访问且 approval_env=DRY-RUN（防止误在真实环境产生提案）。
    """
    global approval_store, approval_env, dip_monitor_ref, trend_monitor_ref
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({'ok': False, 'error': '仅允许本机调用'}), 403
    if approval_store is None:
        return jsonify({'ok': False, 'error': '人工确认未启用'}), 400
    if approval_env != 'DRY-RUN':
        return jsonify({'ok': False, 'error': '仅 DRY-RUN 环境允许创建模拟提案'}), 403

    body = request.get_json(silent=True) or {}
    code = str(body.get('code', '')).strip().upper()
    mode = str(body.get('mode', 'dip_buy')).strip().lower()
    if not code.startswith('US.') or not code[3:]:
        return jsonify({'ok': False, 'error': 'code 需为 US.XXXX'}), 400
    if mode not in ('dip_buy', 'donchian'):
        return jsonify({'ok': False, 'error': 'mode 需为 dip_buy 或 donchian'}), 400

    # 价格：可用接口传值，否则取当前真实行情价（保证点单时漂移复检能过）
    price = None
    try:
        price = float(body.get('price') or 0)
    except (TypeError, ValueError):
        price = None
    if not price or price <= 0:
        for ref in (dip_monitor_ref, trend_monitor_ref):
            if ref is not None:
                try:
                    p = ref._get_current_price(code)
                except Exception:
                    p = None
                if p and p > 0:
                    price = float(p)
                    break
    if not price or price <= 0:
        return jsonify({'ok': False, 'error': '无法获取当前价格，请显式传 price'}), 502

    qty = int(5000 / price)
    if qty <= 0:
        return jsonify({'ok': False, 'error': '价格过高，数量为0'}), 400

    item = approval_store.create(
        stock_code=code,
        stock_name=f'{code}（模拟）',
        market_type='US',
        env='DRY-RUN',
        price=round(price, 4),
        quantity=qty,
        estimated_cost=round(price * qty, 2),
        per_stock_capital=5000.0,
        entry_mode=mode,
        trigger_reason='模拟测试',
        kline_signal='sim_test',
        reason=(f"【模拟测试】{mode} 策略线待确认提案 @ ${price:.2f} x {qty}股。"
                "点「下单」将走完整 DRY-RUN：登记持仓簿 → ChandelierExitManager 接管 → 模拟挂单/平仓。"),
        llm=None,
        expires_at=time.time() + 30 * 60,
    )
    return jsonify({'ok': True, 'item': item})


@app.route('/api/dev/simulate-price', methods=['POST'])
def api_dev_simulate_price():
    """开发/周末演示用：给出场管理器的价格订阅器注入模拟价格。
    仅 DRY-RUN + 本机；设 price<=0 表示清除强制价。
    """
    global exit_manager_ref, approval_env
    if request.remote_addr not in ('127.0.0.1', '::1'):
        return jsonify({'ok': False, 'error': '仅允许本机调用'}), 403
    if approval_env != 'DRY-RUN':
        return jsonify({'ok': False, 'error': '仅 DRY-RUN 环境允许'}), 403
    body = request.get_json(silent=True) or {}
    code = str(body.get('code', '')).strip().upper()
    try:
        price = float(body.get('price') or 0)
    except (TypeError, ValueError):
        price = 0.0
    if not code:
        return jsonify({'ok': False, 'error': '缺少 code'}), 400
    em = exit_manager_ref
    if em is None or not hasattr(em, 'ticker'):
        return jsonify({'ok': False, 'error': '出场管理器未注入'}), 400
    em.ticker.force_price(code, price)
    return jsonify({'ok': True, 'code': code, 'price': price})


# ─── LLM 选股建议页（宏观日报 → 候选 → 一键加入观察池）────────────

@app.route('/suggestions')
def suggestions_page():
    return render_template('suggestions.html')


@app.route('/api/suggestions')
def api_suggestions():
    from scripts.live_trading.llm_suggestions import load_latest
    from scripts.live_trading.llm_suggestions import watchlist
    from scripts.live_trading.llm_suggestions.freshness import assess
    from scripts.live_trading.llm_suggestions.store import load_latest_research_batch

    data = load_latest()
    # 时效阈值可配置；缺省用 freshness 模块默认值。GET 不触发模型刷新。
    fresh_cfg = {}
    try:
        config_path = os.path.join(BASE_DIR, 'config.yaml')
        with open(config_path, 'r', encoding='utf-8') as f:
            _cfg = yaml.safe_load(f) or {}
        fresh_cfg = _cfg.get('llm_suggestions', {}) or {}
    except Exception:
        pass
    data = assess(data, fresh_cfg)
    return jsonify({
        'ok': True,
        'data': data,
        # 研究候选（LLM 排序，非交易信号）与可执行交易提案分开
        'research': load_latest_research_batch(),
        'us_watch': watchlist.current_us_watch(),
        'hk_watch': watchlist.current_hk_watch(),
        'server_time': datetime.now().timestamp(),
    })


@app.route('/api/suggestions/<suggestion_id>/<action>', methods=['POST'])
def api_suggestion_action(suggestion_id: str, action: str):
    from scripts.live_trading.llm_suggestions import load_latest, update_item_status
    from scripts.live_trading.llm_suggestions import watchlist

    data = load_latest()
    item = next((c for c in data.get('candidates', []) if c.get('id') == suggestion_id), None)
    if not item:
        return jsonify({'ok': False, 'error': '建议不存在（先运行生成器）'}), 404

    if action == 'add':
        valid, vmsg = watchlist.validate_futu_symbol(item.get('code', ''))
        if not valid:
            return jsonify({'ok': False, 'error': vmsg}), 400
        added = False
        if item.get('market') == 'US':
            added = watchlist.add_us_watch(item.get('code', ''))
            # 美股系统正在运行时，立即加入当前监控，不用重启
            global dip_monitor_ref
            if dip_monitor_ref is not None:
                code = str(item.get('code', '')).upper()
                if code and code not in list(dip_monitor_ref.watch_codes):
                    dip_monitor_ref.watch_codes.append(code)
                    added = True
                    logger.info(f'[LLM选股] 已热加入当前美股监控: {code}')
        elif item.get('market') == 'HK':
            added = watchlist.add_hk_watch(item.get('code', ''))
        update_item_status(suggestion_id, 'added')
        msg = '已加入观察池' if added else '已在观察池中（或代码格式不正确）'
        return jsonify({'ok': True, 'message': msg, 'added': added})

    if action == 'ignore':
        update_item_status(suggestion_id, 'ignored')
        return jsonify({'ok': True, 'message': '已忽略'})

    return jsonify({'ok': False, 'error': 'unknown action'}), 400


# ─── LLM 决策评估 API（PR7，只读，不触发模型/下单）────────────────────────

def _decision_registry():
    """决策账本所在的 registry（复用审批存储的，否则全局默认）。"""
    if approval_store is not None and getattr(approval_store, 'registry', None) is not None:
        registry = approval_store.registry
        # web/app.py 独立启动时全局 Registry 尚未由券商账户配置；只读 API
        # 使用 Selection shadow 的显式 scope 查询同一个 SQLite 文件。
        if registry.namespace == 'unconfigured':
            from scripts.live_trading.position_registry import PositionRegistry
            return PositionRegistry(registry.path, APP_CONFIG['decision_account_scope'])
        return registry
    from scripts.live_trading.position_registry import REGISTRY
    return REGISTRY


@app.route('/api/llm/decisions')
def api_llm_decisions():
    from scripts.live_trading.decision_ledger.decision_run_store import DecisionRunStore
    from scripts.live_trading.project_decision_metrics import ProjectDecisionMetrics
    role = request.args.get('role')
    try:
        registry = _decision_registry()
        store = DecisionRunStore(registry)
        runs = store.list_runs(role=role, limit=int(request.args.get('limit', 100)))
        overview = ProjectDecisionMetrics(registry).overview(role=role)
        return jsonify({'ok': True, 'runs': runs, 'overview': overview,
                        'server_time': datetime.now().timestamp()})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/llm/decisions/<decision_id>')
def api_llm_decision(decision_id):
    from scripts.live_trading.decision_ledger.decision_run_store import DecisionRunStore
    try:
        registry = _decision_registry()
        store = DecisionRunStore(registry)
        run = store.get_run(decision_id)
        if not run:
            return jsonify({'ok': False, 'error': 'not_found'}), 404
        validated = store.get_snapshot_latest('validated_decision', decision_id)
        return jsonify({'ok': True, 'run': run, 'validated': validated})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/llm/decisions/<decision_id>/replay')
def api_llm_replay(decision_id):
    from scripts.live_trading.replay_decision import ReplayEngine
    mode = request.args.get('mode', 'validate')
    try:
        registry = _decision_registry()
        engine = ReplayEngine(registry=registry)
        if mode == 'project':
            out = engine.project(decision_id)
        elif mode == 'compare':
            out = engine.compare(decision_id, request.args.get('attempt_a'),
                                 request.args.get('attempt_b'))
        else:
            out = engine.validate(decision_id)
        return jsonify({'ok': True, 'replay': out})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/llm/metrics/<role>')
def api_llm_metrics(role):
    from scripts.live_trading.project_decision_metrics import ProjectDecisionMetrics
    if role not in ('selection', 'entry', 'position'):
        return jsonify({'ok': False, 'error': 'role 需为 selection/entry/position'}), 400
    try:
        registry = _decision_registry()
        m = ProjectDecisionMetrics(registry)
        fn = {'selection': m.selection_metrics, 'entry': m.entry_metrics,
              'position': m.position_metrics}[role]
        return jsonify({'ok': True, 'metrics': fn()})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/llm/permissions')
def api_llm_permissions():
    from scripts.live_trading.llm_permission import PERMISSIONS, level_for
    cfg = {}
    try:
        with open(os.path.join(BASE_DIR, 'config.yaml'), 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        pass
    perms = {p: level_for(p, cfg) for p in PERMISSIONS}
    return jsonify({'ok': True, 'permissions': perms,
                    '_default': (cfg.get('llm_permissions') or {}).get('_default', 'shadow')})


@app.route('/api/llm/health')
def api_llm_health():
    from scripts.live_trading.project_decision_metrics import ProjectDecisionMetrics
    try:
        registry = _decision_registry()
        return jsonify({'ok': True, 'health': ProjectDecisionMetrics(registry).health()})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


if __name__ == '__main__':
    print("🚀 quant_us Web 服务启动...")
    web_port = int(os.environ.get('US_WEB_PORT', '8890'))
    print(f"   访问 http://127.0.0.1:{web_port}")
    app.run(host='0.0.0.0', port=web_port, debug=False)
