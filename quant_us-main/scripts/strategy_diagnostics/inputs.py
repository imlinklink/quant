"""Explicit snapshot readers; no network or default production writes."""
import numpy as np
import pandas as pd

from scripts.medium_term.p2_selection_check import market_frame, trading_calendar


def load(data, root):
    def paths(kind):
        return [root / item['path'] for item in data['input_index'][kind]]
    frames = []
    for path in paths('prices'):
        frame = pd.read_csv(path)
        if 'security_id' not in frame:
            frame['security_id'] = path.name.split('-', 1)[1].split('.')[0].replace('US_', 'SEC-US-')
        frames.append(frame)
    prices = pd.concat(frames, ignore_index=True)
    required = {'security_id', 'session', 'raw_open', 'raw_high', 'raw_low', 'raw_close',
                'volume', 'asof_atr', 'scale_to_next'}
    if missing := required - set(prices):
        raise ValueError(f'PRICE_COLUMNS_MISSING:{sorted(missing)}')
    prices['session'] = pd.to_datetime(prices.session).dt.normalize()
    prices['security_id'] = prices.security_id.astype(str)
    if prices.duplicated(['security_id', 'session']).any():
        raise ValueError('PRICE_DUPLICATES')
    nums = prices[['raw_open', 'raw_high', 'raw_low', 'raw_close']].to_numpy(float)
    if not np.isfinite(nums).all() or (nums <= 0).any():
        raise ValueError('INVALID_OHLC')
    if ((prices.raw_high < prices[['raw_open', 'raw_close', 'raw_low']].max(axis=1)) |
            (prices.raw_low > prices[['raw_open', 'raw_close']].min(axis=1))).any():
        raise ValueError('OHLC_ORDER_INVALID')
    # ATR can be missing in warmup, but never infinite/negative; a missing live value
    # must be handled as DATA_BLOCKED rather than fabricated as zero.
    for key in ('asof_atr', 'scale_to_next'):
        values = prices[key].dropna().to_numpy(float)
        if not np.isfinite(values).all() or (values <= 0).any():
            raise ValueError(f'INVALID_FEATURE:{key}')
    market_path = paths('market')[0]
    market, calendar = market_frame(market_path), trading_calendar(market_path)
    if market.session.duplicated().any():
        raise ValueError('MARKET_DUPLICATES')
    w = data['research_window']
    start, end = pd.Timestamp(w['start']), pd.Timestamp(w['end'])
    sessions = calendar[(calendar >= start) & (calendar <= end)]
    if sessions.empty or start < calendar.min() or end > calendar.max():
        raise ValueError('WINDOW_OUTSIDE_CALENDAR')
    quality, actions = pd.read_csv(paths('quality')[0]), pd.read_csv(paths('actions')[0])
    if not {'security_id', 'quality_status', 'from_session', 'to_session'} <= set(quality):
        raise ValueError('QUALITY_COLUMNS_MISSING')
    if not {'security_id', 'action_type', 'ex_date'} <= set(actions):
        raise ValueError('ACTION_COLUMNS_MISSING')
    if not actions.empty and not actions.action_type.isin(['split', 'reverse_split', 'cash_dividend']).all():
        raise ValueError('UNSUPPORTED_CORPORATE_ACTION')
    return prices.sort_values(['security_id', 'session']), market, calendar, quality, actions
