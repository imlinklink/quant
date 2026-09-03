#!/usr/bin/env python3
"""
实盘买入时机策略回测验证工具
直接调用 IntradayAnalyzer / TrendDetector，与实盘 BuyTimingStrategy 完全一致

核心逻辑（与 buy_timing.py 对齐）：
  1. 前30根1分钟K线 → TrendDetector.detect() 判定 uptrend/sideways/downtrend
  2. 判定后缓存，后续所有时间点固定使用对应打分系统：
     - uptrend   → analyze_momentum()  追涨
     - sideways   → analyze_hybrid()    抄底
     - downtrend  → analyze_hybrid()    抄底
  3. 不再同时计算两套分数做对比（那是旧逻辑）

用法：
  python scripts/verify_intraday_analyzer.py
  STOCK_CODE=HK.02513 TRADE_DATE=2026-04-24 python scripts/verify_intraday_analyzer.py
"""
import os
import sys
from typing import Optional
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from futu import *

from scripts.live_trading.intraday_analyzer import IntradayAnalyzer, TrendDetector


def load_config():
    """从 config.yaml 读取配置"""
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.yaml')
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    return config


# ==================== 配置 =====================
CONFIG = load_config()
CFG = CONFIG.get('trading', {}).get('live_trading', {}).get('buy_timing', {}).get('analysis', {})
BUY_TIMING_CFG = CONFIG.get('trading', {}).get('live_trading', {}).get('buy_timing', {})


def fetch_bars(stock_code: str, date: str):
    """获取1分钟、5分钟K线 + 【正确】昨日收盘价（历史K线获取）"""
    print(f"[INFO] 连接富途 API，拉取 {stock_code} {date} 的K线...")
    ctx = OpenQuoteContext('127.0.0.1', 11111)

    # --------------------------------------------------------------------
    # 🔥 修复 1：获取【真正的昨日收盘价】（用前一天K线，不是快照！）
    # --------------------------------------------------------------------
    prev_close = None
    try:
        # 把传入日期减一天
        trade_dt = pd.to_datetime(date)
        prev_date = (trade_dt - pd.Timedelta(days=1)).strftime("%Y-%m-%d")

        # 拉取前一天的日K
        ret_prev, data_prev, _ = ctx.request_history_kline(
            stock_code, prev_date, prev_date, KLType.K_DAY, AuType.QFQ)
        if ret_prev == RET_OK and data_prev is not None and len(data_prev) > 0:
            prev_close = float(data_prev['close'].iloc[0])
    except:
        prev_close = None

    # 获取当天5分钟/1分钟K线
    try:
        ret5, data5, _ = ctx.request_history_kline(
            stock_code, date, date, KLType.K_5M, AuType.QFQ, max_count=100)
        ret1, data1, _ = ctx.request_history_kline(
            stock_code, date, date, KLType.K_1M, AuType.QFQ, max_count=480)
    finally:
        ctx.close()

    def parse(data):
        if data is None or len(data) == 0:
            return None
        df = pd.DataFrame(data)
        for c in ['open', 'close', 'high', 'low', 'volume', 'turnover']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df['time_key'] = pd.to_datetime(df['time_key'])
        return df.sort_values('time_key').reset_index(drop=True)

    df5 = parse(data5)
    df1 = parse(data1)
    n5 = len(df5) if df5 is not None else 0
    n1 = len(df1) if df1 is not None else 0
    print(f"[INFO] 获取 5分钟×{n5}根  1分钟×{n1}根  昨收={prev_close}")
    return df5, df1, prev_close

def bar_summary(df: pd.DataFrame) -> str:
    if df is None or len(df) == 0:
        return "无数据"
    first = df.iloc[0]
    last = df.iloc[-1]
    chg = (last['close'] - first['close']) / first['close'] * 100
    lo = df['low'].min()
    hi = df['high'].max()
    return (f"开{first['close']:.2f}→收{last['close']:.2f} "
            f"({chg:+.2f}%)  区间{lo:.2f}~{hi:.2f}")


def calc_future_returns(df5: pd.DataFrame, idx: int, look_ahead: int = 4):
    """计算信号后 N 个 5m bar 的收益"""
    if idx + 1 >= len(df5):
        return None, None, None
    signal_price = df5.iloc[idx]['close']
    end_idx = min(idx + look_ahead, len(df5) - 1)
    future_high = df5.iloc[idx + 1:end_idx + 1]['high'].max()
    future_low = df5.iloc[idx + 1:end_idx + 1]['low'].min()
    future_close = df5.iloc[end_idx]['close']
    max_ret = (future_high - signal_price) / signal_price * 100
    min_ret = (future_low - signal_price) / signal_price * 100
    close_ret = (future_close - signal_price) / signal_price * 100
    return max_ret, min_ret, close_ret


def print_buy_signals_summary(signals: list):
    """打印买入机会汇总"""
    watch_threshold = CFG.get('watch_threshold', 4)
    if not signals:
        print(f"\n{'='*90}")
        print(f" 买入机会汇总（无 score >= {watch_threshold} 的信号）")
        print(f"{'='*90}")
        return

    print(f"\n{'='*90}")
    print(f" 买入机会汇总（共 {len(signals)} 个，按时间顺序）")
    print(f"{'='*90}")
    print(f"  {'时间':<10} {'收盘价':>8} {'模式':<8} {'评分':>5} {'信号':<5} {'原因':<18} "
          f"{'之后20min':>10} {'之后20min':>10} {'20min后':>8}")
    print(f"  {'-'*85}")
    print(f"  {'':10} {'':8} {'':8} {'':>5} {'':5} {'':18} "
          f"{'最高涨':>10} {'最低跌':>10} {'收盘':>8}")
    print(f"  {'-'*85}")

    for s in signals:
        ts = s['time_str']
        price = s['price']
        mode_label = s['mode_label']   # 如 "uptrend-追涨" / "sideways-抄底"
        score = s['score']
        sig_type = s['signal']
        reason = s['reason']
        max_r, min_r, close_r = s.get('max_ret'), s.get('min_ret'), s.get('close_ret')
        if max_r is not None:
            max_s, min_s, close_s = f"{max_r:+.2f}%", f"{min_r:+.2f}%", f"{close_r:+.2f}%"
        else:
            max_s = min_s = close_s = "N/A"
        print(f"  {ts:<10} {price:>8.2f} {mode_label:<8} {score:>3}/10 {sig_type:<5} {reason:<18} "
              f"{max_s:>10} {min_s:>10} {close_s:>8}")
    print(f"{'='*90}")


def print_trend_summary(trend_log: list):
    """打印趋势检测汇总"""
    if not trend_log:
        return

    print(f"\n{'='*90}")
    print(f"📈 趋势检测汇总（共 {len(trend_log)} 个检测点）")
    print(f"{'='*90}")
    print(f"  {'时间':<10} {'趋势':>8} {'涨幅%':>7} {'阳线%':>6} {'价位%':>6} {'MA斜率':>10} {'↑':>2} {'↓':>2}")
    print(f"  {'-'*65}")

    for t in trend_log:
        print(f"  {t['time']:<10} {t['trend']:>8} {t['return_pct']:>+7.2f} {t['bull_ratio']:>6.0f} "
              f"{t['price_position']:>6.0f} {t['ma_slope']:>10.6f} {t['score_up']:>2} {t['score_down']:>2}")

    # 统计
    uptrend_count = sum(1 for t in trend_log if t['trend'] == 'uptrend')
    sideways_count = sum(1 for t in trend_log if t['trend'] == 'sideways')
    downtrend_count = sum(1 for t in trend_log if t['trend'] == 'downtrend')
    print(f"\n  统计: 上涨{uptrend_count}次 / 震荡{sideways_count}次 / 下跌{downtrend_count}次")
    print(f"{'='*90}")


def print_bar_by_bar(df5: pd.DataFrame, df1: pd.DataFrame, stock_code: str,
                     prev_close: Optional[float] = None):
    """
    逐5分钟bar输出 —— 与实盘 buy_timing.py 完全一致的逻辑：

    1. 当1分钟K线足够(>=observe_bars)时，调用 TrendDetector.detect() 判定趋势
    2. 判定结果缓存（模拟 _regime_cache），后续时间点复用，不再重新判断
    3. 根据 regime 只跑对应的一套打分系统：
       - uptrend   → analyze_momentum()   追涨
       - sideways   → analyze_hybrid()     抄底
       - downtrend  → analyze_hybrid()     抄底
    """
    analyzer = IntradayAnalyzer(BUY_TIMING_CFG)
    trend_detector = TrendDetector(BUY_TIMING_CFG)

    strong_buy_threshold = BUY_TIMING_CFG.get('analysis', {}).get('momentum', {}).get('strong_buy_threshold', 6)
    watch_threshold = CFG.get('watch_threshold', 4)
    observe_bars = BUY_TIMING_CFG.get('analysis', {}).get('trend_detection', {}).get('observe_bars', 30)

    signals = []
    trend_log = []

    # ====== 模拟实盘的 regime 缓存 ======
    regime_cached = None      # 缓存的行情判定结果
    regime_determined_at = None  # 首次判定的时间点

    print(f"\n{'='*90}")
    print(f" 逐5分钟K线扫描（与实盘一致：前~{observe_bars}根1分K线判定趋势，之后固定模式）")
    print(f"{'='*90}")
    print(f"  {'时间':<6} {'行情':>8} {'收盘价':>7} │ {'模式':<14} {'评分':>4} │ "
          f"{'RSI':>6} {'BB':>5} {'量':>3} {'形':>3} │ {'备注'}")
    print(f"  {'-'*85}")

    for i5, row5 in df5.iterrows():
        t5 = str(row5['time_key'])[11:16]
        c5 = row5['close']

        # 准备数据切片
        bars_5m = df5.iloc[:i5 + 1].reset_index(drop=True)
        bars_1m = None
        if df1 is not None:
            target_i1 = min(i5 * 5 + 4, len(df1) - 1)
            bars_1m = df1.iloc[:target_i1 + 1].reset_index(drop=True)

        n_1m = len(bars_1m) if bars_1m is not None else 0

        # ─── 1. 趋势检测（与 buy_timing._determine_regime 一致）───
        note = ""
        # 只有未缓存 且 有足够1分钟K线 时才检测
        if regime_cached is None and bars_1m is not None and n_1m >= observe_bars:
            regime_cached, trend_detail = trend_detector.detect(bars_1m, prev_close=prev_close)
            regime_determined_at = t5
            mode_name = "追涨" if regime_cached == "uptrend" else "抄底"
            note = f"[首次判定={mode_name}]"

            trend_log.append({
                'time': t5,
                'trend': regime_cached,
                'return_pct': trend_detail.get('return_pct', 0),
                'bull_ratio': trend_detail.get('bull_ratio', 0),
                'price_position': trend_detail.get('price_position', 0),
                'ma_slope': trend_detail.get('ma_slope', 0),
                'score_up': trend_detail.get('score_up', 0),
                'score_down': trend_detail.get('score_down', 0),
            })
        elif regime_cached is not None:
            # 已有缓存，直接复用（不重新检测）
            pass

        # 当前 regime
        regime = regime_cached if regime_cached is not None else 'pending'

        # ─── 2. 根据 regime 只跑对应打分系统（与 buy_timing._get_kline_score 一致）───
        final = None
        mode_label = ""

        if len(bars_5m) >= analyzer.min_bars_required:
            if regime == 'uptrend':
                # 追涨模式
                if bars_1m is not None and n_1m >= analyzer.rsi_period + 1:
                    final = analyzer.analyze_momentum(stock_code, bars_1m, bars_5m, c5)
                    mode_label = f"{regime}-追涨"
                else:
                    final = {'score': 0, 'signal': 'no_buy', 'details': '1分K线不足(追涨)',
                             'rsi': None, 'bb_position': None, 'volume_score': 0, 'candle_score': 0}
                    mode_label = f"{regime}-追涨"
            elif regime in ('sideways', 'downtrend'):
                # 抄底模式
                if bars_1m is not None and n_1m >= 5:
                    final = analyzer.analyze_hybrid(stock_code, bars_1m, bars_5m, c5)
                    mode_label = f"{regime}-抄底"
                else:
                    final = {'score': 0, 'signal': 'no_buy', 'details': '1分K线不足(抄底)',
                             'rsi': None, 'bb_position': None, 'volume_score': 0, 'candle_score': 0}
                    mode_label = f"{regime}-抄底"
            else:
                # pending — 还没到趋势判定时间点，暂不评分（或用默认）
                final = {'score': 0, 'signal': 'no_buy', 'details': '趋势待判定',
                         'rsi': None, 'bb_position': None, 'volume_score': 0, 'candle_score': 0}
                mode_label = "待判定"
        else:
            final = {'score': 0, 'signal': 'no_buy', 'details': '5分K线不足',
                     'rsi': None, 'bb_position': None, 'volume_score': 0, 'candle_score': 0}
            mode_label = "数据不足"

        score = final['score']
        rsi_1m = final.get('rsi')
        bb_1m = final.get('bb_position')
        vol_s = final.get('volume_score', 0)
        cand_s = final.get('candle_score', 0)

        # 信号类型
        if score >= strong_buy_threshold:
            sig = "强买"
        elif score >= watch_threshold:
            sig = "观望"
        elif score >= 2:
            sig = "关注"
        else:
            sig = ""

        # ─── 打印 ───
        trend_icon = {"uptrend": "^", "sideways": "-", "downtrend": "v", "pending": "?"}.get(regime, "?")
        rsi1s = f"{rsi_1m:>5.1f}" if rsi_1m is not None else "  --"
        bb1s = f"{bb_1m:>4.0f}%" if bb_1m is not None else "  --"
        score_marker = " *" if score >= strong_buy_threshold else " <" if score >= watch_threshold else ""

        print(f"  {t5:<6} {trend_icon}{regime:>8} {c5:>7.2f} │ {mode_label:<14} {score:>3}/10{score_marker} │ "
              f"RSI{rsi1s} BB{bb1s:>5} {vol_s:>2}/3 {cand_s:>2}/3  {note}")

        # 收集信号
        if score >= watch_threshold:
            max_r, min_r, close_r = calc_future_returns(df5, i5)
            flag = _make_reason(regime, final, score, strong_buy_threshold, rsi_1m, bb_1m)
            signals.append({
                'time_str': t5,
                'price': c5,
                'mode_label': mode_label,
                'score': score,
                'signal': sig,
                'reason': flag,
                'max_ret': max_r,
                'min_ret': min_r,
                'close_ret': close_r,
            })

    # 结束汇总
    if regime_determined_at:
        mode_name = "追涨" if regime_cached == "uptrend" else "抄底"
        print(f"\n  >> 趋势于 {regime_determined_at} 首次判定为 [{regime_cached}]，全天使用[{mode_name}]模式")
    else:
        print(f"\n  >> 全日未达到趋势检测条件(需>= {observe_bars} 根1分K线)")

    return signals, trend_log


def _make_reason(regime: str, result: dict, score: int, strong_thresh: int,
                 rsi_1m, bb_1m) -> str:
    """生成信号原因标签"""
    if regime == 'uptrend':
        if result.get('rsi_score', 0) >= 2:
            return "RSI强势"
        elif result.get('bb_score', 0) >= 2:
            return "BB突破"
        elif score >= strong_thresh:
            return "追涨强买"
        return "追涨信号"
    else:
        if rsi_1m and rsi_1m < 28:
            return "RSI严重超卖"
        elif rsi_1m and rsi_1m < 32:
            return "RSI超卖"
        elif bb_1m and bb_1m < 20:
            return "BB紧贴下轨"
        elif score >= strong_thresh:
            return "强买信号"
        return "观望信号"


def main():
    stock_code = os.environ.get("STOCK_CODE", "HK.06082")
    trade_date = os.environ.get("TRADE_DATE", "2026-05-07")

    strong_buy = BUY_TIMING_CFG.get('analysis', {}).get('momentum', {}).get('strong_buy_threshold', 6)
    watch = CFG.get('watch_threshold', 4)
    td_cfg = BUY_TIMING_CFG.get('analysis', {}).get('trend_detection', {})
    bo_cfg = BUY_TIMING_CFG.get('analysis', {}).get('momentum', {})

    print(f"\n{'#'*90}")
    print(f"#  实盘买入时机策略回测验证（与 buy_timing.py 完全一致）")
    print(f"#  标的: {stock_code}  日期: {trade_date}")
    print(f"#")
    print(f"#  趋势检测: observe_bars={td_cfg.get('observe_bars', 30)} "
          f"uptrend>={td_cfg.get('uptrend_return_pct', 0.005)*100:.1f}%")
    print(f"#  追涨参数: RSI>{bo_cfg.get('rsi_strong',65)}/{bo_cfg.get('rsi_moderate',55)}/{bo_cfg.get('rsi_weak',50)}  "
          f"BB>{bo_cfg.get('bb_breakout_pct',80)}%  max_rally={bo_cfg.get('max_rally_pct',0.03)*100:.0f}%")
    print(f"#  抄底参数: rsi_severe={CFG.get('rsi_severe',28)} rsi_oversold={CFG.get('rsi_oversold',32)}  "
          f"bb_tight={CFG.get('bb_tight_pct',20)}%")
    print(f"#  阈值: 强买>={strong_buy} 观望>={watch}")
    print(f"{'#'*90}")

    df5, df1, prev_close = fetch_bars(stock_code, trade_date)
    if df5 is None:
        print("[ERROR] 无法获取5分钟K线")
        sys.exit(1)

    # 打印昨收基准（高开判断依据）
    if prev_close:
        first_5m_open = df5['open'].iloc[0] if df5 is not None else 0
        gap_pct = (first_5m_open - prev_close) / prev_close * 100 if prev_close > 0 else 0
        label = "高开" if gap_pct > 0.5 else "低开" if gap_pct < -0.5 else "平开"
        print(f"  昨收={prev_close:.2f}  今开={first_5m_open:.2f}  ({label}{gap_pct:+.2f}%)")

    print(f"\n[K线概览]")
    print(f"  5分钟: {bar_summary(df5)}")
    if df1 is not None:
        print(f"  1分钟: {bar_summary(df1)}")

    # 逐bar扫描（与实盘逻辑一致：regime缓存 + 单模式评分）
    signals, trend_log = print_bar_by_bar(df5, df1, stock_code, prev_close=prev_close)

    # 趋势检测汇总
    print_trend_summary(trend_log)

    # 买入信号汇总
    print_buy_signals_summary(signals)


if __name__ == '__main__':
    main()
