"""Setup 1/3/5/10/20/40 日 outcome 与执行择时反事实。"""
from typing import Optional

from .decision_ledger.outcome_jobs import OutcomeSettlement
from .decision_ledger.event_store import utc

HORIZONS = (1, 3, 5, 10, 20, 40)


def settle_setup(setup: dict, closes: list, settlement: OutcomeSettlement,
                 benchmark_closes: Optional[list] = None,
                 timed_entry_price: Optional[float] = None) -> int:
    """closes=[setup基准收盘, 后续日收盘...]；不足期限明确写 pending。"""
    setup_id = setup['setup_id']
    code = setup['code']
    n = 0
    for h in HORIZONS:
        horizon = f'{h}d'
        if len(closes) <= h:
            settlement.write_outcome(setup_id, {
                'horizon': horizon, 'data_quality': 'pending_future_bars',
                'body': {'code': code, 'setup_id': setup_id, 'status': 'pending',
                         'available_future_bars': max(0, len(closes) - 1)},
            }, subject_key=code)
            n += 1
            continue
        base, end = float(closes[0]), float(closes[h])
        ret = end / base - 1
        bench = None
        if benchmark_closes and len(benchmark_closes) > h:
            bench = float(benchmark_closes[h]) / float(benchmark_closes[0]) - 1
        path = [float(v) / base - 1 for v in closes[1:h + 1]]
        body = {'code': code, 'setup_id': setup_id, 'status': 'settled',
                'strategy': setup.get('strategy')}
        if timed_entry_price:
            body['timing_return_pct'] = end / float(timed_entry_price) - 1
            body['timing_increment_pct'] = body['timing_return_pct'] - ret
        settlement.write_outcome(setup_id, {
            'horizon': horizon, 'label_as_of': utc(), 'return_pct': ret,
            'benchmark_return_pct': bench,
            'excess_return_pct': ret - bench if bench is not None else None,
            'mfe_pct': max(path), 'mae_pct': min(path), 'realized_r': None,
            'data_quality': 'good', 'body': body,
        }, subject_key=code)
        n += 1
    return n
