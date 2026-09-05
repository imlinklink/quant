"""
日内抄底评分 canonical 实现（ATR 自适应）
所有评分函数均为纯函数，Web / 实盘监控共用同一套逻辑

核心设计：
- ATR 自适应：RSI 阈值和回撤阈值用 N×ATR 归一化
  高波动股（SOXL）和低波动股（AAPL）用同一套 ×ATR 语义
- 评分维度：超卖(14根高点回撤÷ATR) / 布林带 / 成交量 / 量价背离 /
            多周期回撤(ATR自适应) / 趋势过滤(ADX)
"""
from typing import Tuple, Dict, Any, Optional
import numpy as np
import pandas as pd

# ─── 工具函数 ──────────────────────────────────────────────────────────────

def atr_pct_from_bars(df: pd.DataFrame, period: int = 14) -> float:
    """
    计算 ATR(period) / 当前价
    Wilder's 平滑，与 Web / 实盘一致
    """
    if df is None or len(df) < period + 1:
        return 0.01
    high = df['high'].values
    low = df['low'].values
    close = df['close'].values
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - close[:-1]),
                   np.abs(low[1:] - close[:-1])))
    if len(tr) < period:
        return 0.01
    atr = float(np.mean(tr[:period]))
    for i in range(period, len(tr)):
        atr = atr * (period - 1) / period + tr[i] / period
    cur = float(close[-1])
    return float(atr / cur) if cur > 0 else 0.01


def rsi_from_prices(prices: pd.Series, period: int = 14) -> float:
    """RSI(14)，用于展示"""
    if len(prices) < period + 1:
        return 50.0
    deltas = prices.diff()
    gains = deltas.clip(lower=0)
    losses = -deltas.clip(upper=0)
    avg_gain = gains.rolling(period).mean().iloc[-1]
    avg_loss = losses.rolling(period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    return float(100 - 100 / (1 + avg_gain / avg_loss))


def _rsi_series(prices: pd.Series, period: int = 14) -> pd.Series:
    """RSI(period) 序列（与 rsi_from_prices 同算法）。"""
    if len(prices) < period + 1:
        return pd.Series(np.nan, index=prices.index)
    deltas = prices.diff()
    gains = deltas.clip(lower=0)
    losses = -deltas.clip(upper=0)
    avg_gain = gains.rolling(period).mean()
    avg_loss = losses.rolling(period).mean()
    rsi = 100 - 100 / (1 + avg_gain / avg_loss)
    return rsi.where(np.isfinite(rsi), np.nan)


# ─── 各维度评分函数 ──────────────────────────────────────────────────────

def score_oversold_atr(df: pd.DataFrame, atr_pct: float) -> Tuple[int, float, float]:
    """
    超卖评分：当前价距 14 根高点回撤 ÷ ATR（跌了几个 ATR）
    阈值固定为 3.0 / 2.0 / 1.0 倍 ATR

    Returns:
        (score: int, dd_atr: float, rsi: float)
        - score: 0-3
        - dd_atr: 回撤 ÷ ATR，跌了几个 ATR
        - rsi: RSI(14) 仅展示用
    """
    closes = df['close']
    rsi = rsi_from_prices(closes)
    high_14 = float(closes.tail(14).max())
    cur = float(closes.iloc[-1])
    atr_abs = atr_pct * cur
    dd_atr = float((high_14 - cur) / atr_abs) if atr_abs > 0 else 0.0
    if dd_atr >= 3.0:
        score = 3
    elif dd_atr >= 2.0:
        score = 2
    elif dd_atr >= 1.0:
        score = 1
    else:
        score = 0
    return score, dd_atr, rsi


def score_bollinger(prices: pd.Series) -> Tuple[int, float]:
    """
    布林带评分
    阈值：<10% → 3分，<20% → 2分，<35% → 1分
    position = (当前价 - 下轨) / (上轨 - 下轨) × 100
    """
    period, std_mult = 20, 2.0
    if len(prices) < period:
        return 0, 50.0
    mid = float(prices.rolling(period).mean().iloc[-1])
    std = float(prices.rolling(period).std().iloc[-1])
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    cur = float(prices.iloc[-1])
    if upper == lower:
        position = 50.0
    else:
        position = float((cur - lower) / (upper - lower) * 100)
    if position < 10:
        score = 3
    elif position < 20:
        score = 2
    elif position < 35:
        score = 1
    else:
        score = 0
    return min(score, 2), position  # 上限 2


def score_volume(volumes: pd.Series) -> int:
    """
    成交量评分（0-3 分）
    - 反弹放量（当前量 > 前5均量 × 1.5）→ +2
    - 整体量能健康（当前量 > 前5均量）→ +1
    - 前期量能翻倍 → 额外 +1
    """
    if len(volumes) < 5:
        return 0
    cur = float(volumes.iloc[-1])
    avg5 = float(volumes.tail(5).mean())
    avg5_prev = float(volumes.tail(10).head(5).mean()) if len(volumes) >= 10 else avg5
    score = 0
    if avg5 > 0:
        if cur > avg5 * 1.5:
            score += 2
        elif cur > avg5:
            score += 1
        if avg5_prev > 0 and cur > avg5_prev * 2:
            score += 1
    return min(score, 2)  # 上限 2


def score_true_divergence(closes: pd.Series, volumes: pd.Series,
                          window: int = 12, rsi_period: int = 14) -> Tuple[int, Dict[str, Any]]:
    """
    真实底背离（0-3 分）：
    - 价格创出“新低”（最近 window 根的最低点 < 前一个 window 的最低点）；
    - 但 RSI 谷底没有同步创新低（RSI 更高）→ +2（真底背离）；
    - 创新低时缩量（< 前段均量 70%）→ +1（抛压衰竭）。
    替代原先只看“最近3根缩量下跌”的伪背离。
    """
    detail: Dict[str, Any] = {'type': 'none', 'score': 0}
    if len(closes) < 2 * window + rsi_period + 3 or len(volumes) < 2 * window:
        detail['detail'] = '数据不足'
        return 0, detail
    vals = closes.values
    vols = volumes.values.astype(float)
    rsi = _rsi_series(closes, rsi_period).values
    first = vals[-2 * window:-window]
    second = vals[-window:]
    fi = int(np.argmin(first))
    si = int(np.argmin(second))
    detail.update({
        'first_low': round(float(first[fi]), 3),
        'last_low': round(float(second[si]), 3),
    })
    score = 0
    if second[si] < first[fi] * 0.9999:
        detail['type'] = 'new_low'
        r1 = rsi[-2 * window + fi]
        r2 = rsi[-window + si]
        if np.isfinite(r1) and np.isfinite(r2) and float(r2) > float(r1):
            score += 2
            detail['bullish_divergence'] = True
            detail['rsi_first_low'] = round(float(r1), 1)
            detail['rsi_last_low'] = round(float(r2), 1)
        # 创新低是否缩量（抛压衰竭）
        avg_vol = float(np.mean(vols[-2 * window:-window]))
        if avg_vol > 0 and vols[-window + si] < avg_vol * 0.7:
            score += 1
            detail['low_volume_shrink'] = True
    detail['score'] = min(score, 3)
    return min(score, 3), detail


def reversal_confirmation(df: pd.DataFrame, atr_pct: float) -> Tuple[bool, Dict[str, Any]]:
    """
    P1-1 反转确认（硬条件，不再“越跌越买”）：
    满足任一即算出现反转迹象：
      - 阳线收高：最后一根 close > open 且 close > 前一根 close
      - 长下影探底：下影线 ≥ 60% 振幅，且 low 跌破前几根低点后收回
      - 自低点回升：收盘较近 3 根最低点回升 ≥ 0.5×ATR
    """
    detail: Dict[str, Any] = {'ok': False, 'conditions': [], 'detail': '无反转确认'}
    if df is None or len(df) < 5:
        return False, detail
    closes = df['close'].values
    opens = df['open'].values
    highs = df['high'].values
    lows = df['low'].values
    cur = float(closes[-1])
    prev = float(closes[-2])
    o, h, l = float(opens[-1]), float(highs[-1]), float(lows[-1])
    conds = []
    if cur > o and cur > prev:
        conds.append('阳线收高')
    rng = h - l
    lower_shadow = min(o, cur) - l
    if rng > 0 and lower_shadow >= 0.6 * rng and l <= float(np.min(lows[-4:-1])):
        conds.append('长下影探底')
    atr_abs = atr_pct * cur
    # “自低点回升”必须是收高/收平后再算，否则大跌大振幅也会误报
    if cur >= prev and atr_abs > 0 and (cur - float(np.min(lows[-3:]))) >= 0.5 * atr_abs:
        conds.append('自低点回升')
    if conds:
        detail.update({'ok': True, 'conditions': conds, 'detail': '、'.join(conds)})
    return bool(conds), detail


def score_higher_tf_env(df: Optional[pd.DataFrame]) -> Tuple[int, Dict[str, Any]]:
    """
    P1-2 60分钟环境评分（建议只对“评分已达标”的信号计算并缓存）：
      - 60m 强下行（收在 MA20 下方 + MA20 走低 + RSI<45）→ -2（禁止/强提示）
      - 60m RSI≥70（超买区）→ -1
      - 60m 深度超卖（RSI≤35 且收在中轨下方，通常配合日线多头回踩）→ +1
      - 其余 → 0
    """
    if df is None or len(df) < 30:
        return 0, {'score': 0, 'detail': '60m数据不足'}
    closes = df['close']
    cur = float(closes.iloc[-1])
    rsi = rsi_from_prices(closes)
    ma20 = float(closes.rolling(20).mean().iloc[-1])
    ma20_prev = float(closes.rolling(20).mean().shift(5).iloc[-1]) if len(closes) >= 25 else ma20
    std = float(closes.rolling(20).std().iloc[-1])
    mid = float(closes.rolling(20).mean().iloc[-1])
    lower = mid - 2.0 * std
    upper = mid + 2.0 * std
    pos = 50.0 if upper == lower else float((cur - lower) / (upper - lower) * 100)
    base = {'rsi': round(rsi, 1), 'ma20': round(ma20, 3), 'bb_pos': round(pos, 1)}
    if cur < ma20 and ma20 <= ma20_prev and rsi < 45:
        base.update({'trend': 'strong_down',
                     'detail': f'60m强下行: RSI {rsi:.0f}<45 且收于MA20下方'})
        return -2, base
    if rsi >= 70:
        base.update({'trend': 'overbought', 'detail': f'60m超买 RSI {rsi:.0f}≥70'})
        return -1, base
    if rsi <= 35 and cur < mid:
        base.update({'trend': 'deep_oversold',
                     'detail': f'60m深度超卖 RSI {rsi:.0f}≤35'})
        return 1, base
    base.update({'trend': 'neutral', 'detail': '60m环境中性'})
    return 0, base


def score_drawdown_atr(prices: pd.Series, atr_pct: float) -> Tuple[int, Dict[str, Any]]:
    """
    短周期回撤评分（ATR 自适应）【仅保留短线超跌信号】
    回撤 = (高点 - 当前) / 高点

    阈值（×ATR，归一化）：
      20根（≈2小时）：≥3.5× → 3分 / ≥2.5× → 2分 / ≥1.5× → 1分

    Returns:
        (total_score: int, details: dict)
    """
    if len(prices) < 20:
        return 0, {'short': 0, 'short_score': 0}

    cur     = float(prices.iloc[-1])
    s_high  = float(prices.tail(20).max())
    s_dd    = float((s_high - cur) / s_high) if s_high > 0 else 0.0
    s_score = 0
    if atr_pct > 0:
        s_dd_atr = s_dd / atr_pct
        if   s_dd_atr >= 3.5: s_score = 3
        elif s_dd_atr >= 2.5: s_score = 2
        elif s_dd_atr >= 1.5: s_score = 1

    return s_score, {
        'short':       round(s_dd * 100, 2),
        'short_score': s_score,
    }


def score_trend_filter(bars: pd.DataFrame) -> Tuple[int, Dict[str, Any]]:
    """
    趋势过滤（ADX）
    - ADX > 25 + -DI > +DI（强下跌趋势）→ -2 分（防阴跌）
    - ADX > 25 + +DI > -DI（强上涨趋势）→ -1 分（不算超跌）
    - ADX < 20 → 0 分（弱趋势/震荡）
    """
    if len(bars) < 30:
        return 0, {'adx': 0, 'plus_di': 0, 'minus_di': 0, 'trend': 'unknown'}
    closes = bars['close'].values
    high   = bars['high'].values
    low    = bars['low'].values

    # Wilder 平滑参数
    period = 28
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - closes[:-1]),
                               np.abs(low[1:] - closes[:-1])))
    if len(tr) < period:
        return 0, {'adx': 0, 'plus_di': 0, 'minus_di': 0, 'trend': 'unknown'}

    plus_dm  = np.where(high[1:]  - high[:-1]  > low[:-1] - low[1:],
                        np.maximum(high[1:]  - high[:-1],  0), 0)
    minus_dm = np.where(low[:-1]  - low[1:]    > high[1:] - high[:-1],
                        np.maximum(low[:-1]  - low[1:],    0), 0)

    atr_arr  = np.zeros(len(tr), dtype=float)
    pdm_arr  = np.zeros(len(tr), dtype=float)
    mdm_arr  = np.zeros(len(tr), dtype=float)
    atr_arr[period-1]  = np.mean(tr[:period])
    pdm_arr[period-1]  = np.mean(plus_dm[:period])
    mdm_arr[period-1]  = np.mean(minus_dm[:period])
    for i in range(period, len(tr)):
        atr_arr[i]  = atr_arr[i-1]  * (period-1)/period  + tr[i]      / period
        pdm_arr[i]  = pdm_arr[i-1]  * (period-1)/period  + plus_dm[i]  / period
        mdm_arr[i]  = mdm_arr[i-1]  * (period-1)/period  + minus_dm[i] / period

    pdi = 100 * pdm_arr / (atr_arr + 1e-10)
    mdi = 100 * mdm_arr / (atr_arr + 1e-10)
    dx  = 100 * np.abs(pdi - mdi) / (pdi + mdi + 1e-10)

    adx_arr = np.zeros(len(dx), dtype=float)
    adx_arr[period-1] = np.mean(dx[:period])
    for i in range(period, len(dx)):
        adx_arr[i] = adx_arr[i-1] * (period-1)/period + dx[i] / period

    adx = float(adx_arr[-1])
    pdi_f = float(pdi[-1])
    mdi_f = float(mdi[-1])

    # 连续阴线检测（近5根有≥3根下跌 + 下跌期间都在ADX区间）
    closes_arr = bars['close'].values
    recent_closes = closes_arr[-5:]
    consecutive_down = 0
    for i in range(len(recent_closes)-1, 0, -1):
        if recent_closes[i] < recent_closes[i-1]:
            consecutive_down += 1
        else:
            break
    strong_persistent_down = (consecutive_down >= 3 and mdi_f > pdi_f)

    if (adx > 20 and mdi_f > pdi_f) or strong_persistent_down:
        trend = 'strong_down'
        adj = -3  # 阴跌不抄，加大扣分
    elif adx > 25 and pdi_f > mdi_f:
        trend = 'strong_up'
        adj = -1
    else:
        trend = 'weak'
        adj = 0

    return adj, {'adx': round(adx, 1), 'plus_di': round(pdi_f, 1),
                 'minus_di': round(mdi_f, 1), 'trend': trend}


# ─── 主分析函数 ──────────────────────────────────────────────────────────

def analyze_score(
    df: pd.DataFrame,
    current_price: float,
    buy_threshold: int = 6,
) -> Dict[str, Any]:
    """
    Canonical 日内抄底评分（纯函数，Web / 实盘共用）

    评分维度（各维度独立计分，满分 10）：
      rsi_score    0-3   超卖（14根高点回撤 ÷ ATR，阈值 3.0/2.0/1.0×ATR）
      bb_score     0-2   布林带（位置 <10/20/35%）
      vol_score    0-2   成交量（放量确认）
      vol_div      0-3   真实底背离（价格新低但 RSI 不创新低 / 缩量创新低）
      trend_adj   -3-0  趋势过滤（strong_down→-3，strong_up→-1，weak→0）
      ──────────────────────────────
      total       0-10

    P1 追加字段（供实盘闸门用，不改总分语义）：
      reversal.ok        反转确认（阳线收高 / 长下影 / 自低点回升≥0.5ATR）
      rr                 盈亏比：(目标价-入场价) / max(-5%止损, 2×ATR)
      divergence_detail  真实背离明细

    信号：
      adj_total >= buy_threshold         → buy
      weak + raw_total >= 7             → buy（弱趋势里深度超跌绕过阈值）
      strong_down/strong_up + adj >= 4   → watch
      else                               → none
    """
    if df is None or len(df) == 0:
        return {'score': 0, 'signal': 'none', 'bars_count': 0}

    closes  = df['close']
    volumes = df['volume']

    atr_pct = atr_pct_from_bars(df)
    rsi_score, dd_atr, rsi = score_oversold_atr(df, atr_pct)
    bb_score, bb_pos      = score_bollinger(closes)
    vol_score              = score_volume(volumes)
    vol_div, div_detail    = score_true_divergence(closes, volumes)
    rev_ok, rev_detail     = reversal_confirmation(df, atr_pct)
    dd_score, dd_details   = score_drawdown_atr(closes, atr_pct)
    trend_adj, trend       = score_trend_filter(df)

    # P1-3 盈亏比参考：目标价取布林中轨（若高于入场价），
    # 风险取 max(固定-5%止损距离, 2×ATR)，只做展示/闸门，不改总分
    entry = float(current_price) if current_price and current_price > 0 \
        else float(closes.iloc[-1])
    atr_abs = atr_pct * entry
    stop_ref = entry * (1 - 0.05)
    risk = max(entry - stop_ref, 2.0 * atr_abs, entry * 0.001)
    bb_mid = float(closes.rolling(20).mean().iloc[-1]) if len(closes) >= 20 \
        else float(closes.mean())
    target = bb_mid if bb_mid > entry else None
    rr = (target - entry) / risk if target else 0.0

    raw_total = rsi_score + bb_score + vol_score + vol_div  # 不含趋势调整
    adj_total = raw_total + trend_adj
    total = max(adj_total, 0)

    # 人类可读明细（确认页/日志展示）
    parts = [
        f"超卖{rsi_score}({dd_atr:.1f}×ATR)",
        f"布林{bb_score}({bb_pos:.0f}%)",
        f"量{vol_score}",
        f"背离{vol_div}",
    ]
    if rev_ok:
        parts.append(f"反转确认[{rev_detail.get('detail', '')}]")
    else:
        parts.append("无反转确认")
    if trend_adj:
        parts.append(f"趋势调整{trend_adj}")
    details = " ".join(parts)

    trend_name = trend.get('trend', 'weak')
    # BUY：调整后总分够，或弱趋势里深度超跌（raw>=7 绕过阈值）
    if adj_total >= buy_threshold or (trend_name == 'weak' and raw_total >= 7):
        signal = 'buy'
    elif trend_name in ('strong_down', 'strong_up') and adj_total >= 4:
        signal = 'watch'
    else:
        signal = 'none'

    return {
        'score':        total,
        'raw_score':    raw_total,           # 不含趋势调整的原始分
        'signal':       signal,
        'details':      details,
        'rsi':          round(rsi, 1),
        'rsi_score':    rsi_score,           # 展示用
        'bb_position':  round(bb_pos, 1),     # 展示用
        'bb_score':     bb_score,            # 展示用
        'volume_score': vol_score,
        'volume_divergence_score': vol_div,
        'divergence_detail': div_detail,
        'reversal':     rev_detail,
        'drawdown_score': dd_score,          # 展示用（不参与总分）
        'drawdown':       dd_details,
        'atr_pct':        round(atr_pct * 100, 3),
        'dd_atr':         round(dd_atr, 2),
        'rr':             round(rr, 2),
        'target_price':   round(target, 3) if target else None,
        'stop_ref':       round(stop_ref, 3),
        'trend':          trend,
        'trend_adj':      trend_adj,
        'bars_count':     len(df),
    }
