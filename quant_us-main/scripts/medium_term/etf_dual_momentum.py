"""B1 ETF 双动量目标生成。"""
from __future__ import annotations

import pandas as pd

from .momentum_features import momentum_snapshot, point_in_time_momentum_snapshot
from .monthly_calendar import month_end_sessions, next_session, normalize_sessions


DEFAULT_RISK_ASSETS = ('SPY', 'QQQ', 'IWM', 'XLK', 'SMH', 'SOXX', 'IGV')
DEFAULT_DEFENSIVE = 'SHY'


def generate_targets(prices: pd.DataFrame, *, price_col='asof_close',
                     risk_assets=DEFAULT_RISK_ASSETS, defensive=DEFAULT_DEFENSIVE,
                     lookback_6m=126, actions: pd.DataFrame | None = None) -> pd.DataFrame:
    """按月选择6个月动量最强且为正的 ETF，否则转入 defensive。

    返回每月目标和是否需要换仓。首个有效目标视为换仓；目标不变时
    `rebalance_required=False`。数据不足的月份保留并写拒绝原因。
    """
    required = {'security_id', 'session', price_col}
    if missing := required - set(prices.columns):
        raise ValueError(f'ETF_COLUMNS_MISSING:{",".join(sorted(missing))}')
    allowed = set(risk_assets) | {defensive}
    data = prices[prices.security_id.astype(str).isin(allowed)].copy()
    calendar = normalize_sessions(data.session)
    rows, previous = [], None
    for decision in month_end_sessions(calendar):
        # 复用严格的 as-of/边界实现；B1 只消费6m字段。
        lookbacks = dict(lookback_6m=lookback_6m, skip_1m=1,
                         lookback_12m=lookback_6m)
        snap = (point_in_time_momentum_snapshot(data, actions, decision, **lookbacks)
                if actions is not None else
                momentum_snapshot(data, decision, price_col=price_col, **lookbacks))
        risk = snap[snap.security_id.isin(risk_assets)].copy()
        valid = risk[risk.eligible & risk.mom_6m.notna()]
        execution = next_session(calendar, decision)
        reason, target, score = '', None, None
        if set(risk_assets) - set(valid.security_id):
            reason = 'RISK_ASSET_LOOKBACK_INCOMPLETE'
        elif execution is None:
            reason = 'NEXT_SESSION_MISSING'
        else:
            best = valid.sort_values(['mom_6m', 'security_id'], ascending=[False, True]).iloc[0]
            target = str(best.security_id) if float(best.mom_6m) > 0 else defensive
            score = float(best.mom_6m)
        changed = target is not None and target != previous
        rows.append({'decision_session': decision, 'execution_session': execution,
                     'target_security_id': target, 'winning_mom_6m': score,
                     'rebalance_required': changed, 'reject_reason': reason})
        if target is not None:
            previous = target
    return pd.DataFrame(rows)
