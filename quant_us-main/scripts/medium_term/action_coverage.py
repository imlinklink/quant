"""公司行动覆盖与衔接门：核对 QFQ/raw 因子跳变是否都被已记录行动解释。

原理：前复权/不复权之比（调整因子）只在除权日跳变，与行情涨跌无关。若某交易日
因子发生可见跳变（默认 >0.5%）却无对应已记录行动，即为**无法解释的行动缺口**，
该证券区间必须排除，不能把"没有记录"当作"没有行动"。反向的不匹配（有记录无跳变）
通常是低于阈值的小额股息，不作阻断，但一并报告。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.data.derive_corporate_actions import derive_actions

REQUIRED = {'security_id', 'ex_date', 'action_type'}


def _normalise(bars: pd.DataFrame, column: str = 'close') -> pd.DataFrame:
    d = bars[['session', column]].copy()
    d['session'] = pd.to_datetime(d.session).dt.tz_localize(None).dt.normalize()
    d[column] = pd.to_numeric(d[column], errors='coerce')
    if d.session.isna().any() or d.session.duplicated().any():
        raise ValueError('ACTION_COVERAGE_DUPLICATE_OR_INVALID_SESSION')
    if (~np.isfinite(d[column]) | d[column].le(0)).any():
        raise ValueError('ACTION_COVERAGE_INVALID_PRICE')
    return d.sort_values('session')


def audit_action_coverage(raw_bars: pd.DataFrame, adjusted_bars: pd.DataFrame,
                          recorded: pd.DataFrame, security_id: str, *,
                          tolerance: float = .005, max_ratio_term: int = 12) -> dict:
    """返回单只证券的行动覆盖结论；`verdict != 'ok'` 时应阻断其区间。"""
    sid = str(security_id)
    if missing := REQUIRED - set(recorded.columns):
        raise ValueError(f'ACTION_COVERAGE_COLUMNS_MISSING:{",".join(sorted(missing))}')
    r = _normalise(raw_bars)
    a = _normalise(adjusted_bars)
    rec = recorded[recorded.security_id.astype(str) == sid].copy()
    invalid: list = []
    duplicate: list = []
    if not rec.empty:
        rec['ex_date'] = pd.to_datetime(rec.ex_date).dt.normalize()
        kind = rec.action_type.astype(str).str.lower()
        ratio = pd.to_numeric(rec.ratio, errors='coerce').fillna(0.)
        cash = pd.to_numeric(rec.cash_amount, errors='coerce').fillna(0.)
        bad = ((kind.isin(['split', 'reverse_split']) & (ratio <= 0)) |
               (kind.eq('cash_dividend') & (cash <= 0)))
        invalid = sorted(rec.loc[bad, 'ex_date'].dt.strftime('%Y-%m-%d'))
        dup = rec.duplicated(['ex_date', 'action_type'], keep=False)
        duplicate = sorted(set(rec.loc[dup, 'ex_date'].dt.strftime('%Y-%m-%d')))
    derived = derive_actions(r, a, sid, tolerance=tolerance, max_ratio_term=max_ratio_term)
    jump_dates = set(derived.ex_date) if not derived.empty else set()
    rec_dates = set(rec.ex_date.dt.strftime('%Y-%m-%d')) if not rec.empty else set()
    unmatched = sorted(jump_dates - rec_dates)
    missing_prices = sorted(set(r.session) - set(a.session))
    joined = r.merge(a, on='session', suffixes=('_raw', '_adj')).sort_values('session')
    joined['step'] = (joined.close_adj / joined.close_raw).pct_change() + 1
    joined['previous_close'] = joined.close_raw.shift(1)
    mismatched = []
    for day, events in rec.groupby('ex_date'):
        point = joined[joined.session.eq(day)]
        if point.empty or pd.isna(point.previous_close.iloc[0]):
            continue
        previous = float(point.previous_close.iloc[0])
        expected = 1.
        for event in events.itertuples():
            if event.action_type in ('split', 'reverse_split') and float(event.ratio) > 0:
                expected *= float(event.ratio)
            elif event.action_type == 'cash_dividend' and 0 < float(event.cash_amount) < previous:
                expected /= 1 - float(event.cash_amount) / previous
        if abs(float(point.step.iloc[0]) / expected - 1) > tolerance:
            mismatched.append(str(day.date()))
    verdict = 'ok' if not (unmatched or invalid or duplicate or missing_prices or mismatched) else 'unexplained_actions'
    return {'security_id': sid, 'derived_jumps': int(len(derived)),
            'recorded_actions': int(len(rec)), 'matched': len(jump_dates & rec_dates),
            'unexplained_dates': unmatched, 'invalid_records': invalid,
            'duplicate_records': duplicate, 'verdict': verdict,
            'mismatched_records': mismatched,
            'missing_adjusted_dates': [str(day.date()) for day in missing_prices],
            'tolerance': tolerance}


def blocked_sessions(audit: dict) -> list:
    """需排除的时间点：无法解释的跳变日、非正比率/金额日、同日同类型重复日。"""
    days = set(audit.get('unexplained_dates', []))
    days.update(audit.get('invalid_records', []))
    days.update(audit.get('duplicate_records', []))
    days.update(audit.get('mismatched_records', []))
    days.update(audit.get('missing_adjusted_dates', []))
    return [pd.Timestamp(day).normalize() for day in sorted(days)]
