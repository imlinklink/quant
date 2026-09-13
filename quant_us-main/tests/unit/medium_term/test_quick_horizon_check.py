import pandas as pd

from scripts.medium_term.quick_horizon_check import paired_horizons, summarize


def test_paired_horizons_uses_entry_open_and_same_complete_set():
    days = pd.bdate_range('2020-01-02', periods=125)
    prices = pd.DataFrame({'session': days, 'open': [100.] * 125,
                           'close': [110.] * 125})
    benchmark = pd.DataFrame({'session': days, 'open': [100.] * 125,
                              'close': [105.] * 125})
    entries = pd.DataFrame([
        {'entry_id': 'first', 'experiment': 'A', 'security_id': 'SEC-US-A',
         'entry_time': '2020-01-02 14:30:00+00:00'},
        {'entry_id': 'censored', 'experiment': 'A', 'security_id': 'SEC-US-A',
         'entry_time': '2020-01-10 14:30:00+00:00'},
        {'entry_id': 'other_group', 'experiment': 'B', 'security_id': 'SEC-US-A',
         'entry_time': '2020-01-02 14:30:00+00:00'},
    ])
    quality = pd.DataFrame([{'security_id': 'SEC-US-A', 'quality_status': 'verified'}])
    paired, funnel = paired_horizons(entries, quality, {'SEC-US-A': prices}, benchmark)
    assert funnel['paired_entries'] == 1
    assert funnel['excluded'] == {'RIGHT_CENSORED_120': 1}
    assert len(paired) == 5
    assert paired.loc[paired.holding_sessions.eq(20), 'exit_day'].iloc[0] == days[19]
    assert abs(paired.net_return.iloc[0] - .098) < 1e-12
    assert abs(paired.qqq_return.iloc[0] - .048) < 1e-12
    summary = summarize(paired)
    assert summary.trades.eq(1).all()
    assert summary.paired_mean_difference_vs_40.eq(0).all()
