"""公司行动表的**只追加**合并：观察期内新出现的拆股/分红必须进得来，历史一格不能改。

为什么需要它：三臂前向观察的行动表原先指向一份**冻结 run**（`futu-actions39-20260920c`），
而行情刷新不刷行动 ⇒ 观察期内新出现的拆股**不会被应用**，价格会"假摔"（3:1 拆股表现为
−67%）⇒ **触发假止损**。这不是偏差，是会污染结论的雷（与 §3.1 里 SHW 那条同类）。

三条纪律：

1. **只追加**：`base` 的每一行原样保留，新增行**按内容**（`record_hash`）追加。历史段被
   改写会让已记录的观察失去可复现性。
2. **幂等**：`record_hash` 已在 base 里的就跳过 ⇒ 一天跑三次不会重复追加。
3. **拆股的同键异内容 = 冲突，必须炸出来**：已宣告的拆股被修订或取消是**输入变更**，
   静默处理会让"当时按什么记账的"无从判断，要人来决定（与 `frozen_code` 守卫同一立场）。

**为什么只有拆股有第 3 条**：股息做不到 —— 同一除息日两条不同金额的股息是**合法的**
（MSFT 2004-11-15 的 $3.00 特别 + $0.08 常规），与"分红被修订"在数据上不可区分。
所以股息的修订会表现为**多一条记录**，这一点必须随观察结论一起披露。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

# **身份是内容，不是 (证券, 除息日, 类型)**：实测冻结表里 MSFT 2004-11-15 有**两条**股息 ——
# $3.00 特别股息与 $0.08 常规股息落在同一除息日（`source_record_id` 已经把两者分开：
# `US.MSFT|2004-11-15|3.0` vs `|0.08`）。按三元组判键会把**合法记录**误判成冲突。
IDENTITY = 'record_hash'
# 只有**拆股**做同键冲突检查：同一证券同一除息日出现两个不同拆股比近乎不可能，
# 那正是"拆股被修订（或取消）"的样子。股息没有这道检查 —— 它与上面的特别股息不可区分。
SPLIT_KEY = ('security_id', 'ex_date', 'action_type')
SPLIT_KINDS = ('split', 'reverse_split')


def merge_frames(base: pd.DataFrame, new: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """`base` ∪ `new`（只追加）。返回 (合并结果, 统计)。同键异内容抛 `ValueError`。"""
    for frame, label in ((base, 'base'), (new, 'new')):
        if missing := [c for c in (*SPLIT_KEY, IDENTITY) if c not in frame.columns]:
            raise ValueError(f'ACTION_MERGE_COLUMNS_MISSING:{label}:{missing}')
    # 导入结果的列必须是 base 的超集：缺列会让"追加"变成一串 NaN，而那是**静默**的
    # 会计错误（导入器 schema 变了就会这样）
    if extra := sorted(set(base.columns) - set(new.columns)):
        raise ValueError(f'ACTION_MERGE_INCOMING_COLUMNS_MISSING:{extra}')
    known_hashes = set(base[IDENTITY].astype(str))
    # 拆股：同键不同内容 ⇒ 修订/取消，必须炸出来（输入变更要人决定）
    base_splits = {tuple(str(r[c]) for c in SPLIT_KEY): str(r[IDENTITY])
                   for r in base[base.action_type.isin(SPLIT_KINDS)].to_dict('records')}
    revised = [k for r in new[new.action_type.isin(SPLIT_KINDS)].to_dict('records')
               if (k := tuple(str(r[c]) for c in SPLIT_KEY)) in base_splits
               and base_splits[k] != str(r[IDENTITY])]
    if revised:
        raise ValueError(f'SPLIT_REVISED:{sorted(revised)[:5]}'
                         '（同一证券同一除息日的拆股内容变了 —— 行动被修订或取消，'
                         '需要人工确认后再继续观察）')
    fresh = new[~new[IDENTITY].astype(str).isin(known_hashes)]
    if fresh.empty:
        return base.copy(), {'added': 0, 'base_rows': len(base), 'incoming_rows': len(new),
                             'already_known': int(len(new))}
    merged = pd.concat([base, fresh[list(base.columns)]], ignore_index=True)
    return merged, {'added': int(len(fresh)), 'base_rows': len(base),
                    'incoming_rows': len(new), 'already_known': int(len(new) - len(fresh))}


def merge_files(base: Path, new: Path, out: Path) -> dict:
    """读两份 CSV → 合并 → **只有确实新增才写** `out`。"""
    base_df, new_df = pd.read_csv(base), pd.read_csv(new)
    for frame in (base_df, new_df):
        frame['security_id'] = frame.security_id.astype(str)
    merged, stats = merge_frames(base_df, new_df)
    if stats['added']:
        out.parent.mkdir(parents=True, exist_ok=True)
        merged.to_csv(out, index=False)
    return {**stats, 'out': str(out), 'written': bool(stats['added'])}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', required=True, help='既有行动表（历史段原样保留）')
    p.add_argument('--new', required=True, help='本次导入的结果')
    p.add_argument('--out', required=True)
    args = p.parse_args(argv)
    try:
        print(json.dumps(merge_files(Path(args.base), Path(args.new), Path(args.out)),
                         ensure_ascii=False, indent=2))
        return 0
    except ValueError as exc:
        # 冲突不是"这次没新增"，是**输入变更** —— 必须非 0 退出让人看见
        print(json.dumps({'status': 'ENGINEERING_BLOCKED', 'reason': str(exc)},
                         ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
