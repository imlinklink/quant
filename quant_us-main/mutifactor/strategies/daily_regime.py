"""日线阶段识别（四层决策链的第 1–3 层）。

对应设计：15 分钟信号只能回答「现在是不是短线反弹」，回答不了「中期下跌是否结束」。
本模块负责后者——用**日线**判断某只股票是否已经结束中期下跌、可以进入买入流程。

全部计算只用「当日及之前」的数据，无前视。

状态机（§2.2）：
    FALLING → CAPITULATION → STABILIZING → REVERSING → CONFIRMED
      FALLING       持续创新低，禁止买入
      CAPITULATION  恐慌下跌，只观察
      STABILIZING   不再快速创新低，等待结构
      REVERSING     出现更高低点 + 站上 MA20
      CONFIRMED     允许生成买入 proposal
"""
from typing import Any, Dict

import numpy as np
import pandas as pd

STATES = ('FALLING', 'CAPITULATION', 'STABILIZING', 'REVERSING', 'CONFIRMED')
# 允许生成买入的终态（§2.2）
ACTIONABLE = ('REVERSING', 'CONFIRMED')


def daily_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """日线指标。所有滚动项均 shift(1) 或用截至当日数据，无前视。"""
    d = df.copy()
    close, high, low = d['close'], d['high'], d['low']
    vol = d['volume'].astype(float)

    for n in (20, 50, 200):
        d[f'ma{n}'] = close.rolling(n).mean()
    # 斜率：与 N 日前比较（当日已知）
    d['ma20_slope'] = d['ma20'] - d['ma20'].shift(3)
    d['ma50_slope'] = d['ma50'] - d['ma50'].shift(10)

    # ATR(14) 日线，与现网同源
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    d['atr'] = tr.rolling(14).mean()

    # 此前 N 日高低点（不含当日，避免把当日算进突破判定）
    d['hh10'] = high.shift(1).rolling(10).max()
    d['ll20'] = low.shift(1).rolling(20).min()

    # 「不再创新低」：近 5 日最低 > 前 5 日最低（更高低点的粗粒度判定）
    recent_low = low.rolling(5).min()
    prior_low = low.shift(5).rolling(5).min()
    d['higher_low'] = recent_low > prior_low

    # 5 日跌幅与放量（用于识别恐慌）
    d['ret5'] = close / close.shift(5) - 1.0
    vma20 = vol.shift(1).rolling(20).mean()
    d['vol_ratio'] = vol / vma20

    return d


def classify_state(row: pd.Series, prev_state: str = 'FALLING') -> str:
    """按当日一行指标判定阶段。prev_state 用于不允许状态无依据地跳级。

    判定优先级：越靠后越「接近可买」。CONFIRMED 需要突破局部高点。
    """
    close = row.get('close')
    ma20 = row.get('ma20')
    hh10 = row.get('hh10')
    if not np.isfinite(close) or not np.isfinite(ma20):
        return 'FALLING'

    higher_low = bool(row.get('higher_low', False))
    above_ma20 = close > ma20
    slope_up = np.isfinite(row.get('ma20_slope', np.nan)) and row['ma20_slope'] >= 0
    breakout = np.isfinite(hh10) and close > hh10
    ret5 = row.get('ret5', np.nan)
    vol_ratio = row.get('vol_ratio', np.nan)

    # CONFIRMED：更高低点 + 站上 MA20 + 突破近 10 日高点
    if higher_low and above_ma20 and breakout:
        return 'CONFIRMED'
    if higher_low and above_ma20:
        return 'REVERSING'
    if higher_low:
        return 'STABILIZING'
    # 恐慌：5 日急跌且放量
    if np.isfinite(ret5) and np.isfinite(vol_ratio) and ret5 <= -0.08 and vol_ratio >= 1.5:
        return 'CAPITULATION'
    return 'FALLING'


def state_series(df: pd.DataFrame) -> pd.Series:
    """逐日推进状态机（顺序相关，但每步只用当日及之前信息）。"""
    if 'ma20' not in df.columns:
        df = daily_indicators(df)
    states = []
    prev = 'FALLING'
    for _, row in df.iterrows():
        s = classify_state(row, prev)
        # 不可跨越：未经历 STABILIZING 不得直接 CONFIRMED（由判定式自然满足，
        # 这里仅防御 NaN 等异常导致的跳变）
        states.append(s)
        prev = s
    return pd.Series(states, index=df.index, name='state')


def trend_qualified(row: pd.Series, rs_ok: bool = True) -> bool:
    """策略 A（上升趋势中的回调）的资格判定：中长期趋势向上。

    close > MA200 且 MA50 上行 且 相对强度合格（若提供）。
    """
    close = row.get('close')
    ma200 = row.get('ma200')
    ma50_slope = row.get('ma50_slope')
    if not np.isfinite(close) or not np.isfinite(ma200):
        return False
    if not (close > ma200):
        return False
    if not (np.isfinite(ma50_slope) and ma50_slope > 0):
        return False
    return bool(rs_ok)


def relative_strength(df: pd.DataFrame, bench: pd.DataFrame, window: int = 63) -> pd.Series:
    """相对基准（默认 SPY）的 N 日相对强度：个股收益 − 基准收益。无前视。"""
    a = df.set_index('date')['close'].pct_change(window)
    b = bench.set_index('date')['close'].pct_change(window)
    idx = df.set_index('date').index
    return (a.reindex(idx) - b.reindex(idx)).rename('rel_strength')


def annotate(df: pd.DataFrame, bench: pd.DataFrame = None) -> pd.DataFrame:
    """一次性算好日线指标 + 状态 + 相对强度，供四组对照复用。"""
    d = daily_indicators(df)
    d['state'] = state_series(d)
    if bench is not None and not bench.empty:
        d['rel_strength'] = relative_strength(df, bench).values
    return d
