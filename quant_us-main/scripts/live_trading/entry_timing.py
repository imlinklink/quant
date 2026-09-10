"""已批准日线 setup 的 15 分钟执行择时。"""
import pandas as pd

from .strategy_rules import completed_bars


def evaluate_entry_timing(setup, intraday, now, lookback_bars=5):
    bars = completed_bars(intraday, now, 15)
    if len(bars) < lookback_bars + 1:
        return {'triggered': False, 'reason': 'INSUFFICIENT_COMPLETED_BARS'}
    current = bars.iloc[-1]
    prior = bars.iloc[-lookback_bars - 1:-1]
    price = float(current['close'])
    if float(current['low']) <= float(setup['invalidation_price']):
        return {'triggered': False, 'reason': 'SETUP_INVALIDATED', 'price': price}
    if price > float(setup['max_chase_price']):
        return {'triggered': False, 'reason': 'PRICE_CHASE_LIMIT', 'price': price}
    trigger = max(float(setup['trigger_price']), float(prior['high'].max()))
    if price <= trigger:
        return {'triggered': False, 'reason': 'WAIT_INTRADAY_CONFIRMATION', 'price': price,
                'intraday_trigger': trigger}
    return {'triggered': True, 'reason': 'COMPLETED_BAR_BREAKOUT', 'price': price,
            'bar_end': current['bar_end'].isoformat(), 'intraday_trigger': trigger}
