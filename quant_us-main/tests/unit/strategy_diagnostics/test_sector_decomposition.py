"""行业分解 runner 的 fail-closed 守卫（登记 `SECTOR-SELECTION-SPLIT-20260921`）。

端到端跑一次约 8 分钟，所以这里钉的是**在计算之前就该拒绝的东西**，以及一个
**只在计算末尾才会爆的类别**——本文件第一次提交时 `simulate_multi_asset_portfolio`
没有导入：它不在模块顶部的引用里，于是 317s 的月末截面 + 64s 的择时都跑完之后才
`NameError`。纯读代码看不出来（函数体看着是对的），跑一次也要 8 分钟才发现。
故有 `test_module_has_no_undefined_globals`：**静态检查整个模块的全局引用**，
并用"删掉一行导入"当场证明这个检查本身有效。
"""
import builtins
import json
import symtable
from pathlib import Path

import pandas as pd
import pytest

from scripts.medium_term.stock_cross_section import generate_monthly_candidates
from scripts.strategy_diagnostics import sector_decomposition as sd
from tests.unit.medium_term.test_portfolio_and_candidates import cross_section_prices

ROOT = Path(__file__).resolve().parents[3]
SOURCE = (ROOT / 'scripts/strategy_diagnostics/sector_decomposition.py').read_text()


def _undefined_globals(source: str, filename: str, available: set) -> list[str]:
    """`source` 里**引用得到、但既不在本模块全局、也不是内建**的名字。

    逐作用域判定（`symtable`）：函数体里的局部变量、参数、推导式变量都不会误报，
    只有真正落到全局查找的名字才被检查。
    """
    missing: list[str] = []

    def walk(table):
        for sym in table.get_symbols():
            name = sym.get_name()
            if not sym.is_referenced():
                continue
            if table.get_type() == 'module':
                if not sym.is_assigned() and not sym.is_imported() and name not in available:
                    missing.append(name)
            elif sym.is_global() and name not in available:
                missing.append(name)
        for child in table.get_children():
            walk(child)

    walk(symtable.symtable(source, filename, 'exec'))
    return sorted(set(missing))


def test_module_has_no_undefined_globals():
    """整个模块不得有解析不到的全局名 —— 否则它会在**计算末尾**才崩。

    反例就是本文件的由来：`simulate_multi_asset_portfolio` 漏了导入。
    """
    available = set(vars(sd)) | set(dir(builtins))
    assert _undefined_globals(SOURCE, str(sd.__file__), available) == []


def test_undefined_global_check_catches_a_removed_import():
    """**注入缺陷即失败**：删掉那一行导入，检查必须点名报出它。

    没有这条，"上面那条测试通过"只说明检查是哑的（它可能在检查一个空的集合）。
    """
    assert 'from scripts.medium_term.portfolio_engine import simulate_multi_asset_portfolio' in SOURCE
    mutated = SOURCE.replace(
        'from scripts.medium_term.portfolio_engine import simulate_multi_asset_portfolio\n', '')
    # 只有那一行被删；其余全局可用性不变（用真模块的 globals 作基准）
    available = (set(vars(sd)) - {'simulate_multi_asset_portfolio'}) | set(dir(builtins))
    assert _undefined_globals(mutated, str(sd.__file__), available) == \
        ['simulate_multi_asset_portfolio']


def test_arm_control_can_actually_evaluate():
    """`ARM_A_MISMATCH_WITH_ATTRIBUTION` 这条控制必须**能算出结果**。

    控制项读的是归因实验产物里的 A 臂；产物键名一变，这个控制就变成 `KeyError`
    或者（更糟）永远比较两个同样的东西 —— 一个不能失败的控制与没有控制等价。
    """
    prior = json.loads(sd.ATTRIBUTION_RESULT.read_text())['arms']['A']
    for key in ('CAGR', 'max_drawdown', 'final_equity', 'accepted_entries'):
        assert key in prior, f'控制项要比的键在归因产物里不存在：{key}'
    assert prior['universe_size'] == 13


def test_subset_closure_holds_on_the_registered_mapping():
    """登记里的分类表必须真的闭合：`T ∪ N == B`、`T ∩ N == ∅`、`A ⊆ T`。"""
    closure = sd._check_subsets(sd.read(sd.REGISTRATION))
    assert closure['universe'] == 32
    assert closure['tech_broad'] == 17 and closure['non_tech'] == 15
    assert closure['tech_narrow'] == 13
    assert closure['rule_tech_not_in_hindsight_13'] == [
        'SEC-US-AVGO', 'SEC-US-AXTI', 'SEC-US-COHR', 'SEC-US-NBIS']
    assert closure['gics_reclassified_out'] == [
        'SEC-US-AMZN', 'SEC-US-GOOGL', 'SEC-US-META', 'SEC-US-NFLX']


def _reg():
    return json.loads(sd.REGISTRATION.read_text())


def test_subset_closure_rejects_an_overlapping_partition():
    """同一只证券被两边都收 ⇒ 分解不闭合，必须拒绝（否则 T−A 与 N−T 有共同分量）。

    用 `GOOGL` 做这次注入：它**不在** GICS 窄口径里，所以只会撞上闭合这一条，
    不会先被 `NARROW_NOT_SUBSET_OF_BROAD` 拦下 —— 测试要钉的是哪一条守卫就只触发哪一条。
    （注意是**只往 N 追加、不从 T 删**：一删一加是一次干净的搬家，并集不变、交集为空、
    守卫根本不该响。重叠必须是"两边都收"。）
    """
    reg = _reg()
    reg['sector_mapping']['non_tech_in_both'].append('SEC-US-GOOGL')
    with pytest.raises(ValueError, match='SUBSET_CLOSURE_FAILED'):
        sd._check_subsets(reg)


def test_subset_closure_rejects_a_missing_member():
    """少一只（并集 != 32）⇒ 拒绝 —— 静默少一只会让 B 臂与 T∪N 不再是同一个池子。"""
    reg = _reg()
    reg['sector_mapping']['non_tech_in_both'].remove('SEC-US-XOM')
    with pytest.raises(ValueError, match='SUBSET_CLOSURE_FAILED'):
        sd._check_subsets(reg)


def test_subset_closure_rejects_narrow_not_inside_broad():
    """GICS 窄口径若跑出宽口径之外，第二套边界就不再是"同一件事的更严版本"。"""
    reg = _reg()
    reg['sector_mapping']['tech_narrow_gics']['names'].append('SEC-US-XOM')
    with pytest.raises(ValueError, match='NARROW_NOT_SUBSET_OF_BROAD'):
        sd._check_subsets(reg)


def test_subset_closure_rejects_hindsight_arm_outside_tech():
    """A 臂（13 只硬编码）必须落在宽口径科技里 —— 否则 T−A 不再是同行业内的比较。

    用 `AMZN` 做注入：它在 A 里、**不在**窄口径里（GICS 把它归消费），所以这条注入
    只触发 `A_NOT_INSIDE_TECH`，不会先撞上窄口径那一条。
    """
    reg = _reg()
    reg['sector_mapping']['tech_broad']['names'].remove('SEC-US-AMZN')
    reg['sector_mapping']['non_tech_in_both'].append('SEC-US-AMZN')
    with pytest.raises(ValueError, match='A_NOT_INSIDE_TECH'):
        sd._check_subsets(reg)


def test_static_mask_marks_only_that_arms_members():
    """掩码必须**只**把本臂的池子标为合格。

    这是多臂共享管线正确性的全部所在：掩码若把并集都标合格，每臂都会在**同一个
    并集**上排名、选出同一批前 5 —— 六臂会得到六个几乎相同的数字，而看不出错。
    """
    sessions = pd.to_datetime(['2020-01-02', '2020-01-03'])
    mask = sd.static_mask(['SEC-US-AAPL', 'SEC-US-XOM'], sessions)
    assert len(mask) == 4
    assert set(mask.session) == set(sessions)
    for session in sessions:
        eligible = set(mask[(mask.session == session) & mask.eligible].security_id)
        assert eligible == {'SEC-US-AAPL', 'SEC-US-XOM'}


def test_static_mask_excludes_non_members():
    """反证：非成员必须在掩码里是**不合格**的。

    退化成"全部合格"的实现在上一条里也可能侥幸通过（那里恰好只喂了成员），
    所以这里喂一个**真子集**并要求非成员不合格。
    """
    sessions = pd.to_datetime(['2020-01-02'])
    universe = sorted(set(sd.arm_names()['B']))
    non_tech = ['SEC-US-XOM']
    mask = sd.static_mask(non_tech, sessions)
    assert int(mask.eligible.sum()) == 1
    full = sd.static_mask(universe, sessions)
    assert int(full.eligible.sum()) == len(universe) == 32


def _snapshot_fixture():
    """三只证券、斜率不同的小面板（复用 `cross_section_prices` 那套夹具）。"""
    prices = cross_section_prices()
    sessions = pd.DatetimeIndex(prices.session.unique())
    market = pd.DataFrame({'session': sessions, 'asof_close': 101., 'asof_ma200': 100.})
    return prices, sessions, market


def _mask(sessions, members):
    return pd.DataFrame([{'session': s, 'security_id': sid, 'eligible': sid in set(members)}
                         for s in sessions for sid in ('A', 'B', 'C')])


def test_shared_snapshot_path_equals_the_single_arm_path():
    """共享管线（`monthly_snapshots` + `assemble_candidates`）必须与单臂路径**逐帧相同**。

    六臂都走共享管线，所以这是"六臂的数到底是不是各自池子算出来的"的前提。
    单臂路径 = `generate_monthly_candidates`（它现在就是这两个函数拼起来的，但这条测试
    钉的是**带掩码时**两者等价，而不是"实现恰好共用"这件事本身）。
    """
    prices, sessions, market = _snapshot_fixture()
    mask = _mask(sessions, {'A', 'B'})
    shared = sd.assemble_candidates(sd.monthly_snapshots(prices), prices, market,
                                    top_n=2, members=mask)
    direct = generate_monthly_candidates(prices, market, top_n=2, members=mask)
    pd.testing.assert_frame_equal(shared, direct)


def test_different_masks_select_different_securities():
    """不同的掩码必须选出**不同**的证券。

    若掩码失效（例如实现退化成"并集全体合格"），六个臂会选出同一批前 5 ——
    六份数字会几乎相同，而**看不出错**。所以这条要能抓住它。
    """
    prices, sessions, market = _snapshot_fixture()
    ab = sd.assemble_candidates(sd.monthly_snapshots(prices), prices, market, top_n=2,
                                members=_mask(sessions, {'A', 'B'}))
    bc = sd.assemble_candidates(sd.monthly_snapshots(prices), prices, market, top_n=2,
                                members=_mask(sessions, {'B', 'C'}))
    picked_ab = set(ab[ab.selected.astype(bool)].security_id)
    picked_bc = set(bc[bc.selected.astype(bool)].security_id)
    assert picked_ab == {'A', 'B'}
    assert picked_bc == {'B', 'C'}
    assert picked_ab != picked_bc


def test_output_exists_is_refused(tmp_path):
    """产物只能新建 —— 覆盖会让"跑之前登记的是什么"无从核对。"""
    out = tmp_path / 'result.json'
    out.write_text('{}')
    with pytest.raises(ValueError, match='OUTPUT_EXISTS'):
        sd.execute(out)


def test_already_examined_registration_is_refused(tmp_path, monkeypatch):
    """登记自称已看过结果 ⇒ 拒绝。本登记 `returns_examined: false`，这条是它的执行力。"""
    monkeypatch.setattr(sd, 'read', lambda path: {'returns_examined': True})
    with pytest.raises(ValueError, match='REGISTRATION_NOT_BLIND'):
        sd.execute(tmp_path / 'r.json')


def test_registration_is_present_and_declares_the_four_arms():
    """登记必须真的描述四臂，且**先于**结果存在（本文件提交时它已经在 git 里）。"""
    reg = sd.read(sd.REGISTRATION)
    assert reg['returns_examined'] is False
    assert set(reg['arms']) == {'A_13_hindsight_tech', 'T_tech_broad',
                                'N_non_tech', 'B_32_ref'}
    assert 'T − A' in reg['decomposition']['selection_effect']
    assert 'N − T' in reg['decomposition']['sector_effect']
