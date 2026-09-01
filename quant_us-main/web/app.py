"""
quant_us Web 服务
包含：
- K线分析（支持盘前/盘中/盘后/夜盘）
- 实时行情监控
"""
import os
import sys
import logging
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

# ─── 配置加载 ──────────────────────────────────────────────────────
APP_CONFIG = {
    'buy_threshold': 10,  # 运行时可通过 /api/kline-analysis?threshold= 覆盖
}

def load_config():
    """从 config.yaml 加载默认阈值（启动时一次性读）"""
    config_path = os.path.join(BASE_DIR, 'config.yaml')
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
        dip = cfg.get('dip_buy', {})
        # 兼容老的 strong/normal 两档配置
        APP_CONFIG['buy_threshold'] = dip.get('buy_threshold', dip.get('strong_buy_threshold', 10))
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


if __name__ == '__main__':
    print("🚀 quant_us Web 服务启动...")
    print("   访问 http://127.0.0.1:8899")
    app.run(host='0.0.0.0', port=8899, debug=False)
