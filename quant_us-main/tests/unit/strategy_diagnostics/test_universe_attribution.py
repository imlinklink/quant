"""宇宙归因 runner 的 fail-closed 守卫。

端到端跑一次要 18 分钟，所以这里钉的是**守卫**本身 —— 它们是"结果可不可信"的第一道闸门：
登记被改过、登记已经看过结果、输出要覆盖已有产物，都必须**在计算之前**拒绝。
"""
import json
from pathlib import Path

import numpy as np
import pytest

from scripts.strategy_diagnostics import universe_attribution as ua


def test_output_exists_is_refused(tmp_path):
    """产物必须只能新建 —— 覆盖会让"跑之前冻的是什么"无从核对。"""
    out = tmp_path / 'result.json'
    out.write_text('{}')
    with pytest.raises(ValueError, match='OUTPUT_EXISTS'):
        ua.execute(out)


def test_changed_registration_is_refused(tmp_path, monkeypatch):
    """登记被改过（哈希与修订记录不符）⇒ 拒绝。

    这是"先登记再计算"的执行力：没有它，改完登记再跑，修订记录就变成摆设。
    """
    monkeypatch.setattr(ua, '_digest', lambda path: 'deadbeef')
    monkeypatch.setattr(ua, 'read', lambda path: {
        'returns_examined': False, 'original_registration_sha256': 'something-else'})
    with pytest.raises(ValueError, match='REGISTRATION_CHANGED'):
        ua.execute(tmp_path / 'r.json')


def test_already_examined_registration_is_refused(tmp_path, monkeypatch):
    """登记自称已经看过结果 ⇒ 拒绝 —— 否则"先锁定门槛"的前提不成立。"""
    monkeypatch.setattr(ua, '_digest', lambda path: 'same')
    monkeypatch.setattr(ua, 'read', lambda path: {
        'returns_examined': True, 'original_registration_sha256': 'same'})
    with pytest.raises(ValueError, match='REGISTRATION_NOT_BLIND'):
        ua.execute(tmp_path / 'r.json')


def test_plain_converts_numpy_scalars_for_json():
    """numpy 标量必须转成原生类型。

    实测踩过：`manifest.write_json` 用 `allow_nan=False` 且不认 numpy，
    一个 `numpy.int64` 就让 18 分钟的计算在最后一行白跑。
    """
    out = ua._plain({'i': np.int64(3), 'f': np.float64(1.5), 'b': np.bool_(True),
                     'nan': np.float64('nan'), 'inf': np.float64('inf'),
                     'nested': [np.int32(7), {'k': np.int64(9)}], 's': 'x'})
    assert out['i'] == 3 and isinstance(out['i'], int)
    assert out['f'] == 1.5 and isinstance(out['f'], float)
    assert out['b'] is True
    assert out['nan'] is None and out['inf'] is None      # 非有限 → None，不写成 NaN
    assert out['nested'] == [7, {'k': 9}]
    json.dumps(out, allow_nan=False)                       # 能过就会过 write_json 那一关


def test_registration_files_are_present_and_consistent():
    """登记与修订必须都在，且修订记录的哈希与登记文件**实际**哈希一致。"""
    assert ua.REGISTRATION.exists() and ua.AMENDMENT.exists()
    amendment = json.loads(ua.AMENDMENT.read_text(encoding='utf-8'))
    assert ua._digest(ua.REGISTRATION) == amendment['original_registration_sha256']
    assert amendment['returns_examined'] is False
    reg = json.loads(ua.REGISTRATION.read_text(encoding='utf-8'))
    assert reg['returns_examined'] is False
    # 三臂的门与池必须在登记里写死
    assert reg['arms']['B']['pit_gate'] is True
    assert reg['arms']['C']['pit_gate'] is False
