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


def score_volume_divergence(closes: pd.Series, volumes: pd.Series) -> int:
    """
    量价背离评分（0-3 分）
    - 连续 2 根下跌 + 缩量（< 前5均量 70%）→ +2（下跌衰竭）
    - 反弹 + 放量（> 前5均量 150%）→ +1（反弹确认）
    """
    if len(closes) < 10 or len(volumes) < 10:
        return 0
    c = closes.tail(10).values
    v = volumes.tail(10).values
    score = 0
    if c[-1] < c[-2] < c[-3]:
        avg5 = float(v[-6:-1].mean())
        if avg5 > 0 and v[-1] < avg5 * 0.7:
            score += 2
    if c[-1] > c[-2]:
        avg5 = float(v[-6:-1].mean())
        if avg5 > 0 and v[-1] > avg5 * 1.5:
            score += 1
    return min(score, 3)  # 上限 3


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
      vol_div      0-3   量价背离（缩量止跌 / 放量反弹）
      trend_adj   -3-0  趋势过滤（strong_down→-3，strong_up→-1，weak→0）
      ──────────────────────────────
      total       0-10

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
    vol_div                = score_volume_divergence(closes, volumes)
    dd_score, dd_details   = score_drawdown_atr(closes, atr_pct)
    trend_adj, trend       = score_trend_filter(df)

    raw_total = rsi_score + bb_score + vol_score + vol_div  # 不含趋势调整
    adj_total = raw_total + trend_adj
    total = max(adj_total, 0)

    trend_name = trend.get('trend', 'weak')
    # BUY：调整后总分够，或弱趋势里深度超跌（raw>=7 绕过阈值）
    if adj_total >= buy_threshold or (trend_name == 'weak' and raw_total >= 7):
        signal = 'buy'
    elif trend_name in ('strong_down', 'strong_up') and adj_total >= 4:
        signal = 'watch'
    else:
        signal = 'none'

    return {
        'score':       total,
        'raw_score':   raw_total,           # 不含趋势调整的原始分
        'signal':      signal,
        'rsi':         round(rsi, 1),
        'rsi_score':   rsi_score,           # 展示用
        'bb_position': round(bb_pos, 1),     # 展示用
        'bb_score':    bb_score,            # 展示用
        'volume_score': vol_score,
        'volume_divergence_score': vol_div,
        'drawdown_score': dd_score,          # 展示用（不参与总分）
        'drawdown':       dd_details,
        'atr_pct':        round(atr_pct * 100, 3),
        'dd_atr':         round(dd_atr, 2),
        'trend':          trend,
        'trend_adj':      trend_adj,
        'bars_count':     len(df),
    }
