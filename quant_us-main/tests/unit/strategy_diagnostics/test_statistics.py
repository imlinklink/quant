"""§6.2 / §7.2 / §11.1 的描述性统计。

三条纪律各有对应测试，它们正是这些统计最容易被写坏的地方：

1. **算不出来是 `None`，不是 0**（零分母 ≠ 结果为零）。
2. **采集了不等于报出来了** —— §7.2 的容量数据原先逐日采了 7 个字段，报告里却是空的。
3. **引擎新增退出原因必须看得见**，否则会静默漏出止损/期限统计。
"""
import re
from pathlib import Path

import pytest

from scripts.strategy_diagnostics.exit_attribution import (KNOWN_EXIT_REASONS,
                                                           summarize, trades_from_events)
from scripts.strategy_diagnostics.statistics import (annual_returns, capacity_summary,
                                                     concentration, robustness)

ROOT = Path(__file__).resolve().parents[3]
RISK = {'max_positions': 5, 'single_position_risk_bp': 100}


def _trade(**kw):
    base = dict(trade_id='t', security_id='SEC-US-A', entry_session='2025-01-02',
                exit_session='2025-02-03', exit_reason='STOP', status='closed',
                holding_sessions=20, net_pnl_micro=10_000_000, initial_risk_micro=5_000_000,
                net_r=2.0, followup={})
    base.update(kw)
    return base


def test_zero_denominators_are_none_not_zero():
    """没有已平仓交易时，胜率是 unavailable 而不是 0% —— 两者含义完全不同。"""
    out = summarize([])
    assert out['realized_win_rate'] is None
    assert out['exit_reasons'] == {}


def test_holding_period_and_account_contribution_are_reported():
    """持有期早就计算了却从未报出；对账户收益贡献则完全没算。"""
    out = summarize([_trade()], initial_cash_micro=100_000_000)
    row = out['exit_reasons']['STOP']
    assert row['mean_holding_sessions'] == 20
    assert row['share_of_account_return'] == pytest.approx(0.1)   # 1e7 / 1e8
    # 没有初始资金分母时必须是 None，不能悄悄按 0 算
    assert summarize([_trade()])['exit_reasons']['STOP']['share_of_account_return'] is None


def test_maturity_and_stop_recovery_only_count_mature_horizons():
    """`pending_or_missing` 的期限不能混进"未恢复"—— 那是"还没走完"，不是"没恢复"。"""
    trade = _trade(followup={
        '5': {'status': 'mature', 'sessions_to_entry_recovery': 3},
        '20': {'status': 'pending_or_missing', 'sessions_to_entry_recovery': None}})
    out = summarize([trade])
    assert out['followup_maturity']['5'] == {'mature': 1, 'pending_or_missing': 0, 'mature_share': 1.0}
    assert out['followup_maturity']['20']['mature_share'] == 0.0
    assert out['stop_recovery']['5'] == {'stop_trades_mature': 1, 'recovered_to_entry': 1,
                                         'recovered_share': 1.0, 'mean_sessions_to_recovery': 3.0,
                                         'unrecovered': 0}
    # 20 日还没有成熟样本 ⇒ 比例 unavailable（分母为 0），不是 0
    assert out['stop_recovery']['20']['recovered_share'] is None


def test_new_engine_exit_reason_is_visible():
    """引擎新增一个退出原因而统计没归类时，它必须出现在 unclassified_reasons 里。"""
    out = summarize([_trade(exit_reason='SOMETHING_NEW')])
    assert out['unclassified_reasons'] == ['SOMETHING_NEW']


def test_engine_sell_reasons_are_all_known():
    """引擎产出的 SELL 原因必须都在 KNOWN_EXIT_REASONS 里。

    **两种写法都要扫**。原先只扫 `_fill(session, 'SELL', ..., 'X', ...)` 的字面量，而意图侧
    的减仓路径是先把值赋给变量再传进去（`reason = 'REVIEW_REDUCE'`）—— 正则看不见它，
    于是 `REVIEW_REDUCE` 从未出现在常量里，而"引擎新增原因不会静默漏掉"这句保证是空的。
    没有这条，引擎哪天加一个 `TRAILING_EXIT`，止损后恢复统计会**静默**把它排除掉。
    """
    source = (ROOT / 'scripts/portfolio_shadow/paper_engine.py').read_text(encoding='utf-8')
    literal = set(re.findall(r"_fill\(session,\s*'SELL'[^)]*?'([A-Z_]{3,})'", source, re.S))
    assigned = set(re.findall(r"\breason\s*=\s*'([A-Z_]{3,})'", source))
    found = literal | assigned
    assert literal, '字面量扫描没找到任何 SELL 原因 —— 正则或引擎结构变了，请先修扫描器'
    assert assigned, '没扫到变量形式的 SELL 原因 —— 意图侧减仓路径变了，请先修扫描器'
    assert found <= set(KNOWN_EXIT_REASONS), f'引擎新增未归类的退出原因: {sorted(found - set(KNOWN_EXIT_REASONS))}'


def test_model_exit_reason_is_not_reported_as_unclassified():
    """引擎**已知**的退出原因不得被报成「未归类」。

    `unclassified_reasons` 是条喊狼来了的告警：`REVIEW_EXIT`（模型减仓/退出）是
    `paper_engine` 自己产出的、且已在 `KNOWN_EXIT_REASONS` 里的原因，却被手写的
    `classified` 集合漏掉 ⇒ 它一出现就被标成"未知原因"。喊错一次的告警，等真正的未知
    原因出现时已经没人看了。这里钉死"归类集合必须覆盖已知原因"。
    """
    for reason in KNOWN_EXIT_REASONS:
        assert summarize([_trade(exit_reason=reason)])['unclassified_reasons'] == [], reason
    # 反证：真正未知的原因仍必须看得见（否则这条测试会因为"永远为空"而失去意义）
    assert summarize([_trade(exit_reason='SOMETHING_NEW')])['unclassified_reasons'] == ['SOMETHING_NEW']


def test_capacity_summary_aggregates_the_collected_fields():
    """§7.2：这些字段原先只被采集、从未聚合，所以报告那一段是空的。"""
    rows = [
        {'session': '2025-01-02', 'held_positions': 0, 'max_positions': 5, 'equity_micro': 100_000_000,
         'market_value_micro': 0, 'cash_fraction': 1.0, 'exposure': 0.0,
         'rejections': {'MAX_POSITIONS': 1}, 'risk_used_micro': 0},
        {'session': '2025-01-03', 'held_positions': 5, 'max_positions': 5, 'equity_micro': 100_000_000,
         'market_value_micro': 50_000_000, 'cash_fraction': 0.5, 'exposure': 0.5,
         'rejections': {'MAX_POSITIONS': 1, 'DUPLICATE_ACTIVE_SECURITY': 2}, 'risk_used_micro': 5_000_000},
    ]
    out = capacity_summary(rows, RISK)
    assert out['sessions'] == 2
    assert out['cash']['days_without_positions'] == 1
    assert out['cash']['share_without_positions'] == 0.5
    assert out['exposure']['mean'] == pytest.approx(0.25)
    assert out['exposure']['max'] == 0.5
    assert out['slots']['mean_held'] == 2.5
    assert out['slots']['mean_occupancy'] == pytest.approx(0.5)
    assert out['slots']['days_at_capacity'] == 1
    # 预算 = 单仓风险 100bp × 最大仓位 5 = 500bp；实际 (0 + 5000000/1e8*1e4)/2 = 250bp
    assert out['risk_budget']['budget_bp'] == 500
    assert out['risk_budget']['mean_used_bp'] == pytest.approx(250.0)
    assert out['risk_budget']['mean_utilization'] == pytest.approx(0.5)
    assert out['rejections'] == {'MAX_POSITIONS': 2, 'DUPLICATE_ACTIVE_SECURITY': 2}


def test_concentration_uses_none_when_there_is_no_gain():
    """没有盈利交易时集中度是 unavailable —— 0 会被读成"毫不集中"。"""
    losing = [_trade(net_pnl_micro=-1_000_000)]
    out = concentration(losing, 100_000_000)
    assert out['valued'] == 1
    assert out['gross_gain_micro'] == 0
    assert out['top1_trade_share_of_gain'] is None
    assert out['top1_security_share_of_gain'] is None


def test_concentration_shares_and_by_security():
    trades = [_trade(security_id='SEC-US-A', net_pnl_micro=3_000_000),
              _trade(security_id='SEC-US-B', net_pnl_micro=1_000_000)]
    out = concentration(trades, 100_000_000)
    assert out['top1_trade_share_of_gain'] == pytest.approx(0.75)
    assert out['top1_security_share_of_gain'] == pytest.approx(0.75)
    assert out['top2_security_share_of_gain'] == pytest.approx(1.0)
    assert out['by_security_gain_micro'] == {'SEC-US-A': 3_000_000, 'SEC-US-B': 1_000_000}
    assert out['by_security_net_micro'] == {'SEC-US-A': 3_000_000, 'SEC-US-B': 1_000_000}


def test_concentration_numerator_excludes_losses():
    """分子只取**盈利**中最大的前 N —— 含亏损会把集中度系统性低估。

    实测（review 指出）：一笔赚 100、一笔亏 90 时，`top3_trade_share_of_gain` 原先返回
    **10%**（(100−90)/100），而全部盈利都来自那一笔，应当是 **100%**。集中度正是用来判断
    "这点收益是不是靠个别标的"的指标，低估它等于把风险说小。证券轴同病：
    原先按**净损益**排序取前 N，同样把亏损算进了分子。
    """
    out = concentration([_trade(net_pnl_micro=100_000_000),
                         _trade(net_pnl_micro=-90_000_000)], 100_000_000)
    assert out['gross_gain_micro'] == 100_000_000
    assert out['top1_trade_share_of_gain'] == pytest.approx(1.0)
    assert out['top3_trade_share_of_gain'] == pytest.approx(1.0)
    assert out['top1_security_share_of_gain'] == pytest.approx(1.0)
    assert out['top2_security_share_of_gain'] == pytest.approx(1.0)
    # 同证券的盈利与亏损相抵后为净 0.1 亿：盈利口径与净口径必须都能看见，且不可混读
    same = concentration([_trade(security_id='SEC-US-A', net_pnl_micro=100_000_000),
                          _trade(security_id='SEC-US-A', net_pnl_micro=-90_000_000)], 100_000_000)
    assert same['by_security_gain_micro'] == {'SEC-US-A': 100_000_000}
    assert same['by_security_net_micro'] == {'SEC-US-A': 10_000_000}


def test_holding_period_counts_zero_session_trades():
    """`holding_sessions=0` 必须计入 —— 真值判断会把它整类丢掉，均值只会偏高。

    review 指出：009 有两笔当日止损。原先 `if t.get('holding_sessions')` 把 0 判为假，
    于是"最短的持有"从均值里消失。改成判 `None`。
    """
    out = summarize([_trade(holding_sessions=0, net_r=-1.0),
                     _trade(holding_sessions=10, net_r=1.0)])
    row = out['exit_reasons']['STOP']
    assert row['mean_holding_sessions'] == pytest.approx(5.0)   # (0 + 10) / 2
    assert summarize([_trade(holding_sessions=0)])['exit_reasons']['STOP'][
        'mean_holding_sessions'] == 0.0     # 0，不是 None


def test_holding_period_is_calendar_based_and_uniform():
    """持有期按**参考交易日历**计，入场日与退出日都算：当日进出 = 1。

    引擎自报的 `holding_sessions` 数的是"活过几个收盘"（当日止损在计数之前 ⇒ 0），
    与时间退出的 1 起算序号混在一列里。两套口径的差异要**看得见**，不是把引擎值丢掉。
    """
    import pandas as pd
    days = pd.bdate_range('2025-01-02', periods=5)
    prices = pd.DataFrame({'security_id': 'A', 'session': days, 'raw_close': 100.0})
    def fill(side, session, reason):
        return {'type': 'fill', 'side': side, 'security_id': 'A', 'session': str(session.date()),
                'shares': 10, 'price_micro': 100_000_000, 'stop_micro': 90_000_000,
                'fee_micro': 10_000, 'reason': reason, 'opportunity_id': 'o1'}
    same_day = trades_from_events([fill('BUY', days[0], 'ENTRY'), fill('SELL', days[0], 'STOP')],
                                  type('S', (), {'positions': {}})(), prices, pd.DataFrame(),
                                  days, days[-1])
    assert same_day[0]['holding_sessions'] == 1              # 当日进出
    assert same_day[0]['holding_sessions_engine'] == 0       # 引擎口径（活过的收盘数）
    out = summarize(same_day)
    assert out['holding_sessions_engine_mismatch'] == 1
    assert out['holding_basis']
    # 持有到第 3 个 session 收盘退出 = 3
    held = trades_from_events([fill('BUY', days[0], 'ENTRY'), fill('SELL', days[2], 'TIME_EXIT')],
                              type('S', (), {'positions': {}})(), prices, pd.DataFrame(),
                              days, days[-1])
    assert held[0]['holding_sessions'] == 3


def test_annual_returns_come_from_nav_not_from_trade_grouping():
    """年度**账户**收益按逐日净值算；交易损益按退出年归集**不是**年度收益。

    review 指出：跨年持仓会把整笔盈亏压到退出那一年（2024-12-20 入场、2025-01-05 出场
    的一笔，整笔记在 2025）。故两者必须分开：`annual` 由净值算，`by_exit_year` 只是
    交易损益的归集，且字段名与 note 都要写明口径。
    """
    navs = [{'session': '2025-01-02', 'full_cost_equity': 100_000_000},
            {'session': '2025-06-30', 'full_cost_equity': 110_000_000},
            {'session': '2025-12-31', 'full_cost_equity': 120_000_000},
            {'session': '2026-12-31', 'full_cost_equity': 90_000_000}]
    annual = annual_returns(navs, 100_000_000)
    assert annual['years']['2025']['return'] == pytest.approx(0.2)    # 100 → 120
    assert annual['years']['2026']['return'] == pytest.approx(-0.25)  # 120 → 90
    assert annual['count'] == 2 and annual['positive'] == 1
    # 首年以**初始资金**为分母：若用当年第一行净值，会漏掉第一年的收益
    first = annual_returns([{'session': '2024-06-30', 'full_cost_equity': 150_000_000}],
                           100_000_000)
    assert first['years']['2024']['return'] == pytest.approx(0.5)
    # 交易损益归集：只含已平仓，且明说不是年度收益
    out = robustness([_trade(entry_session='2024-12-20', exit_session='2025-01-05',
                             net_pnl_micro=50_000_000),
                      _trade(entry_session='2025-06-01', exit_session=None,
                             status='right_censored', net_pnl_micro=1_000_000)])
    assert out['by_exit_year'] == {'2025': {'count': 1, 'net_micro': 50_000_000}}
    assert out['censored_excluded'] == 1
    assert '不是年度账户收益' in out['basis']
    text = _rendered()
    assert '年度账户收益（按逐日净值）' in text
    assert '交易损益按退出年归集（不是年度收益）' in text


def test_robustness_groups_by_exit_year():
    trades = [_trade(exit_session='2024-03-01', net_pnl_micro=1_000_000),
              _trade(exit_session='2025-03-01', net_pnl_micro=-3_000_000),
              _trade(exit_session='2025-06-01', net_pnl_micro=2_000_000)]
    out = robustness(trades)
    assert out['years'] == 2
    assert out['by_exit_year']['2024']['net_micro'] == 1_000_000
    assert out['by_exit_year']['2025']['count'] == 2
    assert out['years_positive'] == 1
    assert out['years_positive_share'] == pytest.approx(0.5)


def _capacity_row(**kw):
    row = {'session': '2025-01-02', 'held_positions': 0, 'max_positions': 5,
           'equity_micro': 100_000_000, 'market_value_micro': 0, 'cash_fraction': 1.0,
           'exposure': 0.0, 'rejections': {}, 'risk_used_micro': 0}
    row.update(kw)
    return row


def _rendered(trades=None, capacity=None, checks=None):
    """用**真的**统计函数拼一个 result 再渲染，避免夹具与实现漂移。"""
    from scripts.strategy_diagnostics.report import render
    trades = [_trade()] if trades is None else trades
    capacity = capacity or []
    result = {
        'study_id': 'TEST-001', 'manifest_hash': 'x', 'sessions': 1,
        'full_cost_return': None, 'max_drawdown': None,
        'verdict': None, 'phase_conclusion': 'INSUFFICIENT_EVIDENCE',
        'checks': checks or {},
        'funnel': {'candidate_count': 0, 'ready_fraction': None, 'candidate_states': {},
                   'stages': {}, 'note': 'n'},
        'exits': summarize(trades, 100_000_000),
        'statistics': {'capacity': capacity_summary(capacity, RISK),
                       'concentration': concentration(trades, 100_000_000),
                       'robustness': robustness(trades),
                       'annual': annual_returns(
                           [{'session': '2025-01-02', 'full_cost_equity': 100_000_000},
                            {'session': '2025-12-31', 'full_cost_equity': 110_000_000}],
                           100_000_000)},
        'capacity': capacity, 'pending_execution_at_window_end': 0,
        'excluded_actions': {'count': 0, 'by_reason': {}},
        'audit': {'warnings': []}, 'limitations': [],
    }
    return render(result)


def test_report_parity_line_is_derived_not_hardcoded():
    """首页那句"基线是否对齐"必须从 checks 算出来。

    原先写死"工程检查均通过" —— 而"本引擎自洽"与"与旧基线一致"是两件事，写死会让
    "对账没跑成"与"对账通过"在报告上长得一模一样（本模块反复要防的形态）。
    """
    verified = _rendered(checks={'baseline_parity': {
        'status': 'VERIFIED', 'n_sessions': 2631, 'n_diffs': 0, 'tolerance_usd': 0.0}})
    assert '逐日零分歧' in verified and '2631' in verified
    assert '未完成' not in verified
    missed = _rendered(checks={'baseline_parity': {
        'status': 'NOT_EVALUATED', 'reason': 'BASELINE_PARITY_NO_PREPARED_ENTRIES'}})
    assert '未完成' in missed and '不构成 §15 P0 的基线' in missed
    # 完全没有这条 check 时也不得默认"通过"
    assert '未完成' in _rendered(checks={})


def test_report_conclusion_is_derived_not_hardcoded():
    """首页结论必须从判定字段算出来。

    写死的话，"只有基线、还谈不上判定" 与 "比过了、判定为证据不足" 在报告上长得一模一样 ——
    这正是本模块反复要防的形态。§9.3 的判定只有比过 challenger 才存在，故 `verdict is None`
    时必须如实说"没作判定"，而不是报一个最接近的令牌。
    """
    from scripts.strategy_diagnostics.report import conclusion
    assert '未作 §9.3 判定' in conclusion({'verdict': None, 'phase_conclusion': 'INSUFFICIENT_EVIDENCE'})
    assert 'INSUFFICIENT_EVIDENCE' in conclusion({'verdict': None, 'phase_conclusion': 'INSUFFICIENT_EVIDENCE'})
    assert conclusion({'verdict': 'CONCENTRATED'}) == '**结论：CONCENTRATED。**'
    # 判定令牌只能是 §9.3 枚举里的（或"还没有"）
    from scripts.strategy_diagnostics.experiments import VERDICT_TOKENS
    assert set(VERDICT_TOKENS) == {'DATA_INVALID', 'ENGINEERING_BLOCKED', 'INSUFFICIENT_SAMPLE',
                                   'NO_IMPROVEMENT', 'RISK_REJECTED', 'CONCENTRATED',
                                   'INCONCLUSIVE', 'EVIDENCE_SUPPORTED'}


def test_report_renders_unavailable_for_zero_denominators():
    """§14「报告诚实性：零分母为 unavailable」。

    一笔都没平仓时胜率必须是 unavailable —— 写成 `0.00%` 会被读成"试过了但全输"。
    """
    text = _rendered(trades=[_trade(status='right_censored', exit_reason=None)],
                     capacity=[_capacity_row()])
    assert '已实现胜率：unavailable' in text
    assert '占账户收益' in text


def test_report_shows_unclassified_exit_reasons_loudly():
    """引擎新增退出原因必须出现在报告里，而不是静默漏出止损/期限统计。"""
    text = _rendered(trades=[_trade(exit_reason='SOMETHING_NEW')], capacity=[_capacity_row()])
    assert '未归类的退出原因' in text and 'SOMETHING_NEW' in text


def test_report_includes_capacity_and_concentration_sections():
    """§7.2 / §6.2 的两段必须真的出现在报告里，而不是只存在于 json。"""
    text = _rendered(capacity=[_capacity_row()])
    assert '## 容量与资金使用' in text and '风险预算' in text
    assert '## 集中度与稳健性' in text and '盈利集中度' in text
    # 描述性统计不得被读成结论
    assert '不构成规则缺陷' in text


def test_empty_and_nonempty_summaries_have_the_same_shape():
    """空样本与非空样本必须返回**同一组键**。

    这是本模块第一条纪律（算不出来是 None）最容易被违反的方式：非空分支逐个守住了零分母，
    空分支却早退成一个残缺形状 —— 于是消费方取 `stat['cash']` 直接 KeyError。原先
    `capacity_summary([])` 只返回 `{'sessions', 'note'}`、`concentration([])` 只返回
    `{'trades', 'valued', 'note'}`，报告对空窗口直接崩（xfail 那条测试）。
    """
    empty_capacity = capacity_summary([], RISK)
    full_capacity = capacity_summary([_capacity_row()], RISK)
    assert set(empty_capacity) == set(full_capacity)
    for key in ('cash', 'exposure', 'slots', 'risk_budget'):
        assert set(empty_capacity[key]) == set(full_capacity[key]), key
    empty_con, full_con = concentration([], 100_000_000), concentration([_trade()], 100_000_000)
    assert set(empty_con) == set(full_con)
    assert set(robustness([])) == set(robustness([_trade()]))


def test_no_valued_trade_is_none_not_zero():
    """一笔都不可计值时，净损益是 unavailable —— 写成 0.00 会被读成"这批交易白做了"。

    `valued == 0` 有两条路径：窗口内一笔没成交，以及全部右删失且拿不到估值（`marks` 缺该
    证券）。两条都**不是**"算出来等于零"。而"有可计值交易、合计恰好为零"仍应报 0.00。
    """
    out = concentration([], 100_000_000)
    assert out['valued'] == 0 and out['net_micro'] is None
    assert out['gross_loss_micro'] is None
    assert out['share_of_account_return'] is None
    unloved = concentration([_trade(status='right_censored', net_pnl_micro=None)], 100_000_000)
    assert unloved['valued'] == 0 and unloved['trades'] == 1 and unloved['net_micro'] is None
    flat = concentration([_trade(net_pnl_micro=0)], 100_000_000)
    assert flat['valued'] == 1 and flat['net_micro'] == 0


def test_report_survives_an_empty_window():
    """空窗口（零成交、零会话）也必须渲染出完整报告，而不是 KeyError。"""
    text = _rendered(trades=[], capacity=[])
    assert '## 容量与资金使用' in text and '## 集中度与稳健性' in text
    assert '已实现胜率：unavailable' in text
    assert 'unavailable' in text  # 每一处零分母都如实标注，不落成 0.00



