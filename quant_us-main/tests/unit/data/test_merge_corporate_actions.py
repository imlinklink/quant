"""公司行动表的只追加合并：新增进得来、历史不能动、**拆股**被修订必须炸出来。

设计是**照真实数据改过的**：第一版按 `(证券, 除息日, 类型)` 判键，撞上冻结表里
MSFT 2004-11-15 的**两条**股息（$3.00 特别 + $0.08 常规落在同一除息日）—— 那是合法记录，
不是冲突。所以身份改成**内容**（`record_hash`），同键冲突检查只留给拆股。
"""
import pandas as pd
import pytest

from scripts.data.merge_corporate_actions import merge_files, merge_frames

COLUMNS = ['security_id', 'action_type', 'ex_date', 'effective_at', 'ratio', 'cash_amount',
           'source_record_id', 'record_hash']


def frame(rows):
    return pd.DataFrame(rows, columns=COLUMNS)


def row(sid, kind, ex, ratio, cash, h):
    return [sid, kind, ex, None, ratio, cash, f'{sid}|{ex}|{cash}', h]


BASE = frame([row('SEC-US-A', 'split', '2021-04-01', 3.0, 0.0, 'h-shw'),
              row('SEC-US-B', 'cash_dividend', '2026-09-04', 0.0, 0.22, 'h-b1')])


def test_only_appends_and_leaves_history_untouched():
    """`base` 的每一行原样保留 —— 历史段被改写会让已记录的观察失去可复现性。"""
    merged, stats = merge_frames(BASE, frame([row('SEC-US-C', 'split', '2026-10-01', 2.0, 0.0, 'h-c1')]))
    assert stats['added'] == 1
    assert merged.iloc[:len(BASE)].equals(BASE)
    assert set(merged.security_id) == {'SEC-US-A', 'SEC-US-B', 'SEC-US-C'}


def test_reimporting_the_same_actions_is_idempotent():
    """身份 = 内容 ⇒ 一天跑三次不会重复追加。"""
    merged, stats = merge_frames(BASE, BASE.copy())
    assert stats['added'] == 0 and stats['already_known'] == len(BASE)
    assert len(merged) == len(BASE)


def test_two_dividends_on_one_ex_date_are_legitimate():
    """同一除息日**两条**股息是合法的（MSFT 2004-11-15 的 $3.00 特别 + $0.08 常规）。

    这正是第一版设计错的地方：按三元组判键会把合法记录当冲突拒掉。
    """
    base = frame([row('SEC-US-MSFT', 'cash_dividend', '2004-11-15', 0.0, 3.00, 'h-special')])
    merged, stats = merge_frames(base, frame([row('SEC-US-MSFT', 'cash_dividend', '2004-11-15', 0.0, 0.08, 'h-regular')]))
    assert stats['added'] == 1
    assert len(merged) == 2


def test_a_revised_split_is_refused_loudly():
    """拆股的同键异内容 ⇒ 修订/取消，必须炸出来。

    同一证券同一除息日出现两个不同拆股比近乎不可能，所以那正是"拆股被改了"的样子。
    静默处理会让"当时按什么记账的"无从判断 —— 它属于**输入变更**，要人决定
    （与 `frozen_code` 守卫同一个立场）。
    """
    revised = frame([row('SEC-US-A', 'split', '2021-04-01', 4.0, 0.0, 'h-shw-revised')])
    with pytest.raises(ValueError, match='SPLIT_REVISED'):
        merge_frames(BASE, revised)


def test_incoming_must_carry_every_column_the_base_has():
    """导入结果缺列会让"追加"变成一串 NaN，而那是**静默**的会计错误。"""
    thin = frame([row('SEC-US-C', 'split', '2026-10-01', 2.0, 0.0, 'h')]).drop(
        columns=['source_record_id'])
    with pytest.raises(ValueError, match='ACTION_MERGE_INCOMING_COLUMNS_MISSING'):
        merge_frames(BASE, thin)


def test_missing_identity_column_is_reported():
    with pytest.raises(ValueError, match='ACTION_MERGE_COLUMNS_MISSING'):
        merge_frames(BASE, frame([row('SEC-US-C', 'split', '2026-10-01', 2.0, 0.0, 'h')]).drop(
            columns=['record_hash']))


def test_files_are_only_written_when_something_was_added(tmp_path):
    """没有新增就**不写** —— 免得每天重写一遍同一个文件（也会把 mtime 弄脏）。"""
    base_path, new_path, out = tmp_path / 'b.csv', tmp_path / 'n.csv', tmp_path / 'o.csv'
    BASE.to_csv(base_path, index=False)
    BASE.to_csv(new_path, index=False)
    assert merge_files(base_path, new_path, out)['written'] is False
    assert not out.exists()
    frame([row('SEC-US-C', 'split', '2026-10-01', 2.0, 0.0, 'h-c1')]).to_csv(new_path, index=False)
    stats = merge_files(base_path, new_path, out)
    assert stats['written'] is True and len(pd.read_csv(out)) == len(BASE) + 1
