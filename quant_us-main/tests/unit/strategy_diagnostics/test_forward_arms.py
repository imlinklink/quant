"""三臂前向 runner 的守卫（登记 `B3-FORWARD-20260921`）。

端到端重放要跑真实面板，故这里钉**守卫与构造**：它们是"这份前向记录可不可信"的第一道闸门。
"""
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.strategy_diagnostics import forward_arms as fa


def test_arm_universes_differ_only_by_the_pit_gate():
    """三臂的候选池：A = 13 只硬编码；B/C = 同一份 32 只（B 多一道门）。"""
    names = fa.arm_names()
    assert len(names['A']) == 13
    assert len(names['B']) == len(names['C']) == 32
    assert set(names['B']) == set(names['C'])
    # A 必须是 B 的**严格子集** —— 归因才读得干净（差异全来自"池子里放了谁"）
    assert set(names['A']) < set(names['B'])
    assert fa.ARMS == ('A', 'B', 'C')


def test_start_is_refused_when_registration_is_not_blind(tmp_path, monkeypatch):
    """登记自称已看过结果 ⇒ 拒绝 —— 否则"先登记再观察"的前提不成立。"""
    monkeypatch.setattr(fa, 'registration', lambda: (_ for _ in ()).throw(
        ValueError('REGISTRATION_NOT_BLIND')))
    with pytest.raises(ValueError, match='REGISTRATION_NOT_BLIND'):
        fa.start(tmp_path / 'fwd', start_session='2026-08-25')


def test_start_can_only_run_once(tmp_path, monkeypatch):
    """三臂必须**同日**起步；任一臂账本已存在即拒绝（不允许中途改起点）。"""
    monkeypatch.setattr(fa, 'registration', lambda: {'returns_examined': False})
    first = fa.start(tmp_path / 'fwd', start_session='2026-08-25')
    assert first['start_session'] == '2026-08-25'
    assert {a: v['universe_size'] for a, v in first['arms'].items()} == {'A': 13, 'B': 32, 'C': 32}
    assert first['arms']['B']['pit_gate'] is True
    assert first['arms']['A']['pit_gate'] is False
    assert first['registration_sha256']
    with pytest.raises(ValueError, match='ARM_ALREADY_STARTED'):
        fa.start(tmp_path / 'fwd', start_session='2026-08-25')


def test_arm_manifest_rebuilds_to_the_frozen_hash(tmp_path, monkeypatch):
    """重建出的臂 manifest 必须与冻结记录**同哈希**。

    这条守着 store/loader 的单位不对称：`save_experiment` 落库的是 `_asdict(manifest)`
    （`initial_cash` 是**微美元**），而 `manifest_from_dict` 按**美元**解析 —— 从账本读回
    会得到 1e6 倍的初始资金（实测踩过：权益显示成 1e17）。所以 runner 走**确定性重建**，
    并用这个哈希守卫保证重建结果与冻结一致。
    """
    monkeypatch.setattr(fa, 'registration', lambda: {'returns_examined': False})
    fa.start(tmp_path / 'fwd', start_session='2026-08-25')
    from scripts.portfolio_shadow.store import ShadowStore
    for arm in fa.ARMS:
        store = ShadowStore(tmp_path / 'fwd' / arm / fa.LEDGER_NAME, fa.ARM_IDS[arm])
        rebuilt = fa.arm_manifest(arm, '2026-08-25')
        assert rebuilt.manifest_hash() == store.frozen_manifest_hash()
        # 反证这条守卫守着什么：从账本读回会得到 1e6 倍的初始资金
        from scripts.portfolio_shadow.cli import manifest_from_dict
        round_tripped = manifest_from_dict(store.get_experiment())
        assert round_tripped.initial_cash == rebuilt.initial_cash * 1_000_000


def test_run_day_refuses_a_target_before_the_start(tmp_path, monkeypatch):
    """观察起点写死在 `arms.json` 里；目标日早于起点即拒绝（不能倒着补数据）。"""
    monkeypatch.setattr(fa, 'registration', lambda: {'returns_examined': False})
    fa.start(tmp_path / 'fwd', start_session='2026-08-25')
    with pytest.raises(ValueError, match='TARGET_BEFORE_START'):
        fa.run_day(tmp_path / 'fwd', '2026-08-20')


def test_arms_json_records_the_provenance(tmp_path, monkeypatch):
    """`arms.json` 必须记下登记与策略参数来源的哈希 —— 否则"按哪把尺子跑的"无从核对。"""
    monkeypatch.setattr(fa, 'registration', lambda: {'returns_examined': False})
    fa.start(tmp_path / 'fwd', start_session='2026-08-25')
    meta = json.loads((tmp_path / 'fwd' / 'arms.json').read_text(encoding='utf-8'))
    assert meta['registration_sha256'] == fa.file_hash(fa.FORWARD_REGISTRATION)
    assert meta['baseline_manifest_sha256'] == fa.file_hash(fa.BASELINE_MANIFEST)
    assert meta['start_session'] == '2026-08-25'


def test_run_day_refuses_after_the_frozen_code_changes(tmp_path, monkeypatch):
    """观察期内改了冻结集里的任何一件 ⇒ 拒绝继续。

    登记写明"观察期内不得改规则/参数/prompt/成本/股票池；任何一处变更 ⇒ 该臂的前向记录作废"。
    这条守卫是那句话的执行力：没有它，改了尺子照样记，而已记录的几天与新记的几天不可比。

    **不收数据面板**：它们是增长型的，冻进来第二天就会被自己的校验拒（与 M1 manifest 里
    "`data_hashes` 不能装增长型数据"是同一条教训）。
    """
    monkeypatch.setattr(fa, 'registration', lambda: {'returns_examined': False})
    fa.start(tmp_path / 'fwd', start_session='2026-08-25')
    meta_path = tmp_path / 'fwd' / 'arms.json'
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    assert 'data/survivor_sample_audit/asof_panels/US_AAPL.csv.gz' not in meta['frozen_code']
    meta['frozen_code']['scripts/portfolio_shadow/paper_engine.py'] = 'deadbeef'
    meta_path.write_text(json.dumps(meta), encoding='utf-8')
    with pytest.raises(ValueError, match='OBSERVATION_CODE_CHANGED'):
        fa.run_day(tmp_path / 'fwd', '2026-08-26')
