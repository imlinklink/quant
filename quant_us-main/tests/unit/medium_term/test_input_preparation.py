from pathlib import Path

import pandas as pd

from scripts.medium_term.prepare_etf_inputs import prepare
from scripts.medium_term.weekly_features import add_weekly_exit_features
from scripts.medium_term.momentum_features import point_in_time_momentum_snapshot


def _write_partition(root: Path, adjustment: str, code: str, closes):
    path = root / 'day' / adjustment / 'year=2020' / f'{code.replace(".", "_")}.csv.gz'
    path.parent.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range('2020-01-06', periods=len(closes))
    pd.DataFrame({'time_key': dates, 'open': closes, 'high': closes,
                  'low': closes, 'close': closes, 'volume': 100}).to_csv(
                      path, index=False, compression='gzip')


def test_prepare_etf_inputs_is_blocked_until_actions_are_verified(tmp_path):
    codes = [f'US.T{i}' for i in range(8)]
    universe = tmp_path / 'universe.csv'
    pd.DataFrame({'code': codes}).to_csv(universe, index=False)
    for code in codes:
        _write_partition(tmp_path / 'prices', 'none', code, [100., 100., 100.])
        _write_partition(tmp_path / 'prices', 'qfq', code, [100., 100., 100.])
    summary = prepare(tmp_path / 'prices', universe, tmp_path / 'out')
    assert summary['status'] == 'blocked_pending_action_verification'
    assert summary['formal_performance_allowed'] is False
    assert summary['futu_action_import'] == 'disabled_zero_rows_in_pilot'
    assert summary['action_candidates_are_accounting_input'] is False
    assert summary['files']['actions']['rows'] == 0


def test_weekly_features_marks_holiday_short_week_and_rejects_partial_tail():
    # 1/20 is Monday holiday; Thursday 1/23 is only a partial data tail.
    calendar = pd.to_datetime(['2020-01-06', '2020-01-07', '2020-01-08', '2020-01-09',
                               '2020-01-10', '2020-01-13', '2020-01-14', '2020-01-15',
                               '2020-01-16', '2020-01-17', '2020-01-21', '2020-01-22',
                               '2020-01-23', '2020-01-24', '2020-01-27'])
    bars = pd.DataFrame({'session': calendar[:-2],
                         'raw_close': range(100, 100 + len(calendar) - 2)})
    out = add_weekly_exit_features(bars, calendar, ma_weeks=2)
    completed = out.loc[out.week_complete, 'session'].dt.strftime('%Y-%m-%d').tolist()
    assert completed == ['2020-01-10', '2020-01-17']
    assert out.loc[out.session.eq(pd.Timestamp('2020-01-17')), 'weekly_ma20'].iloc[0] == 106.5
    assert not out.loc[out.session.eq(pd.Timestamp('2020-01-23')), 'week_complete'].iloc[0]


def test_weekly_features_rejects_week_with_missing_middle_session():
    calendar = pd.bdate_range('2020-01-06', '2020-01-20')
    bars = pd.DataFrame({'session': calendar[calendar != pd.Timestamp('2020-01-08')],
                         'raw_close': 100.})
    out = add_weekly_exit_features(bars, calendar, ma_weeks=1)
    assert not out.loc[out.session.eq(pd.Timestamp('2020-01-10')), 'week_complete'].iloc[0]
    assert out.loc[out.session.eq(pd.Timestamp('2020-01-17')), 'week_complete'].iloc[0]


def test_momentum_reconstructs_full_lookback_at_decision_split():
    dates = pd.bdate_range('2020-01-06', periods=8)
    raw = pd.DataFrame({'security_id': 'A', 'session': dates, 'open': [100.] * 4 + [50.] * 4,
                        'high': [100.] * 4 + [50.] * 4,
                        'low': [100.] * 4 + [50.] * 4,
                        'close': [100.] * 4 + [50.] * 4, 'volume': 100})
    actions = pd.DataFrame([{'security_id': 'A', 'ex_date': dates[4],
                             'action_type': 'split', 'ratio': 2., 'cash_amount': 0.}])
    snap = point_in_time_momentum_snapshot(raw, actions, dates[-1],
                                            lookback_6m=6, skip_1m=1, lookback_12m=7)
    assert snap.iloc[0].mom_6m == 0
    assert snap.iloc[0].mom_12_1 == 0
