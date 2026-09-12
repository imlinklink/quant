#!/usr/bin/env python3
"""历史证券主数据 v2：稳定证券 ID、ticker 历史、公司行动、跨源冲突审计。

对应技术设计 `historical-universe-and-evidence-handoff-technical-design-2026-09-12.md`
§2.2 数据契约与 §2.3 采集接口。本模块只做契约与校验，不内置任何真实数据源；
真实来源由适配器提供（见 import_* CLI），测试使用本地 fixture。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# 一行代表一只证券的一个有效属性区间（范围：当前存续普通股样本，不含退市字段）。
MASTER_COLUMNS = ('security_id', 'issuer_id', 'asset_type', 'exchange', 'currency',
                  'valid_from', 'valid_to', 'listed_at', 'source_id', 'source_record_id',
                  'source_observed_at', 'ingested_at', 'record_hash', 'quality_status')
# 一行一个 ticker 有效区间。
SYMBOL_COLUMNS = ('security_id', 'symbol', 'exchange', 'valid_from', 'valid_to',
                  'source_id', 'source_record_id', 'quality_status')
# 一行一个公司行动。
ACTION_COLUMNS = ('security_id', 'action_type', 'ex_date', 'effective_at', 'ratio',
                  'cash_amount', 'source_id', 'source_record_id',
                  'source_published_at', 'source_observed_at', 'record_hash')

ASSET_TYPES = ('stock', 'etf', 'leveraged_etf', 'adr', 'other')
# 优先覆盖拆股/反向拆股/股息；并购与分拆单独处理（见 UNRESOLVED_ACTION_TYPES）。
ACTION_TYPES = ('split', 'reverse_split', 'cash_dividend', 'stock_dividend',
                'merger', 'spinoff')
QUALITY_STATUS = ('verified', 'unverified', 'conflict', 'missing_history')
# 退市结算不在范围；样本期内并购/分拆无法核实价格衔接时标此原因并排除绩效。
UNRESOLVED_ACTION_TYPES = ('merger', 'spinoff')
# 记录哈希覆盖的稳定字段（不含来源时间与哈希本身，保证跨源/跨运行稳定）。
HASH_FIELDS = ('security_id', 'issuer_id', 'asset_type', 'exchange', 'currency',
               'valid_from', 'valid_to', 'listed_at')
PLACEHOLDER_DATES = ('1970-01-01', '1970-01-01T00:00:00', '1900-01-01')


def _empty(value) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == ''


def _norm_date(value):
    """把日期规整为 ISO 'YYYY-MM-DD'；空值返回 None。"""
    if _empty(value):
        return None
    stamp = pd.to_datetime(value, errors='coerce', utc=False)
    if pd.isna(stamp):
        raise ValueError(f'INVALID_DATE:{value}')
    return stamp.strftime('%Y-%m-%d')


def _norm_time(value):
    """把时间规整为 UTC ISO8601；空值返回 None。"""
    if _empty(value):
        return None
    stamp = pd.to_datetime(value, errors='coerce', utc=True)
    if pd.isna(stamp):
        raise ValueError(f'INVALID_TIMESTAMP:{value}')
    return stamp.tz_convert('UTC').isoformat()


def canonical_record_hash(record: dict) -> str:
    """对稳定字段规范序列化后取 SHA-256；相同记录跨运行稳定。"""
    payload = {k: (None if _empty(record.get(k)) else str(record.get(k)).strip())
               for k in HASH_FIELDS}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def _stamp_times(frame: pd.DataFrame, columns) -> pd.DataFrame:
    out = frame.copy()
    for column in columns:
        if column in out.columns:
            out[column] = out[column].map(_norm_date)
    return out


def normalize_master(frame: pd.DataFrame, source_id: str, ingested_at=None) -> pd.DataFrame:
    """把某来源的原始主数据标准化为 v2 主表行，并补 source_id/ingested_at/record_hash。"""
    missing = [c for c in MASTER_COLUMNS if c not in frame.columns
               and c not in ('source_id', 'ingested_at', 'record_hash')]
    if missing:
        raise ValueError('主数据缺字段: ' + ','.join(sorted(missing)))
    d = frame.copy()
    d['source_id'] = source_id
    d['ingested_at'] = _norm_time(ingested_at) or datetime.now(timezone.utc).isoformat()
    d['asset_type'] = d['asset_type'].astype(str).str.strip().str.lower()
    d['quality_status'] = d['quality_status'].astype(str).str.strip().str.lower()
    for column in ('valid_from', 'valid_to', 'listed_at'):
        d[column] = d[column].map(_norm_date)
    d['source_observed_at'] = d['source_observed_at'].map(_norm_time)
    d['security_id'] = d['security_id'].astype(str).str.strip()
    d['record_hash'] = [canonical_record_hash(row) for row in d.to_dict('records')]
    return d[list(MASTER_COLUMNS)]


def normalize_symbols(frame: pd.DataFrame, source_id: str) -> pd.DataFrame:
    missing = [c for c in SYMBOL_COLUMNS if c not in frame.columns
               and c not in ('source_id',)]
    if missing:
        raise ValueError('symbol_history 缺字段: ' + ','.join(sorted(missing)))
    d = frame.copy()
    d['source_id'] = source_id
    d['symbol'] = d['symbol'].astype(str).str.strip().str.upper()
    d['quality_status'] = d['quality_status'].astype(str).str.strip().str.lower()
    for column in ('valid_from', 'valid_to'):
        d[column] = d[column].map(_norm_date)
    return d[list(SYMBOL_COLUMNS)]


def normalize_actions(frame: pd.DataFrame, source_id: str) -> pd.DataFrame:
    missing = [c for c in ACTION_COLUMNS if c not in frame.columns
               and c not in ('source_id', 'record_hash')]
    if missing:
        raise ValueError('corporate_actions 缺字段: ' + ','.join(sorted(missing)))
    d = frame.copy()
    d['source_id'] = source_id
    d['action_type'] = d['action_type'].astype(str).str.strip().str.lower()
    d['ex_date'] = d['ex_date'].map(_norm_date)
    if 'effective_at' in d.columns:
        d['effective_at'] = d['effective_at'].map(_norm_time)
    for column in ('source_published_at', 'source_observed_at'):
        if column in d.columns:
            d[column] = d[column].map(_norm_time)
    d['record_hash'] = [
        hashlib.sha256(json.dumps(
            {k: (None if _empty(row.get(k)) else str(row.get(k)))
             for k in ('security_id', 'action_type', 'ex_date', 'ratio', 'cash_amount')},
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        for row in d.to_dict('records')]
    return d[list(ACTION_COLUMNS)]


def find_overlaps(frame: pd.DataFrame, id_col: str = 'security_id') -> list:
    """返回同一 ID 下 [valid_from, valid_to) 重叠的区间对（valid_to 空视为无穷）。"""
    problems = []
    for key, group in frame.groupby(id_col, dropna=False):
        g = group.copy()
        g['vfrom'] = pd.to_datetime(g['valid_from'], errors='coerce')
        g['vto'] = pd.to_datetime(g['valid_to'], errors='coerce')
        g = g.sort_values('vfrom')
        rows = list(g.itertuples(index=False))
        for left, right in zip(rows, rows[1:]):
            left_to = left.vto
            if pd.isna(left_to) or right.vfrom < left_to:
                problems.append({'id': str(key), 'a_from': str(left.valid_from),
                                 'a_to': None if pd.isna(left_to) else str(left.valid_to),
                                 'b_from': str(right.valid_from),
                                 'b_to': None if pd.isna(right.vto) else str(right.valid_to)})
    return problems


def validate_master(master: pd.DataFrame) -> list:
    errors = []
    missing = [c for c in MASTER_COLUMNS if c not in master.columns]
    if missing:
        return ['MASTER_MISSING_COLUMNS:' + ','.join(sorted(missing))]
    if master.duplicated(['security_id', 'valid_from']).any():
        errors.append('SECURITY_ID_VALID_FROM_REPEATED')  # 同 ID 多区间合法，同起始日才冲突
    bad_types = sorted(set(master['asset_type']) - set(ASSET_TYPES))
    if bad_types:
        errors.append('BAD_ASSET_TYPE:' + ','.join(bad_types))
    bad_status = sorted(set(master['quality_status']) - set(QUALITY_STATUS))
    if bad_status:
        errors.append('BAD_QUALITY_STATUS:' + ','.join(bad_status))
    if master['valid_from'].map(_empty).any():
        errors.append('VALID_FROM_EMPTY')
    for column in ('valid_from', 'valid_to', 'listed_at'):
        for value in master[column].dropna():
            if str(value)[:10] in PLACEHOLDER_DATES or str(value).startswith('1970'):
                errors.append('PLACEHOLDER_DATE:' + column)
                break
    from_ts = pd.to_datetime(master['valid_from'], errors='coerce')
    to_ts = pd.to_datetime(master['valid_to'], errors='coerce')
    if ((to_ts.notna()) & (to_ts <= from_ts)).any():
        errors.append('VALID_TO_NOT_AFTER_FROM')
    if master['record_hash'].map(_empty).any():
        errors.append('RECORD_HASH_EMPTY')
    else:
        for row in master.to_dict('records'):
            if canonical_record_hash(row) != row['record_hash']:
                errors.append('RECORD_HASH_MISMATCH')
                break
    if find_overlaps(master[['security_id', 'valid_from', 'valid_to']]):
        errors.append('MASTER_INTERVAL_OVERLAP')
    return sorted(set(errors))


def validate_symbols(symbols: pd.DataFrame) -> list:
    errors = []
    missing = [c for c in SYMBOL_COLUMNS if c not in symbols.columns]
    if missing:
        return ['SYMBOL_MISSING_COLUMNS:' + ','.join(sorted(missing))]
    if symbols['valid_from'].map(_empty).any():
        errors.append('SYMBOL_VALID_FROM_EMPTY')
    if find_overlaps(symbols[['security_id', 'valid_from', 'valid_to']]):
        errors.append('SYMBOL_INTERVAL_OVERLAP')      # 同一证券的 symbol 区间不可重叠
    if symbol_reuse_conflicts(symbols):
        errors.append('SYMBOL_REUSE_CONFLICT')        # 同 symbol 同窗口指向不同证券
    return sorted(set(errors))


def symbol_reuse_conflicts(symbols: pd.DataFrame) -> list:
    """同一 symbol 在重叠时间窗内被两只不同证券使用。"""
    problems = []
    for symbol, group in symbols.groupby('symbol'):
        if group['security_id'].nunique() < 2:
            continue
        g = group.copy()
        g['vfrom'] = pd.to_datetime(g['valid_from'], errors='coerce')
        g['vto'] = pd.to_datetime(g['valid_to'], errors='coerce')
        rows = list(g.sort_values('vfrom').itertuples(index=False))
        for left, right in zip(rows, rows[1:]):
            if left.security_id == right.security_id:
                continue
            if pd.isna(left.vto) or right.vfrom < left.vto:
                problems.append({'symbol': symbol, 'a': str(left.security_id),
                                 'b': str(right.security_id)})
    return problems


def resolve_symbol(symbols: pd.DataFrame, symbol: str, date) -> dict:
    """按当时有效区间把 symbol+日期解析为 security_id。

    返回 {'security_id':.., 'status':..}；status ∈ resolved/not_found/ambiguous。
    """
    target = pd.Timestamp(date)
    rows = symbols[symbols['symbol'].astype(str).str.upper() == str(symbol).upper()].copy()
    if rows.empty:
        return {'security_id': None, 'status': 'not_found'}
    rows['_from'] = pd.to_datetime(rows['valid_from'], errors='coerce')
    rows['_to'] = pd.to_datetime(rows['valid_to'], errors='coerce')
    hit = rows[(rows['_from'] <= target) & (rows['_to'].isna() | (target < rows['_to']))]
    ids = sorted(set(hit['security_id'].astype(str)))
    if not ids:
        return {'security_id': None, 'status': 'not_found'}
    if len(ids) > 1:
        return {'security_id': None, 'status': 'ambiguous', 'candidates': ids}
    return {'security_id': ids[0], 'status': 'resolved'}


def merge_sources(masters: list, source_priority: list):
    """按来源优先级合并主数据；同 ID 属性冲突不静默覆盖，输出冲突表。

    返回 (merged, conflicts)；conflicts 非空时相关行 quality_status 记为 conflict。
    """
    if not masters:
        return pd.DataFrame(columns=list(MASTER_COLUMNS)), pd.DataFrame(
            columns=['security_id', 'field', 'sources', 'values'])
    data = pd.concat(masters, ignore_index=True)
    order = {name: i for i, name in enumerate(source_priority)}
    data['_priority'] = data['source_id'].map(order).fillna(len(order)).astype(int)
    conflicts = []
    fields = [f for f in HASH_FIELDS if f not in ('security_id',)]
    for sec_id, group in data.groupby('security_id'):
        for field in fields:
            values = sorted({str(v) for v in group[field] if not _empty(v)})
            if len(values) > 1:
                conflicts.append({'security_id': sec_id, 'field': field,
                                  'sources': ';'.join(sorted(set(group['source_id']))),
                                  'values': ';'.join(values)})
    conflict_ids = {c['security_id'] for c in conflicts}
    merged = data.sort_values(['security_id', '_priority']).groupby(
        'security_id', as_index=False).last()
    merged = merged.drop(columns=['_priority'])
    merged.loc[merged['security_id'].isin(conflict_ids), 'quality_status'] = 'conflict'
    merged['record_hash'] = [canonical_record_hash(row) for row in merged.to_dict('records')]
    return merged[list(MASTER_COLUMNS)].sort_values('security_id').reset_index(drop=True), \
        pd.DataFrame(conflicts, columns=['security_id', 'field', 'sources', 'values'])


def audit_master(master: pd.DataFrame, symbols: pd.DataFrame, actions: pd.DataFrame,
                 unresolved_actions=()) -> tuple:
    """逐证券输出质量问题并汇总。范围不含退市；并购/分拆无法核实价格衔接时标此原因。"""
    rows = []
    sec_ids = sorted(set(master.get('security_id', pd.Series(dtype=str))))
    symbol_ids = set(symbols.get('security_id', pd.Series(dtype=str)))
    unresolved = set(map(str, unresolved_actions))
    if actions is not None and not actions.empty:
        special = actions[actions['action_type'].astype(str).str.lower()
                          .isin(UNRESOLVED_ACTION_TYPES)]
        for record in special.to_dict('records'):
            ratio = pd.to_numeric(record.get('ratio'), errors='coerce')
            cash = pd.to_numeric(record.get('cash_amount'), errors='coerce')
            has_terms = (pd.notna(ratio) and float(ratio) > 0) or \
                (pd.notna(cash) and float(cash) > 0)
            if not has_terms:
                unresolved.add(str(record.get('security_id')))
    for sec_id in sec_ids:
        row = master[master['security_id'] == sec_id].iloc[0]
        problems = []
        if sec_id in unresolved:
            problems.append('corporate_action_unresolved')  # 价格衔接不可核实 → 排除绩效
        if sec_id not in symbol_ids:
            problems.append('MISSING_SYMBOL_HISTORY')
        if _empty(row.get('listed_at')):
            problems.append('MISSING_HISTORY')
        rows.append({'security_id': sec_id, 'asset_type': row.get('asset_type'),
                     'quality_status': row.get('quality_status'),
                     'problems': ';'.join(problems)})
    quality = pd.DataFrame(rows, columns=['security_id', 'asset_type', 'quality_status', 'problems'])

    def _count(token):
        return int(quality.problems.str.contains(token).sum()) if not quality.empty else 0

    summary = {
        'securities': int(len(sec_ids)),
        'master_errors': validate_master(master) if not master.empty else [],
        'symbol_errors': validate_symbols(symbols) if not symbols.empty else [],
        'corporate_action_unresolved': _count('corporate_action_unresolved'),
        'missing_symbol_history': _count('MISSING_SYMBOL_HISTORY'),
        'missing_history': _count('MISSING_HISTORY'),
    }
    return quality, summary


def build_outputs(sources: list, output_dir, tables=('master', 'symbols', 'actions'),
                  overwrite=False) -> dict:
    """从多个来源归档构建 v2 主表、ticker 历史、公司行动与冲突表。

    `sources` 为按优先级排列的 [{'source_id','archive_dir'}]。写入不可覆盖目录，
    并返回含输入/输出哈希的结构化摘要。
    """
    from scripts.data.io_utils import sha256_file, write_frame
    from scripts.data.source_archive import load_raw_table

    out = Path(output_dir)
    if out.exists() and any(out.iterdir()) and not overwrite:
        raise FileExistsError(f'输出目录非空，禁止覆盖: {out}')
    out.mkdir(parents=True, exist_ok=True)

    masters, symbols, actions, priority = [], [], [], []
    for item in sources:
        source_id, archive_dir = item['source_id'], item['archive_dir']
        priority.append(source_id)
        raw = load_raw_table(archive_dir, 'security_master')
        if raw is not None and 'master' in tables:
            masters.append(normalize_master(raw, source_id))
        raw = load_raw_table(archive_dir, 'symbol_history')
        if raw is not None and 'symbols' in tables:
            symbols.append(normalize_symbols(raw, source_id))
        raw = load_raw_table(archive_dir, 'corporate_actions')
        if raw is not None and 'actions' in tables:
            actions.append(normalize_actions(raw, source_id))

    summary = {'sources': [{'source_id': s['source_id'], 'archive_dir': str(s['archive_dir'])}
                           for s in sources], 'inputs': []}
    for item in sources:
        manifest = Path(item['archive_dir']) / 'source_manifest.json'
        if manifest.is_file():
            summary['inputs'].append({'source_id': item['source_id'],
                                      'source_manifest_sha256': sha256_file(manifest)})

    outputs = {}
    symbol_frames = pd.concat(symbols, ignore_index=True) if symbols else pd.DataFrame(
        columns=list(SYMBOL_COLUMNS))
    conflict_frames = []
    if not symbol_frames.empty:
        reuse = symbol_reuse_conflicts(symbol_frames)
        if reuse:
            conflict_frames.append(pd.DataFrame(
                [{'security_id': r['a'] + '|' + r['b'], 'field': 'symbol_reuse',
                  'sources': '', 'values': r['symbol']} for r in reuse]))
    if 'master' in tables:
        merged, conflicts = merge_sources(masters, priority)
        write_frame(merged, out / 'security_master_v2.csv', overwrite=overwrite)
        outputs['security_master_v2.csv'] = sha256_file(out / 'security_master_v2.csv')
        summary['master_rows'] = int(len(merged))
        summary['master_errors'] = validate_master(merged) if not merged.empty else []
        if not conflicts.empty:
            conflict_frames.append(conflicts)
    if 'symbols' in tables:
        merged_symbols = symbol_frames.drop_duplicates().sort_values(
            ['security_id', 'valid_from']).reset_index(drop=True)
        write_frame(merged_symbols, out / 'symbol_history.csv', overwrite=overwrite)
        outputs['symbol_history.csv'] = sha256_file(out / 'symbol_history.csv')
        summary['symbol_rows'] = int(len(merged_symbols))
        summary['symbol_errors'] = validate_symbols(merged_symbols) if not merged_symbols.empty else []
    if 'actions' in tables:
        merged_actions = pd.concat(actions, ignore_index=True) if actions else pd.DataFrame(
            columns=list(ACTION_COLUMNS))
        merged_actions = merged_actions.drop_duplicates().sort_values(
            ['security_id', 'ex_date']).reset_index(drop=True)
        write_frame(merged_actions, out / 'corporate_actions.csv', overwrite=overwrite)
        outputs['corporate_actions.csv'] = sha256_file(out / 'corporate_actions.csv')
        summary['action_rows'] = int(len(merged_actions))
    if 'master' in tables or 'symbols' in tables:
        all_conflicts = pd.concat(conflict_frames, ignore_index=True) if conflict_frames else pd.DataFrame(
            columns=['security_id', 'field', 'sources', 'values'])
        all_conflicts = all_conflicts[['security_id', 'field', 'sources', 'values']]
        write_frame(all_conflicts, out / 'master_conflicts.csv', overwrite=overwrite)
        outputs['master_conflicts.csv'] = sha256_file(out / 'master_conflicts.csv')
        summary['conflicts'] = int(len(all_conflicts))
    summary['outputs'] = outputs
    (out / 'import_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return summary
