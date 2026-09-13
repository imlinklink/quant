#!/usr/bin/env python3
"""合并B1年度分区并生成价差诊断；不调用富途公司行动导入、不计算收益。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.derive_corporate_actions import derive_actions
from scripts.data.io_utils import sha256_file, write_frame


def _load_partitions(root: Path, adjustment: str, codes: list[str]) -> pd.DataFrame:
    frames = []
    for code in codes:
        files = sorted((root / 'day' / adjustment).glob(f'year=*/{code.replace(".", "_")}.csv.gz'))
        if not files:
            raise ValueError(f'ETF_PARTITIONS_MISSING:{adjustment}:{code}')
        item = pd.concat([pd.read_csv(path) for path in files], ignore_index=True)
        item['session'] = pd.to_datetime(item.time_key).dt.normalize()
        item['security_id'] = 'SEC-' + code.replace('.', '-')
        item = item.sort_values('session').drop_duplicates('session', keep='last')
        item['source_code'] = code
        frames.append(item[['security_id', 'source_code', 'session', 'open', 'high',
                            'low', 'close', 'volume']])
    return pd.concat(frames, ignore_index=True)


def prepare(root, universe, output):
    universe_frame = pd.read_csv(universe)
    codes = sorted(universe_frame.code.astype(str).unique())
    if len(codes) != 8:
        raise ValueError(f'ETF_UNIVERSE_MUST_HAVE_8:{len(codes)}')
    raw = _load_partitions(Path(root), 'none', codes)
    qfq = _load_partitions(Path(root), 'qfq', codes)
    if set(zip(raw.security_id, raw.session)) != set(zip(qfq.security_id, qfq.session)):
        raise ValueError('ETF_RAW_QFQ_SESSION_MISMATCH')
    action_frames = []
    coverage = []
    for security_id, group in raw.groupby('security_id'):
        adjusted = qfq[qfq.security_id == security_id]
        actions = derive_actions(group, adjusted, security_id)
        if not actions.empty:
            action_frames.append(actions)
        coverage.append({'security_id': security_id, 'rows': len(group),
                         'start': str(group.session.min().date()),
                         'end': str(group.session.max().date()),
                         'derived_actions': len(actions)})
    action_columns = ['security_id', 'action_type', 'ex_date', 'ratio',
                      'cash_amount', 'source_id', 'quality_status']
    actions = (pd.concat(action_frames, ignore_index=True)
               if action_frames else pd.DataFrame(columns=action_columns))
    invalid_ratio = actions.action_type.isin(['split', 'reverse_split']) & (
        pd.to_numeric(actions.ratio, errors='coerce').sub(1).abs() <= .01)
    duplicate_cash = actions.action_type.eq('cash_dividend') & actions.assign(
        ex_date=pd.to_datetime(actions.ex_date)).sort_values(
            ['security_id', 'cash_amount', 'ex_date']).groupby(
                ['security_id', 'cash_amount']).ex_date.diff().dt.days.le(7)
    actions['candidate_issue'] = ''
    actions.loc[invalid_ratio, 'candidate_issue'] = 'NO_OP_SPLIT_RATIO'
    actions.loc[duplicate_cash, 'candidate_issue'] = 'POSSIBLE_DUPLICATE_CASH_EVENT'
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    paths = {'raw': out / 'etf_raw_daily.csv.gz',
             'qfq': out / 'etf_qfq_daily.csv.gz',
             'actions': out / 'etf_action_candidates.csv'}
    for path in paths.values():
        if path.exists():
            raise FileExistsError(f'禁止覆盖ETF审计产物: {path}')
    write_frame(raw, paths['raw'])
    write_frame(qfq, paths['qfq'])
    write_frame(actions, paths['actions'])
    candidate_issues = actions[actions.candidate_issue.ne('')][
        ['security_id', 'ex_date', 'candidate_issue']].to_dict('records')
    summary = {'status': 'blocked_pending_action_verification',
               'codes': codes, 'coverage': coverage,
               'futu_action_import': 'disabled_zero_rows_in_pilot',
               'action_quality': 'unverified_derived_qfq_vs_raw',
               'action_candidates_are_accounting_input': False,
               'candidate_issues': candidate_issues,
               'formal_performance_allowed': False,
               'files': {name: {'path': str(path), 'sha256': sha256_file(path),
                                'rows': len({'raw': raw, 'qfq': qfq,
                                             'actions': actions}[name])}
                         for name, path in paths.items()}}
    summary_path = out / 'etf_input_audit.json'
    if summary_path.exists():
        raise FileExistsError(f'禁止覆盖ETF审计产物: {summary_path}')
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n',
                            encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser(description='准备B1 ETF行情及价差诊断（不导入公司行动）')
    parser.add_argument('--root', required=True)
    parser.add_argument('--universe', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.root, args.universe, args.output), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
