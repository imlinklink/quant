"""中期 setup 状态机和候选生成。无 IO、无模型调用。"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .decision_ledger.event_store import stable_id

STATES = ('FALLING', 'CAPITULATION', 'STABILIZING', 'REVERSING', 'CONFIRMED')


def transition(previous: str, snapshot: Dict[str, Any],
               config: Optional[Dict[str, Any]] = None) -> Tuple[str, list]:
    cfg = config or {}
    if snapshot.get('quality', {}).get('status') != 'pass':
        return 'FALLING', ['DATA_QUALITY_FAILED']
    f, st = snapshot['features'], snapshot['structure']
    if bool(cfg.get('require_weekly_gate', True)) and not f.get('weekly_gate', False):
        return 'FALLING', ['WEEKLY_REGIME_BLOCKED']
    close, ma20 = f['close'], f['ma20']
    structural_break = st.get('swing_low') is not None and close < st['swing_low']
    falling = structural_break or (close < f['ma50'] and f['ma20_slope_5d'] < 0 and
                                   not f['no_new_low'])
    if falling:
        return 'FALLING', ['DOWNTREND_ACTIVE']
    capitulation = (f['one_day_return'] <= float(cfg.get('capitulation_return', -.08)) and
                    f['volume_ratio_20d'] >= float(cfg.get('capitulation_volume', 1.5)))
    if capitulation and previous in ('FALLING', 'CAPITULATION'):
        return 'CAPITULATION', ['CAPITULATION_BAR']
    stabilizing = bool(f['no_new_low'])
    reversing = bool(stabilizing and st.get('higher_low') and
                     (close > ma20 or f['ma20_slope_5d'] >= 0))
    confirmed = bool(reversing and st.get('reversal_level') is not None and
                     close > st['reversal_level'])
    if previous in ('FALLING', 'CAPITULATION'):
        return ('STABILIZING', ['NO_NEW_LOW']) if stabilizing else (previous, ['WAIT'])
    if previous == 'STABILIZING':
        return ('REVERSING', ['HIGHER_LOW', 'RECLAIM_MA20']) if reversing else (
            'STABILIZING', ['NO_NEW_LOW'])
    if previous == 'REVERSING':
        return ('CONFIRMED', ['BREAK_REVERSAL_LEVEL']) if confirmed else (
            'REVERSING', ['REVERSAL_IN_PROGRESS'])
    if previous == 'CONFIRMED':
        return ('CONFIRMED', ['CONFIRMATION_HELD']) if close >= ma20 else (
            'STABILIZING', ['LOST_MA20'])
    return 'FALLING', ['INVALID_PREVIOUS_STATE']


def setup_family(snapshot: Dict[str, Any], state: str,
                 require_weekly_gate: bool = True) -> Optional[str]:
    f, st = snapshot.get('features', {}), snapshot.get('structure', {})
    if require_weekly_gate and not f.get('weekly_gate', False):
        return None
    trend_ok = (f.get('close', 0) > f.get('ma200', float('inf')) and
                f.get('ma50_slope_20d', -1) > 0 and
                (f.get('relative_strength_20d') is None or
                 f.get('relative_strength_20d') > 0))
    near_ma = abs(f.get('close', 0) - f.get('ma20', 0)) <= max(
        f.get('atr14', 0) * .75, 1e-9)
    if trend_ok and near_ma and st.get('higher_low'):
        return 'trend_pullback'
    if state in ('REVERSING', 'CONFIRMED'):
        return 'reversal_confirmed'
    return None


def build_setup_candidate(code: str, snapshot: Dict[str, Any], state: str,
                          selection_decision_id: str = '',
                          config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    cfg = config or {}
    family = setup_family(snapshot, state, bool(cfg.get('require_weekly_gate', True)))
    if not family or snapshot.get('quality', {}).get('status') != 'pass':
        return None
    f, st = snapshot['features'], snapshot['structure']
    trigger = st.get('reversal_level') or f['close']
    invalidation = st.get('swing_low')
    if invalidation is None or invalidation >= trigger:
        return None
    stop = invalidation - .2 * f['atr14']
    max_stop_atr = float(cfg.get('max_stop_atr', 2.0))
    if trigger - stop > max_stop_atr * f['atr14']:
        return None
    session = snapshot['session']
    setup_id = stable_id('setup', code, family, session, snapshot['feature_version'])
    return {'setup_id': setup_id, 'code': code, 'strategy': family,
            'state': state, 'session': session, 'status': 'active',
            'selection_decision_id': selection_decision_id,
            'trigger_price': float(trigger), 'invalidation_price': float(invalidation),
            'initial_stop': float(stop),
            'max_chase_price': float(trigger + float(cfg.get('max_chase_atr', .5)) * f['atr14']),
            'risk_per_share': float(trigger - stop),
            'weekly_gate': bool(f.get('weekly_gate')), 'weekly_regime': f.get('weekly_regime'),
            'daily_confirmed': state == 'CONFIRMED',
            'reason_codes': ['WEEKLY_' + str(f.get('weekly_regime', 'unknown')).upper(),
                             'DAILY_' + state],
            'feature_version': snapshot['feature_version']}
