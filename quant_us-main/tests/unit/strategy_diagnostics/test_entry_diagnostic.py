import pandas as pd
from scripts.strategy_diagnostics.entry_diagnostic import verdict,block_interval,distribution


def test_risk_gate_cannot_be_overridden_by_positive_mean():
    m={'delta_mean':1,'gates':dict(sample=True,experimental_sample=True,interval=True,
        stop_rate=True,tail=True,worst5=True,mae=True,concentration=True,experimental_concentration=True,stress=True)}
    assert verdict(m)=='SUPPORTED'
    for key in ['stop_rate','tail','worst5','mae']:
        m['gates'][key]=False
        assert verdict(m)=='RISK_REJECTED'
        m['gates'][key]=True
    m['gates']['interval']=False
    assert verdict(m)=='INCONCLUSIVE'
    m['delta_mean']=0
    assert verdict(m)=='NO_IMPROVEMENT'


def test_time_blocks_keep_same_round_correlated_and_seed_stable():
    cal=pd.bdate_range('2025-01-01',periods=80)
    dates=[str(cal[i].date()) for i in [1,1,25,25,45,45]]
    values=[1,-1,2,-2,3,-3]
    assert block_interval(values,dates,cal,20)==[0,0]
    assert block_interval([1]*6,dates,cal,20)==[1,1]


def test_tail_is_strictly_below_minus_two():
    rows=[{'net_r':v,'mae_r':1,'mae_pct':.08,'reason':'STOP'} for v in [-2,-2.001,1]]
    assert distribution(rows)['below_minus_2r']==1


def test_next_open_and_stop_clipped_mae_with_actual_engine():
    from scripts.strategy_diagnostics.entry_diagnostic import simulate
    from scripts.portfolio_shadow.schema import Manifest,to_micro
    m=Manifest(experiment_id='test',status='FROZEN',parent_strategy_id='B3',parent_version='1',
        parent_code_hash='test',universe_id='u',universe_hash='h',account_scopes=('SHADOW:test:R',),
        initial_cash=to_micro(100000),risk_policy={'single_position_risk_bp':100,'max_weight_bp':2000,'max_positions':5},
        execution_policy={'entry_rule':'b3','exit_policy_id':'H60','horizon':60},
        llm_policy={'overlay':'fixed_pass'},calendar_version='v')
    dates=pd.bdate_range('2025-01-01',periods=65)
    p=pd.DataFrame({'raw_open':100.,'raw_high':101.,'raw_low':99.,'raw_close':100.,
                    'asof_atr':2.,'scale_to_next':1.},index=dates)
    p.loc[dates[1],'raw_low']=50.
    r=simulate('A','a',str(dates[0].date()),{'A':p},dates,{},m,10)
    assert r['entry_session']==str(dates[1].date())
    assert r['holding_sessions']==1
    assert r['reason']=='STOP'
    assert r['mae_r']==1
    assert r['shares']==125
    assert r['net_r'] < -1


def test_fill_reconciliation_is_bidirectional():
    """对不上的成交**必须**由预登记的截止日解释，否则失败。

    原先只写 `if (sid,day) in a_index:` —— 对得上才比、对不上静默跳过，于是"重建漏掉
    一笔真成交"会静默通过。那正是这个控制（"账户成交复现"）要防的事。
    """
    import pytest
    from scripts.strategy_diagnostics.entry_diagnostic import reconcile_fills
    a_index={('SEC-A','2025-01-06'):{'entry_price_micro':100,'stop_micro':92}}
    rounds={('SEC-A','2025-01-06'):'2024-12-31',('SEC-B','2025-02-03'):'2025-01-31'}
    out=reconcile_fills([('SEC-A','2025-01-06',100,10,92),('SEC-B','2025-02-03',50,10,46)],
                        a_index,rounds,'2025-01-15')
    assert out['matched']==1 and out['excused_by_cutoff']==1
    assert out['excused']==[['SEC-B','2025-02-03','2025-01-31']]
    # 轮次在截止内却对不上 ⇒ **真缺口**，必须报错而不是跳过
    with pytest.raises(ValueError,match='BASELINE_FILL_NOT_RECONSTRUCTED'):
        reconcile_fills([('SEC-C','2024-12-02',9,1,8)],a_index,
                        {('SEC-C','2024-12-02'):'2024-11-29'},'2025-01-15')
    # 查不到轮次 ⇒ 不能当作"已解释"
    with pytest.raises(ValueError,match='BASELINE_FILL_UNKNOWN_ROUND'):
        reconcile_fills([('SEC-D','2025-03-03',9,1,8)],a_index,{},'2025-01-15')
    # 对得上但价格/止损不同 ⇒ 失败
    with pytest.raises(ValueError,match='BASELINE_ENTRY_OR_STOP_MISMATCH'):
        reconcile_fills([('SEC-A','2025-01-06',101,10,92)],a_index,rounds,'2025-01-15')


def test_sample_gate_uses_the_evaluated_span_not_the_whole_calendar():
    """`≥60 交易日` 必须落在**被评价的那一段**上。

    原先用 `len(calendar)`（研究窗口 2932 个 session）⇒ 门槛恒真、形同虚设。
    """
    from scripts.strategy_diagnostics.entry_diagnostic import candidate_span
    cal=pd.bdate_range('2025-01-01',periods=200)
    assert candidate_span(cal,[str(cal[0].date()),str(cal[100].date())])==101
    assert candidate_span(cal,[str(cal[0].date()),str(cal[10].date())])==11


def test_block_bootstrap_is_centered_and_partitions_by_absolute_position():
    """桶按**参考日历的绝对位置**划分（不是按"第几个候选"），且重采样以样本均值为中心。

    这是要防的实际错误：若改成"每 20 个候选一块"，同一轮内的相关结构会被打散、区间失真。
    可验的结构性判据：**块内两个候选** ⇒ 只有一个块可抽 ⇒ 区间坍缩成一个点；
    **跨块两个候选** ⇒ 区间变宽。

    另：从全部 N 块（含空块）有放回抽 N 个时，E[样本量] = 原始样本数，对任意块长分布成立 ——
    真实数据实测 136 个轮次 / 143 块（7 个空块）下 bootstrap 平均样本量 135.9；
    改成"只抽非空块"反而掉到 128.9。**不要"修"这一点。**
    """
    import numpy as np
    cal=pd.bdate_range('2025-01-01',periods=300)
    same_block=[str(cal[0].date()),str(cal[5].date())]          # 都落在第 0 块
    across=[str(cal[0].date()),str(cal[20].date())]             # 第 0 块与第 1 块
    assert block_interval([1.,3.],same_block,cal,20)==[2.,2.]   # 唯一块 ⇒ 点
    lo,hi=block_interval([1.,3.],across,cal,20)
    assert hi-lo>0                                              # 两块 ⇒ 区间非退化
    assert lo<=2.<=hi                                           # 并以样本均值为中心
    assert block_interval([1.,1.],across,cal,20)==[1.,1.]
