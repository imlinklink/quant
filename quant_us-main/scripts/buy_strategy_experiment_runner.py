#!/usr/bin/env python3
"""从冻结 setup/触发表生成 A/B/C/D 入场候选，并执行 point-in-time universe 门。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

GROUP_LABELS = {'A': 'legacy_dip_buy', 'B': 'setup_legacy_timing',
                'C': 'setup_next_open', 'D': 'setup_intraday_timing'}


def _time(frame, column):
    out = frame.copy()
    out[column] = pd.to_datetime(out[column], utc=True)
    return out


def build_abcd_entries(baseline: pd.DataFrame, setups: pd.DataFrame,
                       legacy_timing: pd.DataFrame,
                       new_timing: pd.DataFrame) -> pd.DataFrame:
    """输入均为冻结信号表；不在此处重新计算技术指标。"""
    required_setup = {'setup_id', 'stock', 'valid_from', 'expires_at', 'next_open_time',
                      'next_open_price', 'initial_stop'}
    if not required_setup.issubset(setups):
        raise ValueError('setups 缺字段: ' + ','.join(sorted(required_setup - set(setups))))
    s = _time(_time(_time(setups, 'valid_from'), 'expires_at'), 'next_open_time')
    frames = []

    a = baseline.copy()
    if not a.empty:
        a = _time(a, 'entry_time')
        a['experiment'] = 'A'; a['entry_method'] = GROUP_LABELS['A']
        if 'setup_id' not in a: a['setup_id'] = ''
        frames.append(a)

    lt = _time(legacy_timing, 'entry_time') if not legacy_timing.empty else legacy_timing
    if not lt.empty:
        joined = lt.merge(s, on='stock', suffixes=('_timing', ''))
        joined = joined[(joined['entry_time'] >= joined['valid_from']) &
                        (joined['entry_time'] <= joined['expires_at'])]
        # 一个旧 timing 事件只绑定当时最新的一份有效 setup。
        joined = joined.sort_values('valid_from', ascending=False).drop_duplicates('signal_id')
        joined['entry_price'] = joined.get('entry_price_timing', joined.get('entry_price'))
        joined['experiment'] = 'B'; joined['entry_method'] = GROUP_LABELS['B']
        frames.append(joined)

    c = s.copy()
    c['entry_time'] = c['next_open_time']; c['entry_price'] = c['next_open_price']
    c['experiment'] = 'C'; c['entry_method'] = GROUP_LABELS['C']
    frames.append(c)

    nt = _time(new_timing, 'entry_time') if not new_timing.empty else new_timing
    if not nt.empty:
        if 'triggered' in nt:
            nt = nt[nt['triggered'].astype(bool)]
        d = nt.merge(s, on='setup_id', suffixes=('_timing', ''))
        if 'stock_timing' in d:
            bad = d['stock_timing'].ne(d['stock'])
            if bad.any(): raise ValueError('new_timing 与 setup 股票不一致')
        d = d[(d['entry_time'] >= d['valid_from']) & (d['entry_time'] <= d['expires_at'])]
        d['entry_price'] = d.get('entry_price_timing', d.get('entry_price'))
        d['experiment'] = 'D'; d['entry_method'] = GROUP_LABELS['D']
        frames.append(d)

    out = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    if out.empty: return out
    out['stock'] = out.get('stock', out.get('stock_timing'))
    out['signal_id'] = out.get('signal_id', '').fillna('') if hasattr(out.get('signal_id', ''), 'fillna') else ''
    out['entry_id'] = out['setup_id'].where(out['setup_id'].astype(str).ne(''), out['signal_id'])
    out = out.sort_values(['entry_time', 'experiment', 'stock', 'setup_id'])
    return out.drop_duplicates(['experiment', 'stock', 'entry_id', 'entry_time']).reset_index(drop=True)


def apply_universe(entries: pd.DataFrame, universe: pd.DataFrame):
    if entries.empty: return entries.copy(), entries.copy()
    u = universe.copy()
    if not {'universe_date', 'code', 'eligible'}.issubset(u):
        raise ValueError('universe 需要 universe_date/code/eligible')
    u['universe_date'] = pd.to_datetime(u['universe_date']).dt.date
    e = entries.copy(); e['universe_date'] = pd.to_datetime(e['entry_time'], utc=True).dt.date
    joined = e.merge(u[['universe_date', 'code', 'eligible', 'quality', 'reason']],
                     left_on=['universe_date', 'stock'], right_on=['universe_date', 'code'],
                     how='left')
    joined['eligible'] = joined['eligible'].fillna(False).astype(bool)
    joined['portfolio_reject_reason'] = joined['reason'].fillna('NOT_IN_POINT_IN_TIME_UNIVERSE')
    return joined[joined['eligible']].copy(), joined[~joined['eligible']].copy()


def write_new(path, frame):
    p = Path(path)
    if p.exists(): raise FileExistsError(f'禁止覆盖实验产物: {p}')
    p.parent.mkdir(parents=True, exist_ok=True); frame.to_csv(p, index=False)


def main():
    p = argparse.ArgumentParser(description='生成冻结 A/B/C/D 入场集合')
    p.add_argument('--manifest', required=True); p.add_argument('--baseline', required=True)
    p.add_argument('--setups', required=True); p.add_argument('--legacy-timing', required=True)
    p.add_argument('--new-timing', required=True); p.add_argument('--universe', required=True)
    p.add_argument('--output-dir', required=True)
    args = p.parse_args()
    from scripts.experiment_manifest import validate_manifest
    manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    errors = validate_manifest(manifest)
    if errors: raise SystemExit('manifest 无效: ' + ','.join(errors))
    entries = build_abcd_entries(pd.read_csv(args.baseline), pd.read_csv(args.setups),
                                 pd.read_csv(args.legacy_timing), pd.read_csv(args.new_timing))
    accepted, rejected = apply_universe(entries, pd.read_csv(args.universe))
    out = Path(args.output_dir)
    write_new(out / 'signals.csv', accepted); write_new(out / 'rejected_signals.csv', rejected)
    print(json.dumps({'signals': len(accepted), 'rejected': len(rejected)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
