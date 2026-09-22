"""展示必须用真实状态与生命周期，不凭空制造模型贡献。"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from ops.analytics_export.insights import (contribution_cases, decision_rows,
                                          forward_performance, lifecycle_results)
from ops.analytics_export.sections import _report, _build_todo

R, L = 'SHADOW:T:R', 'SHADOW:T:L'


class Store:
    def __init__(self, apps=(), events=None, nav=None):
        self.apps, self.ev, self.nav = list(apps), events or {}, nav or {}

    def applications(self, scope=None):
        return [a for a in self.apps if scope is None or a['scope'] == scope]

    def events(self, scope):
        return self.ev.get(scope, [])

    def daily_nav(self, scope):
        return self.nav.get(scope, [])

    def opportunity(self, oid):
        return {'security_id': 'X', 'planned_execution_session': '2026-09-22'}

    def packet_for_opportunity(self, key):
        return {}

    def job_run(self, key):
        return {'model_id': 'real-model', 'status': 'COMPLETED'}


def fill(side, px, day, **extra):
    return dict(type='fill', security_id='X', opportunity_id='o', side=side,
                shares=10, price_micro=px * 1000000, fee_micro=1000000,
                session=day, **extra)


def app(action='POSITION_EXIT', **extra):
    return dict(scope=L, opportunity_id='o@pos:2026-09-23', decision_id='d',
                action=action, raw_action='exit', reason_code='THESIS_WEAKENED',
                decision_frozen=True, execution_applied=True, model_cost=1000000,
                cost_uncertain=False, **extra)


def test_position_is_not_entry_and_requires_actual_fill_for_change():
    s = Store([app()])
    row = decision_rows(s, '2026-09-22')[0]
    assert row['role'] == 'position'
    assert row['valid_real_review']
    assert not row['path_changed']
    s.ev[L] = [fill('SELL', 11, '2026-09-23', decision_id='d', reason='REVIEW_EXIT')]
    assert decision_rows(s, '2026-09-22')[0]['path_changed']


def test_lifecycle_closed_comparison_counts_all_model_cost_once():
    events = {R: [fill('BUY', 10, '2026-09-22'), fill('SELL', 8, '2026-09-26')],
              L: [fill('BUY', 10, '2026-09-22'),
                  fill('SELL', 11, '2026-09-23', decision_id='d', reason='REVIEW_EXIT')]}
    s = Store([app()], events)
    cases = contribution_cases(s, [R, L], decision_rows(s, '2026-09-22'))
    assert cases['mature_count'] == 1
    assert cases['best'][0]['delta_usd'] == 29  # 30 price difference minus $1 model
    assert cases['early_exit_harm']['cases'][0]['early_exit']
    s.ev[R].pop()
    cases = contribution_cases(s, [R, L], decision_rows(s, '2026-09-22'))
    assert cases['pending_count'] == 1
    assert not cases['best']


def test_unknown_cost_and_replay_never_rank_as_contribution():
    events = {R: [fill('BUY', 10, '2026-09-22'), fill('SELL', 8, '2026-09-26')],
              L: [fill('BUY', 10, '2026-09-22'),
                  fill('SELL', 11, '2026-09-23', decision_id='d', reason='REVIEW_EXIT')]}
    a = app(); a['cost_uncertain'] = True
    s = Store([a], events)
    cases = contribution_cases(s, [R, L], decision_rows(s, '2026-09-22'))
    assert cases['excluded_count'] == 1 and not cases['best']
    assert decision_rows(s, '2026-09-24')[0]['phase'] == 'replay'
    assert not contribution_cases(s, [R, L], decision_rows(s, '2026-09-24'))['all']


def test_split_dividend_cashflow_and_reentry_keep_identity():
    events = [fill('BUY', 10, '2026-09-22'),
              dict(type='split', security_id='X', ratio=2, kind='split'),
              dict(type='dividend_record', security_id='X', total_micro=2000000),
              dict(type='dividend_pay', total_micro=2000000)]
    sell = fill('SELL', 6, '2026-09-26'); sell['shares'] = 20
    events.append(sell)
    again = fill('BUY', 7, '2026-09-27'); again['opportunity_id'] = 'new'
    events.append(again)
    results = lifecycle_results(events)
    assert results['o']['closed'] and results['o']['pnl_micro'] == 20000000
    assert not results['new']['closed']


def test_invalid_real_response_excluded():
    s = Store([app()])
    s.job_run = lambda _: {'model_id': 'real', 'status': 'COMPLETED', 'validation_errors': ['bad']}
    assert not decision_rows(s, '2026-09-22')[0]['valid_real_review']
    s.job_run = lambda _: {'status': 'COMPLETED'}
    assert not decision_rows(s, '2026-09-22')[0]['valid_real_review']


def test_forward_only_return_uses_replay_anchor_and_stops_at_gap():
    def nav(day, eq):
        return dict(session=day, full_cost_equity=eq, valuation_status='OK', cost_status='OK')
    s = Store(nav={R: [nav('2026-09-18', 200), nav('2026-09-21', 220), nav('2026-09-22', 230)],
                   L: [nav('2026-09-18', 200), nav('2026-09-21', 240), nav('2026-09-23', 260)]})
    m = SimpleNamespace(account_scopes=[R, L], initial_cash=100)
    p = forward_performance(s, m, '2026-09-21', _report())
    assert p['common_sessions'] == 1
    assert round(p['L_minus_R_return'], 6) == 0.1
    assert p['excluded_after_gap'] == 2
    assert forward_performance(s, m, '2026-10-01', _report())['common_sessions'] == 0
    assert forward_performance(s, m, None, _report())['status'] == 'NOT_COLLECTED'


def test_budget_less_than_reservation_is_blocked_and_veto_not_claimed_filled():
    todo, _, _ = _build_todo({'veto_unapplied': 1}, {},
                            {'status': 'OK', 'remaining_usd': .005, 'call_reserve_usd': .01}, [], 'OK')
    assert any('预算不足' in x for x in todo)
    assert not any('账户照样买入' in x for x in todo)


def test_harmful_exit_appears_in_worst_and_veto_avoided_loss_is_mature():
    a = app()
    events = {R: [fill('BUY', 10, '2026-09-22'), fill('SELL', 15, '2026-09-26')],
              L: [fill('BUY', 10, '2026-09-22'),
                  fill('SELL', 11, '2026-09-23', decision_id='d', reason='REVIEW_EXIT')]}
    s = Store([a], events)
    cases = contribution_cases(s, [R, L], decision_rows(s, '2026-09-22'))
    assert cases['worst'][0]['delta_usd'] == -41
    assert not cases['best']
    a.update(opportunity_id='o', action='VETO', raw_action='veto')
    s.ev[L] = []
    s.ev[R][-1] = fill('SELL', 8, '2026-09-26')
    cases = contribution_cases(s, [R, L], decision_rows(s, '2026-09-22'))
    assert cases['best'][0]['delta_usd'] == 21
    assert cases['best'][0]['L_exit_session'] is None
