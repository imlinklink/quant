"""把逐股候选交易按时间重建为有持仓上限的组合成交集合。"""
from typing import Tuple

import pandas as pd


def apply_portfolio_constraints(trades: pd.DataFrame, max_positions: int = 3,
                                rank_column: str = 'portfolio_rank') -> Tuple[pd.DataFrame, pd.DataFrame]:
    """同一入场时点先按 rank、再按股票代码选择；返回 accepted/rejected。"""
    if trades is None or trades.empty:
        empty = trades.copy() if trades is not None else pd.DataFrame()
        return empty, empty
    d = trades.copy()
    d['entry_date'] = pd.to_datetime(d['entry_date'])
    d['exit_date'] = pd.to_datetime(d['exit_date'])
    if rank_column not in d:
        d[rank_column] = 999999
    d = d.sort_values(['entry_date', rank_column, 'stock']).reset_index(drop=True)
    active, accepted, rejected = [], [], []
    for _, row in d.iterrows():
        active = [end for end in active if end > row['entry_date']]
        if len(active) >= max_positions:
            item = row.to_dict(); item['portfolio_reject_reason'] = 'MAX_POSITIONS'
            rejected.append(item)
            continue
        accepted.append(row.to_dict())
        active.append(row['exit_date'])
    return pd.DataFrame(accepted), pd.DataFrame(rejected)
