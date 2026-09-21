import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts.strategy_diagnostics import manifest, experiments
from scripts.strategy_diagnostics.funnel import Funnel
from scripts.strategy_diagnostics.exit_attribution import followup


def _write_baseline(tmp_path, name, *, top_n=5, start_session='2025-01-31'):
    """两个夹具共用的父策略 manifest。**一处定义**：两份写法的 `risk_policy` 一旦不同，
    夹具之间的差异就不再只是"数据不同"，而调参的效应会被误读成数据的效应。"""
    base = dict(experiment_id='test-base', status='FROZEN', parent_strategy_id='B3',
        parent_version='1', parent_code_hash='test', universe_id='test', universe_hash='test',
        account_scopes=['SHADOW:test-base:R'], initial_cash=100000., currency='USD',
        risk_policy=dict(single_position_risk_bp=100, max_weight_bp=2000, max_positions=5, top_n=top_n),
        execution_policy=dict(entry_rule='b3', exit_policy_id='H60', horizon=60,max_wait_sessions=20),
        llm_policy=dict(overlay='fixed_pass',use_real_model=False), calendar_version='test',
        data_hashes={}, evaluation_protocol=dict(main_metric='baseline',enrollment_window='2025-01-31/2025-03-31',review_date='2025-03-31',cost_allocation='R'), start_session=start_session)
    manifest.write_json(tmp_path / name, base)
    return tmp_path / name


@pytest.fixture
def study(tmp_path):
    days = pd.bdate_range('2024-01-01', '2025-04-01')
    close = 100 + np.arange(len(days)) * .25
    prices = pd.DataFrame(dict(security_id='SEC-US-A', session=days,
        raw_open=close, raw_high=close + .1, raw_low=close - .1,
        raw_close=close, volume=1000000, asof_atr=2., scale_to_next=1.))
    prices.to_csv(tmp_path / 'prices.csv', index=False)
    pd.DataFrame(dict(security_id='SEC-US-QQQ', session=days, close=close)).to_csv(tmp_path / 'market.csv', index=False)
    pd.DataFrame([dict(security_id='SEC-US-A', quality_status='verified',
        from_session=str(days[0].date()), to_session=str(days[-1].date()))]).to_csv(tmp_path / 'quality.csv', index=False)
    pd.DataFrame(columns=['security_id','action_type','ex_date','ratio','cash_amount']).to_csv(tmp_path / 'actions.csv', index=False)
    d = manifest.draft('test-study', _write_baseline(tmp_path, 'baseline.json'),
        {k:[tmp_path / (k + '.csv')] for k in ['prices','market','quality','actions']},
        '2025-01-31','2025-03-31', window_rationale='fixture：仅验证机制，不代表研究窗口')
    return manifest.freeze(d, tmp_path / 'output')


def test_observer_divergence_aborts_the_run(study, monkeypatch):
    """§14 第 1 行「观察器旁路」必须**能被证伪** —— 这是 P1 的验收项。

    `_run` 每个 session 同时问带 sink 的 `gen` 与不带 sink 的 `control`，不一致就
    `raise OBSERVER_CHANGED_OPPORTUNITIES`。但一条从不失败的断言等于没测到：
    §14 明写「每个关键回归应证明**在注入对应缺陷时失败**」。这条故意让带 sink 的实例
    多吐一条机会，验证守卫真的会中止，而不是永远为真。
    """
    import scripts.strategy_diagnostics.experiments as ex
    original = ex.IncrementalCandidateGenerator.opportunities_for

    def divergent(self, session):
        found = original(self, session)
        if getattr(self, 'observation_sink', None) is not None and found:
            return [*found, replace(found[0], source_candidate_id=found[0].source_candidate_id + '-injected')]
        return found

    monkeypatch.setattr(ex.IncrementalCandidateGenerator, 'opportunities_for', divergent)
    with pytest.raises(ValueError, match='OBSERVER_CHANGED_OPPORTUNITIES'):
        experiments.run(study)


def test_window_rationale_is_required(study):
    """§4.1 要求在冻结时声明**历史数据使用范围**。

    没有它，manifest 里只有两个日期 —— 读的人无法判断窗口是按共同覆盖范围选的还是随手定的。
    2026-09-20 实测：前一个 study 只覆盖 19 个交易日、产出 1 笔交易（诊断没有检验力），
    而 manifest 里看不出那是刻意的还是失误。
    """
    d = manifest.read(study)
    assert d['research_window_rationale']
    d['research_window_rationale'] = '   '
    assert 'RESEARCH_WINDOW_RATIONALE_MISSING' in manifest.audit_draft(d)
    d['research_window_rationale'] = '有内容即可'
    assert 'RESEARCH_WINDOW_RATIONALE_MISSING' not in manifest.audit_draft(d)


def test_replay_idempotence_and_fills(study):
    first = experiments.run(study)
    assert first['checks']['replay_equal']
    assert first['exits']['count'] > 0
    ledger = study.parent / 'variants/baseline/ledger.sqlite3'
    with sqlite3.connect(ledger) as con:
        before = list(con.iterdump())
    second = experiments.run(study)
    assert first == second
    with sqlite3.connect(ledger) as con:
        assert list(con.iterdump()) == before
    assert first['fees_micro'] > 0
    # §9.3 的结果判定只有把 challenger 与 baseline 比过之后才存在。只有基线时它是 None ——
    # 按"算不出来就是 None"的纪律，不挑一个最接近的令牌充数。
    assert first['verdict'] is None
    # 分期结论：**夹具数据太短，批处理管线产不出可对账的条目**，所以它是
    # `ENGINEERING_BLOCKED` 而不是 `INSUFFICIENT_EVIDENCE` —— 后者是研究结论，
    # 前者是"工程还没就绪，先去修"。两者共用一个标签正是要防的事（§15 P0）。
    assert first['checks']['baseline_parity']['status'] == 'NOT_EVALUATED'
    assert first['phase_conclusion'] == 'ENGINEERING_BLOCKED'
    assert first['verdict'] in (None, *experiments.VERDICT_TOKENS)


def test_parity_not_evaluated_is_recorded_not_silently_skipped(study):
    """批处理管线跑不出条目时，必须**如实记下 NOT_EVALUATED**，不能悄悄跳过。

    "没有这条 check" 与 "这条 check 没跑成" 在 `checks.json` 里长得一模一样 —— 除非
    把状态写出来。真实 study 上这条是 `VERIFIED`（有零分歧的对账记录）。
    """
    result = experiments.run(study)
    parity = result['checks']['baseline_parity']
    assert parity['status'] == 'NOT_EVALUATED'
    assert parity['reason'].startswith('BASELINE_PARITY_NO_')
    assert parity['note']
    assert result['checks']['baseline_entries']['status'] == 'NOT_EVALUATED'


def test_resume_after_committed_session(study, monkeypatch):
    original = experiments.ShadowStore.save_state
    calls = []
    def broken(self, *args):
        original(self, *args)
        calls.append(1)
        if len(calls) == 5:
            raise OSError('simulated interruption')
    monkeypatch.setattr(experiments.ShadowStore, 'save_state', broken)
    with pytest.raises(OSError):
        experiments.run(study)
    monkeypatch.setattr(experiments.ShadowStore, 'save_state', original)
    resumed = experiments.run(study)
    assert resumed == experiments.run(study)


def test_every_observation_carries_the_design_fields(study):
    """§5.3 的字段一个都不能少，且**轮次身份不是"观察当天"**。

    等待窗内逐日观察时，session 早已不是轮次日期。把 `candidate_round` 写成观察当天，
    同一候选就会在不同 session 上挂着不同的"轮次"，§5.1 的漏斗守恒无从谈起。
    而在轮次当日做的那些门（股票池/市场门/排名/质量）两者必然相等。
    """
    experiments.run(study)
    rows = json.loads((study.parent / 'funnel_observations.json').read_text(encoding='utf-8'))
    assert rows
    for r in rows:
        assert set(Funnel.REQUIRED) <= set(r), sorted(set(Funnel.REQUIRED) - set(r))
    rounds = {}
    for r in rows:
        rounds.setdefault(r['candidate_id'], set()).add(r['candidate_round'])
        if r['stage'] in ('universe', 'market', 'ranking', 'quality'):
            assert r['candidate_round'] == r['session']
    # 一个候选只能属于一个轮次 —— 「每天一次 WAITING 冒充多个独立候选」的反面
    assert all(len(v) == 1 for v in rounds.values())
    # 等待窗内的观察必须仍挂在轮次日期上（而不是它被观察的那天）
    assert any(r['candidate_round'] != r['session'] for r in rows)
    # §5.3 的两条一致性在**真实产出**上也必须成立（不只是 Funnel 的手工夹具）
    for r in rows:
        if r['result'] == 'not_evaluated':
            assert r['primary_reason'] == '', r
        if r['primary_reason']:
            assert r['all_reasons'][:1] == [r['primary_reason']], r


@pytest.fixture
def rich_study(tmp_path):
    """一个**会走到所有漏斗分支**的研究夹具。

    上面那个 `study` 夹具（单只证券、价格单调上涨、top_n=5）实测只产出 22 个 pass + 1 条
    account_execution reject —— `not_evaluated`、多条件、`unknown`、`EXPIRED` 一次都没走到。
    **这正是 `all_reasons` 恒 ≤1 那个缺陷能活下来的原因**：不是没人写断言，是夹具从没走到
    那条路径。本夹具的路径设计：

    · 市场门尾段关闭 ⇒ `ranking`/`quality` 未执行、`market` 拒绝；
    · 证券 A 先涨后跌 ⇒ 周线门转下，`daily` 未执行、`weekly` 拒绝；
    · 证券 B 单调上涨 ⇒ 真的成交（account_execution pass）；
    · top_n=1 ⇒ B 落选，`BELOW_TOP_N` 与 `MARKET_GATE_CLOSED` 同时成立 ⇒ 多条件。
    """
    days = pd.bdate_range('2024-01-01', '2025-04-01')
    n = len(days)
    market = np.array([100 + i * .4 if i < 250 else 200 - (i - 250) * .6 for i in range(n)], float)
    series = {'SEC-US-A': market,                                              # 先涨后跌
              'SEC-US-B': np.array([100 + i * .25 for i in range(n)], float)}  # 单调涨
    frames = [pd.DataFrame(dict(
        security_id=sid, session=days, raw_open=c, raw_high=c * 1.001, raw_low=c * .999,
        raw_close=c, volume=1000000, asof_atr=2., scale_to_next=1.)) for sid, c in series.items()]
    pd.concat(frames).to_csv(tmp_path / 'rich_prices.csv', index=False)
    pd.DataFrame(dict(security_id='SEC-US-QQQ', session=days, close=market)).to_csv(
        tmp_path / 'rich_market.csv', index=False)
    pd.DataFrame([dict(security_id=s, quality_status='verified', from_session=str(days[0].date()),
                       to_session=str(days[-1].date())) for s in series]).to_csv(
        tmp_path / 'rich_quality.csv', index=False)
    pd.DataFrame(columns=['security_id', 'action_type', 'ex_date', 'ratio', 'cash_amount']).to_csv(
        tmp_path / 'rich_actions.csv', index=False)
    d = manifest.draft('rich-study', _write_baseline(tmp_path, 'rich_base.json', top_n=1,
                                                     start_session='2024-11-01'),
                       {k: [tmp_path / f'rich_{k}.csv'] for k in ['prices', 'market', 'quality', 'actions']},
                       '2024-11-01', '2025-03-31', window_rationale='夹具：覆盖漏斗全部分支')
    return manifest.freeze(d, tmp_path / 'rich_output')


def test_rich_fixture_reaches_every_funnel_branch(rich_study):
    """先证明夹具**真的**覆盖了那些分支 —— 否则后面的断言全是空转（§14 的精神）。"""
    experiments.run(rich_study)
    rows = json.loads((rich_study.parent / 'funnel_observations.json').read_text(encoding='utf-8'))
    assert {r['result'] for r in rows} == {'pass', 'reject', 'unknown', 'not_evaluated'}
    assert {r['candidate_state'] for r in rows if r.get('candidate_state')} >= {
        'WAITING', 'READY', 'RULE_REJECTED', 'DATA_BLOCKED', 'EXPIRED'}
    assert any(len(r['all_reasons']) > 1 for r in rows)      # 多条件真的出现过
    assert any(r['stage'] == 'account_execution' for r in rows)


def test_unevaluated_reason_is_caught_end_to_end(rich_study, monkeypatch):
    """§14「每个关键回归应证明**在注入对应缺陷时失败**」。

    与 `_row` 的手工夹具不同，这条把生产者退回**旧行为**（`not_evaluated` 的行照样带失败
    原因：排名阶段在市场门已关时、日线阶段在周线没过时），走的是真实候选流 —— 证明防线
    接在真实数据路径上，而不是只在一个 helper 里。
    """
    import scripts.portfolio_shadow.candidate_adapter as ca

    experiments.run(rich_study)   # 干净跑一次：确认夹具确实产出 not_evaluated 的行
    rows = json.loads((rich_study.parent / 'funnel_observations.json').read_text(encoding='utf-8'))
    assert any(r['result'] == 'not_evaluated' for r in rows), '夹具没造出未执行的行，这条测不到东西'

    original = ca.IncrementalCandidateGenerator._observe

    def regressed(self, cid, sid, session, stage, result, reason='', *, reasons=(), **kw):
        if result == 'not_evaluated':
            reason, reasons = 'BELOW_TOP_N', ['BELOW_TOP_N']
        return original(self, cid, sid, session, stage, result, reason, reasons=reasons, **kw)

    monkeypatch.setattr(ca.IncrementalCandidateGenerator, '_observe', regressed)
    with pytest.raises(ValueError, match='GATE_OBSERVATION_REASON_ON_UNEVALUATED'):
        experiments.run(rich_study)


def _parity_fixture():
    """一只能跑通两引擎对账的最小 fixture：10 个 session、1 笔 entry、持有 5 个 session。"""
    days = pd.bdate_range('2025-01-02', periods=10)
    close = np.array([100., 101., 102., 103., 104., 105., 106., 107., 108., 109.])
    prices = pd.DataFrame(dict(security_id='SEC-US-A', session=days,
                               raw_open=close, raw_high=close + .5, raw_low=close - .5,
                               raw_close=close))
    i, horizon = 2, 5
    matrix = pd.DataFrame([dict(
        entry_id='e1', security_id='SEC-US-A', entry_session=days[i],
        entry_price=float(close[i]), initial_stop=float(close[i] - 10.), rank=0,
        holding_sessions=horizon, exit_method='H60', exit_session=days[i + horizon - 1],
        exit_price=float(close[i + horizon - 1]), exit_reason='TIME_EXIT',
        exit_phase='CLOSE', portfolio_accepted=True)])
    return matrix, prices, pd.DataFrame(), horizon


def test_baseline_parity_verifies_when_engines_agree(monkeypatch):
    """两引擎在同一条 entry 上必须逐日零分歧 —— 这是 §15 P0「同输入与旧基线一致」。"""
    from scripts.strategy_diagnostics import baseline_parity
    matrix, prices, actions, horizon = _parity_fixture()
    record = baseline_parity.compare(matrix, prices, actions, horizon=horizon,
                                     risk_policy={'single_position_risk_bp': 100},
                                     initial_cash_micro=100_000_000_000)
    assert record['n_diffs'] == 0
    # 历史引擎从**第一笔 entry 所在会话**起算（`evaluation_start`/`entry_session.min()`），
    # 故是 10 − 2 = 8 个会话，不是 10
    assert record['n_sessions'] == 8
    assert record['tolerance_usd'] == 0.0


def test_baseline_parity_catches_a_divergence(monkeypatch):
    """§14「每个关键回归应证明**在注入对应缺陷时失败**」。

    把历史引擎的定仓改掉一股 —— 对账必须报出来。没有这条，`n_diffs == 0` 可能只是因为
    这个检查从来看不见差异（本模块反复要防的形态）。
    """
    import scripts.medium_term.portfolio_engine as pe
    from scripts.strategy_diagnostics import baseline_parity
    matrix, prices, actions, horizon = _parity_fixture()
    original = pe.risk_sized_shares_micro
    monkeypatch.setattr(pe, 'risk_sized_shares_micro',
                        lambda *a, **k: original(*a, **k) + 1)
    with pytest.raises(ValueError, match='BASELINE_PARITY_DIVERGED'):
        baseline_parity.compare(matrix, prices, actions, horizon=horizon,
                                risk_policy={'single_position_risk_bp': 100},
                                initial_cash_micro=100_000_000_000)


def test_baseline_parity_rejects_a_unit_mixup():
    """单位搞错（差 1e6）必须**对不上**，而不是悄悄通过。

    实测踩过：`Manifest.initial_cash` 存的是整数微美元，而 `simulate_multi_asset_portfolio`
    收的是美元。混用会让试算账户以 1000 亿美元起步、影子账户仍是 10 万 ⇒
    `2631/2631` 个会话全部对不上、最大差 $4.58e11。故参数名带单位 `_micro`，且这里钉住
    "传错就报错"——否则下次有人把 1e6 写漏，检查会静默地比两个不同规模的账户。
    """
    from scripts.strategy_diagnostics import baseline_parity
    matrix, prices, actions, horizon = _parity_fixture()
    with pytest.raises(ValueError, match='BASELINE_PARITY_DIVERGED'):
        baseline_parity.compare(matrix, prices, actions, horizon=horizon,
                                risk_policy={'single_position_risk_bp': 100},
                                initial_cash_micro=100_000)      # 差 1e6 倍


def test_entry_reconciliation_declares_the_acceptance_difference():
    """两套接受机制不同是**预期**，但差集必须看得见（§5.1 不得把容量拒绝伪装成策略拒绝）。"""
    from scripts.strategy_diagnostics.baseline_parity import reconcile_entries
    matrix, _prices, _actions, _h = _parity_fixture()
    out = reconcile_entries({('SEC-US-A', '2025-01-06')}, matrix)
    assert out['batch_accepted'] == 1 and out['study_executed'] == 1
    assert out['in_both'] == 1 and out['n_only_study'] == 0
    assert '预期' in out['note']
    never = reconcile_entries(set(), matrix)
    assert never['n_only_batch'] == 1 and never['only_batch'] == [('SEC-US-A', '2025-01-06')]


def test_unsettled_cash_is_zero_without_t1_settlement():
    """`open_equity` 加上 `unsettled_cash` 必须是**可证明的空操作**（§15 P0 的前提）。

    它的证明就是这条：不启用 T+1 时 `unsettled_cash` 恒为 0（卖出款直接进现金），
    故既有历史基线一字不变。反过来若这里出现非零，我改动的那行就不再是空操作。
    """
    from scripts.medium_term.portfolio_engine import simulate_multi_asset_portfolio
    matrix, prices, actions, _h = _parity_fixture()
    out = simulate_multi_asset_portfolio(prices, matrix, initial_cash=100_000.,
                                        risk_fraction=.01, max_weight=.20,
                                        round_trip_cost=.002, actions=actions,
                                        allow_fractional=False, max_positions=5)
    assert (out.equity.unsettled_cash == 0).all()
    assert (out.equity.dividend_receivable == 0).all()


def test_freezing_from_a_frozen_copy_is_rejected(tmp_path):
    """从已冻结的 study 再冻一份会**静默改写证券身份**，必须前置拒绝。

    实测（2026-09-21）：把 009 冻结目录里的副本当输入，`freeze` 的 `{i}-` 前缀被叠加成
    `0-0-US_AAPL.csv.gz`，而 `inputs.load` 按**文件名**还原证券 id ⇒ 得到
    `0-SEC-US-AAPL`，与质量表一个都对不上 ⇒ 每个候选以 `SECURITY_NOT_VERIFIED` 被排除、
    **成交 0 笔**，而报告照常产出、没有任何报错。是"零分歧的对账跑不出来"才暴露的。
    """
    from scripts.strategy_diagnostics.manifest import _reject_already_prefixed
    prefixed = tmp_path / '0-US_AAPL.csv.gz'
    pd.DataFrame({'session': ['2025-01-02'], 'raw_close': [1.0]}).to_csv(prefixed, index=False)
    with pytest.raises(ValueError, match='FREEZE_SOURCE_ALREADY_PREFIXED'):
        _reject_already_prefixed('prices', prefixed)
    # 自带 security_id 列 ⇒ 文件名不承载身份，加前缀无害，放行
    explicit = tmp_path / '0-explicit.csv.gz'
    pd.DataFrame({'security_id': ['SEC-US-AAPL'], 'raw_close': [1.0]}).to_csv(
        explicit, index=False)
    _reject_already_prefixed('prices', explicit)
    # 未加前缀的原件放行；非 prices 类不看文件名
    plain = tmp_path / 'US_AAPL.csv.gz'
    pd.DataFrame({'raw_close': [1.0]}).to_csv(plain, index=False)
    _reject_already_prefixed('prices', plain)
    _reject_already_prefixed('quality', prefixed)


def test_tampered_input_fails(study):
    d = manifest.read(study)
    path = study.parent / d['input_index']['prices'][0]['path']
    path.write_text(path.read_text() + '\n')
    with pytest.raises(ValueError, match='STUDY_INPUT_CHANGED'):
        experiments.run(study)


def test_code_change_fails(study, monkeypatch):
    monkeypatch.setattr(manifest, 'code_hashes', lambda: {'changed':'hash'})
    with pytest.raises(ValueError, match='STUDY_CODE_CHANGED'):
        experiments.run(study)


def _row(**kw):
    base = dict(candidate_id='a-2025-01', security_id='a', candidate_round='2025-01-01',
                session='2025-01-01', stage='weekly', result='unknown', rule_version='1',
                candidate_state='WAITING', primary_reason='', all_reasons=[],
                observed_at='2025-01-01T21:00:00+00:00',
                generated_at='2025-01-01T21:00:00+00:00')
    base.update(kw)
    return base


def test_unevaluated_stage_never_carries_a_failure_reason(tmp_path):
    """§5.3「未执行的后续条件标 not_evaluated，**不能当失败**」。

    原实现里 `BELOW_TOP_N` 之类的 reason 与 `result` 是**分开算**的：排名阶段在市场门已关时
    标 `not_evaluated`（正确），却照样把 `BELOW_TOP_N` 写进 `primary_reason`（错误）——
    于是"这个阶段没轮到它"在账本里长成了"这个阶段否掉了它"。可计算的条件仍要留在
    `all_reasons` 里作影子诊断，但不得充当主原因。
    """
    sink = Funnel('test', tmp_path / 'g.sqlite3')
    with pytest.raises(ValueError, match='GATE_OBSERVATION_REASON_ON_UNEVALUATED'):
        sink(_row(result='not_evaluated', primary_reason='BELOW_TOP_N',
                  all_reasons=['BELOW_TOP_N']))
    # 影子条件本身是允许的：未执行 + 空主原因 + all_reasons 留着
    sink(_row(result='not_evaluated', primary_reason='',
              all_reasons=['WEEKLY_NOT_CONFIRMED', 'NO_ENTRY_SIGNAL']))
    assert sink.observations()[0]['all_reasons'] == ['WEEKLY_NOT_CONFIRMED', 'NO_ENTRY_SIGNAL']


def test_primary_reason_must_head_all_reasons(tmp_path):
    """主原因是"固定优先级选出的首项"（§5.3），不是另一个字段。

    两者分开写就会各自漂移：主原因说 A、条件序列把 B 排在最前 —— 谁都看不出来。
    """
    sink = Funnel('test', tmp_path / 'g.sqlite3')
    with pytest.raises(ValueError, match='GATE_OBSERVATION_PRIMARY_NOT_FIRST'):
        sink(_row(result='reject', primary_reason='MARKET_GATE_CLOSED',
                  all_reasons=['BELOW_TOP_N', 'MARKET_GATE_CLOSED']))
    # 多条件时**全部**保留，主原因只是其中排序最前的那个
    sink(_row(result='reject', primary_reason='MARKET_GATE_CLOSED',
              all_reasons=['MARKET_GATE_CLOSED', 'BELOW_TOP_N', 'QUALITY_UNVERIFIED']))
    assert len(sink.observations()[0]['all_reasons']) == 3


def test_observation_dedup_and_unknown_denominator(tmp_path):
    sink = Funnel('test', tmp_path / 'gates.sqlite3')
    row = _row()
    sink(row); sink(row)
    sink(dict(row, session='2025-01-02', result='pass'))
    summary = sink.summary()
    assert summary['candidate_count'] == 1
    assert summary['stages']['weekly']['conditional_pass_rate'] == 1
    with pytest.raises(ValueError, match='CONFLICT'):
        sink(dict(row, result='reject'))
    with pytest.raises(ValueError, match='CONFLICT'):
        Funnel('test', tmp_path / 'gates.sqlite3')(dict(row, result='pass'))


def test_audit_fields_are_required_and_not_self_generated(tmp_path):
    """§5.3 的墙钟审计字段必须由**生产方**给出，sink 不得自己取当前时间。

    一个自己调 `now()` 的字段在历史重建里等于伪造（把"今天生成"写成"当时观察到的"），
    而且会让每次重跑产出不同的行。缺字段直接拒绝，不给默认值。
    """
    sink = Funnel('test', tmp_path / 'gates.sqlite3')
    for field in ('observed_at', 'generated_at', 'candidate_round'):
        with pytest.raises(ValueError, match=f'GATE_OBSERVATION_MISSING_FIELDS'):
            sink(_row(**{field: ''}))


def test_rerun_keeps_the_first_audit_stamp(tmp_path):
    """重跑时审计戳以**首次写入**为准，业务字段仍然冲突保护（§5.3）。

    否则同一次确定性重放会因为墙钟不同而产出不同的行 —— 那正是 §5.3 禁止的
    「墙钟审计字段产生新的业务身份」。
    """
    path = tmp_path / 'gates.sqlite3'
    first = Funnel('test', path)
    first(_row())
    stamped = first.observations()[0]
    # 业务内容完全相同、只有墙钟审计戳不同 ⇒ 不是冲突，且保留首次的值
    later = Funnel('test', path)
    later(_row(observed_at='2026-09-20T10:00:00+00:00',
               generated_at='2026-09-20T10:00:00+00:00'))
    assert later.observations() == [stamped]
    assert later.observations()[0]['observed_at'] == '2025-01-01T21:00:00+00:00'
    # 业务字段变了才是冲突，且审计戳不改变这个判据
    with pytest.raises(ValueError, match='CONFLICT'):
        Funnel('test', path)(_row(result='reject',
                                  observed_at='2026-09-20T10:00:00+00:00',
                                  generated_at='2026-09-20T10:00:00+00:00'))


def test_actions_are_scoped_to_the_traded_universe():
    """无关证券的行动不得阻断整条研究链，但被交易的证券仍严格校验。

    2026-09-20 实测：P1 那次运行被 `SHADOW_REQUIRES_INTEGER_ACTION_RATIO` 挡死
    （`trial_registry.jsonl` 里留着 `run_failed`）。而窗口内 11 条拆股里只有 2 条非整数，
    **全属 HON** —— 而 HON 不在该 study 的 13 只价格面板内。校验整份行动表而不是被交易的
    证券，等于让一个无关证券的数据问题阻断全部诊断。
    """
    from scripts.strategy_diagnostics.experiments import shadow_actions
    actions = pd.DataFrame([
        dict(security_id='SEC-US-A', ex_date='2025-02-03', action_type='split', ratio=2.0,
             cash_amount=0.0, pay_date=None),
        dict(security_id='SEC-US-HON', ex_date='2025-10-30', action_type='split', ratio=1.061385,
             cash_amount=0.0, pay_date=None),
    ])
    kept, dropped = shadow_actions(actions, universe={'SEC-US-A'})
    assert [k['security_id'] for k in kept] == ['SEC-US-A']
    assert kept[0]['ratio'] == 2
    assert [d['security_id'] for d in dropped] == ['SEC-US-HON']
    assert dropped[0]['reason'] == 'OUTSIDE_TRADED_UNIVERSE'
    # 过滤**不是放宽**：一旦它进了被交易集合，非整数比照样拒绝
    with pytest.raises(ValueError, match='SHADOW_REQUIRES_INTEGER_ACTION_RATIO'):
        shadow_actions(actions, universe={'SEC-US-A', 'SEC-US-HON'})
    # 不给 universe ⇒ 全表校验（旧行为，供不关心证券域的调用方使用）
    with pytest.raises(ValueError, match='SHADOW_REQUIRES_INTEGER_ACTION_RATIO'):
        shadow_actions(actions)


def test_actions_outside_the_traded_sessions_are_excluded():
    """只按证券过滤不够 —— 实盘实测被十二年前的拆股挡死过。

    2026-09-20 第二次实测：`SHADOW_REQUIRES_INTEGER_ACTION_RATIO:SEC-US-GOOGL:2014-04-03:1.9981`
    —— GOOGL **在**宇宙里，但那条拆股在十二年前，对 2026 年的账户毫无影响。引擎按 session
    逐日查 `acts[date]`，区间外的行动永远查不到，过滤掉它们不改变任何会计结果。
    """
    from scripts.strategy_diagnostics.experiments import shadow_actions
    actions = pd.DataFrame([
        dict(security_id='SEC-US-GOOGL', ex_date='2014-04-03', action_type='split',
             ratio=1.9981, cash_amount=0.0),
        dict(security_id='SEC-US-GOOGL', ex_date='2026-09-04', action_type='cash_dividend',
             ratio=0.0, cash_amount=0.22, effective_at='2026-09-14'),
    ])
    kept, dropped = shadow_actions(actions, universe={'SEC-US-GOOGL'},
                                   session_range=('2026-08-17', '2026-09-11'))
    assert [k['ex_date'] for k in kept] == ['2026-09-04']          # 旧的拆股被排除
    assert kept[0]['pay_date'] == '2026-09-14'
    assert dropped == [{'security_id': 'SEC-US-GOOGL', 'ex_date': '2014-04-03',
                        'reason': 'OUTSIDE_TRADED_SESSIONS'}]
    # 区间内的非整数拆股**照样拒绝** —— 收窄范围不是放宽守卫
    inside = pd.DataFrame([dict(security_id='SEC-US-GOOGL', ex_date='2026-08-20',
                                action_type='split', ratio=1.5, cash_amount=0.0)])
    with pytest.raises(ValueError, match='SHADOW_REQUIRES_INTEGER_ACTION_RATIO'):
        shadow_actions(inside, universe={'SEC-US-GOOGL'},
                       session_range=('2026-08-17', '2026-09-11'))


def test_dividend_pay_date_falls_back_to_effective_at():
    """派发日必须能从 `effective_at` 取到；真的没有就留 None，**不猜**。

    `paper_engine.step` 拿 `pay_date` 当 `dividend_receivable` 的字典键，缺了它
    `pop(session)` 永远匹配不上 ⇒ 分红永远转不成可用现金。而富途直取的行动表把
    `dividend_payable_date` 写在 `effective_at` 里（`ACTION_COLUMNS` 没有 `pay_date` 列），
    原先只读 `pay_date` ⇒ 恒为 None ⇒ 持有的证券一分红，账户只能靠
    `HELD_DIVIDEND_PAY_DATE_MISSING` 中止。
    """
    from scripts.strategy_diagnostics.experiments import shadow_actions
    actions = pd.DataFrame([
        dict(security_id='SEC-US-A', ex_date='2026-09-04', effective_at='2026-09-14',
             action_type='cash_dividend', ratio=0.0, cash_amount=0.22),
        dict(security_id='SEC-US-B', ex_date='2026-09-05', effective_at=None,
             action_type='cash_dividend', ratio=0.0, cash_amount=0.1),
    ])
    kept, _ = shadow_actions(actions)
    assert kept[0]['pay_date'] == '2026-09-14'
    assert kept[0]['cash_amount_micro'] == 220000
    # 两者都缺 ⇒ 如实留 None，让守卫中止，而不是编一个日期把分红记到错会话上
    assert kept[1]['pay_date'] is None


def test_followup_adjustment_and_missing_session():
    days = pd.bdate_range('2025-01-01', periods=7)
    p = pd.DataFrame(dict(security_id='a', session=days, raw_close=[100,50,51,52,53,54,55]))
    actions = pd.DataFrame([dict(security_id='a',ex_date=days[1],action_type='split',ratio=2)])
    t = dict(security_id='a',exit_session=str(days[0].date()),exit_price=100,entry_price_exit_basis=100)
    out = followup(t,p,actions,days,days[-1],horizons=(5,20))
    assert out['5']['total_return'] == pytest.approx(.08)
    assert out['20']['status'] == 'pending_or_missing'
    p = p.drop(index=2)
    assert followup(t,p,actions,days,days[-1],horizons=(5,))['5']['total_return'] is None
