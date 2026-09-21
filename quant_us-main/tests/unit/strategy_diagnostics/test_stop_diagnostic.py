"""H3 诊断（`stop_diagnostic`）的判定与门槛。

**阈值来自登记文件**（`docs/preregistrations/EXIT-STOP-20260921.json`），故这里钉的是
"判定确实按登记的门槛走"：把门槛调到刚好通过/刚好不过，看令牌是否随之翻转。
"""
import json
from pathlib import Path

import pytest

from scripts.strategy_diagnostics.stop_diagnostic import (REGISTRATION, registration_digest,
                                                         step1_verdict)

ROOT = Path(__file__).resolve().parents[3]


def _summary(*, delta=10.0, top1=0.3, loo=True, survivors_ok=True, leaked=()):
    return {
        'delta_r_sum': delta,
        'concentration': {'top1_share_of_delta': top1, 'leave_one_out_still_positive': loo},
        'by_security_ordered': {'top2_share': 0.6, 'top5_share': 0.9},
        'control_survivors_arms_identical': survivors_ok,
        'no_stop_leaked_a_stop_exit': list(leaked),
    }


def test_registration_is_committed_and_parsable():
    """登记必须是**先于结果**存在的合法 JSON —— 它同时是判定的阈值来源。"""
    assert REGISTRATION.exists(), '预登记文件缺失：登记先于计算是本诊断的前提'
    data = json.loads(REGISTRATION.read_text(encoding='utf-8'))
    assert data['registration_id'] == 'EXIT-STOP-20260921'
    # 阈值必须写在登记里，不能只活在代码里
    assert '单一证券贡献 ≤ 50%' in data['decision_rules']['step1_supported'][1]
    assert len(registration_digest()) == 64


def test_verdict_flips_on_the_registered_single_security_threshold():
    """门槛是"单一证券 ≤ 50%"：49.9% 通过、50.1% 不通过 —— 边界两侧都钉住。"""
    assert step1_verdict(_summary(top1=0.499))['token'] == 'STEP1_SUPPORTED'
    out = step1_verdict(_summary(top1=0.501))
    assert out['token'] == 'CONCENTRATED'
    assert out['checks']['no_single_security_over_50pct'] is False
    # 恰好 50% 仍算通过（登记写的是"≤ 50%"）
    assert step1_verdict(_summary(top1=0.50))['token'] == 'STEP1_SUPPORTED'


def test_verdict_reports_leave_one_out_and_negative_delta():
    assert step1_verdict(_summary(loo=False))['token'] == 'CONCENTRATED'
    assert step1_verdict(_summary(delta=0.0))['token'] == 'NO_IMPROVEMENT'
    assert step1_verdict(_summary(delta=-1.0))['token'] == 'NO_IMPROVEMENT'


def test_failed_control_blocks_the_verdict_entirely():
    """控制项不过 ⇒ 实现有误，先修（登记的停止条件），不能出研究结论。"""
    assert step1_verdict(_summary(survivors_ok=False))['token'] == 'ENGINEERING_BLOCKED'
    assert step1_verdict(_summary(leaked=('SEC-US-MU',)))['token'] == 'ENGINEERING_BLOCKED'


def test_expected_real_study_verdict_is_concentrated():
    """010 的实测结论：增量 60.9% 来自 MU ⇒ `CONCENTRATED`。

    数字来自 `docs/preregistrations/EXIT-STOP-20260921.result.json`；这条防止
    "判定口径被改动后结论悄悄翻转"（改口径要改登记，而不是改代码）。
    """
    result = ROOT / 'docs/preregistrations/EXIT-STOP-20260921.result.json'
    if not result.exists():
        pytest.skip('结果文件不在（未跑过诊断）')
    data = json.loads(result.read_text(encoding='utf-8'))
    assert data['verdict']['token'] == 'CONCENTRATED'
    assert data['summary']['delta_r_sum'] > 0          # 增量确实为正，但集中
    assert data['summary']['concentration']['top1_share_of_delta'] > 0.5
    assert data['summary']['control_survivors_arms_identical'] is True
    assert data['summary']['no_stop_leaked_a_stop_exit'] == []
    # 左尾：关掉止损后最差单笔明显更差（机会级就已违反 step2 的尾部判据）
    tail = data['summary']['tail']
    assert tail['worst_net_r']['no_stop'] < tail['worst_net_r']['actual']
    assert tail['below_minus_2r']['actual'] == 0 < tail['below_minus_2r']['no_stop']
