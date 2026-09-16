"""同口径 QQQ 被动基准：原始价 + 官方分红表 + 逐腿成本 + 共同日期。

会计口径与 `portfolio_engine.simulate_multi_asset_portfolio` 对齐：
开盘价买入、单边费用、除息日分红进现金（不滚入）、逐日收盘标记、期末不强制卖出。
分红表用独立官方来源（Nasdaq），不复用 Futu qfq 复权序列——后者在 2024-09-23 双计分红。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def build_qqq_benchmark(prices: pd.DataFrame, dividends: pd.DataFrame,
                        start, end, *, initial_cash=100_000., fee_rate=.001) -> pd.DataFrame:
    """返回 DataFrame[session, equity]，与账户净值同口径的 QQQ 被动持有基准。

    prices: QQQ 原始价，需含 session/open/close（session 为交易日，仅 QQQ）。
    dividends: 官方现金分红，需含 ex_date/amount（含小额/特殊分配）。
    """
    q = prices[['session', 'open', 'close']].copy()
    q['session'] = pd.to_datetime(q.session).dt.tz_localize(None).dt.normalize()
    q['open'] = pd.to_numeric(q.open, errors='coerce')
    q['close'] = pd.to_numeric(q.close, errors='coerce')
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    q = q[(q.session >= start) & (q.session <= end)].sort_values('session').reset_index(drop=True)
    if q.empty:
        raise ValueError('QQQ_WINDOW_EMPTY')
    if not (np.isfinite(q.open) & (q.open > 0) & np.isfinite(q.close) & (q.close > 0)).all():
        raise ValueError('QQQ_PRICE_INVALID')
    if not 0 <= fee_rate < 1 or initial_cash <= 0:
        raise ValueError('INVALID_BENCHMARK_CONFIG')

    div = dividends[['ex_date', 'amount']].copy()
    div['ex_date'] = pd.to_datetime(div.ex_date).dt.tz_localize(None).dt.normalize()
    div['amount'] = pd.to_numeric(div.amount, errors='coerce')
    div = div.dropna(subset=['amount'])
    if (div.amount < 0).any():
        raise ValueError('QQQ_DIVIDEND_INVALID')
    if div.ex_date.duplicated().any():
        raise ValueError('QQQ_DIVIDEND_DUPLICATE_EX_DATE')
    div_map = dict(zip(div.ex_date, div.amount))

    first_open = float(q.open.iloc[0])
    shares = initial_cash / (first_open * (1 + fee_rate))  # 全部现金按开盘价买入
    cash = initial_cash - shares * first_open * (1 + fee_rate)  # == 0（费用已扣）
    rows = []
    for i, s in enumerate(q.itertuples(index=False)):
        day = pd.Timestamp(s.session)
        if i > 0:  # 首日开盘买入，不享首日除息（与组合引擎一致）
            cash += shares * float(div_map.get(day, 0.0))
        rows.append({'session': day, 'equity': cash + shares * float(s.close),
                     'initial_equity': initial_cash})
    return pd.DataFrame(rows)
