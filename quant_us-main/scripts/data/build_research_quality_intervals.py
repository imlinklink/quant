#!/usr/bin/env python3
"""把证券审计结果转换成 raw_asof 实验使用的逐证券质量区间。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from scripts.data.io_utils import read_frame,sha256_file,write_frame


def _listing_verdict(record, begin, finish):
    """判断「这只证券在窗口内是否已上市」，并给出可复核的依据。

    原判据是 `listing.year <= 1970` —— 那是**识别哨兵值**的写法，却被当成了窗口问题的
    答案，于是两类完全不同的事被判成同一件：

    · **知道它在窗口开始前就已上市**：富途对老公司返回 `1970-01-01` 哨兵、`listing_date_quality
      == 'listed_on_or_before_history_start'`，管线用首根日线把它修正成数据起点。对任何从
      `begin` 起的窗口，这是一条**充分**的依据；
    · **真的不知道**：既没有日期，也没有可用的下界。

    实测（2026-09-20）：39 只宇宙里 **18 只属于第一类**（XOM/KO/JNJ/PG/CAT/CVX/DIS/JPM/
    NEE/DUK/AMT/AVGO/COHR/SHW/PLD/AXTI/NBIS/SPY），它们因为**与窗口无关的理由**
    被整段排除；而 `year<=1970` 同时也识别不出"真在 1970 年前上市"的公司。

    返回 `(basis, start_ts, reason)`：`reason is None` 表示可用。
    `basis` 如实写进输出 —— 下界就是下界，不能读成"上市日"。
    """
    listing = pd.to_datetime(record.get('listing_date'), errors='coerce')
    placeholder = record.get('listing_placeholder')
    is_placeholder = placeholder is True or str(placeholder).strip().lower() in ('true', '1')
    quality = str(record.get('listing_date_quality') or '').strip()
    has_real_date = pd.notna(listing) and not is_placeholder and listing.year > 1970
    if has_real_date:
        # 上市日晚于窗口结束 ⇒ 整个窗口内都没上市。原实现会给出 from > to 的空区间，
        # 下游按 (from, to) 比对时**静默永不匹配**，看不出是"没上市"。
        if listing.normalize() > finish:
            return 'reported', None, 'LISTED_AFTER_WINDOW'
        return 'reported', listing.normalize(), None
    if quality == 'listed_on_or_before_history_start':
        lower = listing.normalize() if pd.notna(listing) else begin
        if lower <= begin:
            return 'listed_on_or_before_history_start', begin, None
        return 'listed_on_or_before_history_start', lower, 'LISTING_LOWER_BOUND_AFTER_WINDOW'
    return 'unknown', None, 'UNKNOWN_LISTING_DATE'


def build_intervals(audit, symbols, start, end, unresolved=(), incomplete_actions=()):
    required_audit={'code','asset_type_audited','listing_date','verified'}
    required_symbols={'security_id','symbol','valid_from','valid_to'}
    if not required_audit.issubset(audit):
        raise ValueError('AUDIT_MISSING_FIELDS:'+','.join(sorted(required_audit-set(audit))))
    if not required_symbols.issubset(symbols):
        raise ValueError('SYMBOLS_MISSING_FIELDS:'+','.join(sorted(required_symbols-set(symbols))))
    begin=pd.Timestamp(start).normalize(); finish=pd.Timestamp(end).normalize()
    if begin>finish: raise ValueError('INVALID_QUALITY_WINDOW')
    unresolved=set(map(str,unresolved)); incomplete_actions=set(map(str,incomplete_actions)); rows=[]
    s=symbols.copy();s['symbol']=s.symbol.astype(str);s['security_id']=s.security_id.astype(str)
    for record in audit.to_dict('records'):
        code=str(record['code']); matches=s[s.symbol==code]
        if len(matches)!=1:
            raise ValueError(f'SYMBOL_MAPPING_NOT_UNIQUE:{code}:{len(matches)}')
        sec=str(matches.iloc[0].security_id); reasons=[]
        if str(record.get('asset_type_audited','')).lower()!='stock':
            reasons.append('ASSET_TYPE_NOT_STOCK')
        verified=record.get('verified')
        is_verified=(verified is True or str(verified).strip().lower() in ('true','1'))
        basis, listing_start, listing_reason = _listing_verdict(record, begin, finish)
        if listing_reason:
            reasons.append(listing_reason)
        elif basis == 'reported' and not is_verified:
            reasons.append('LISTING_DATE_UNVERIFIED')
        if sec in unresolved or code in unresolved:
            reasons.append('CORPORATE_ACTION_UNRESOLVED')
        if sec in incomplete_actions or code in incomplete_actions:
            reasons.append('ACTION_HISTORY_INCOMPLETE')
        interval_start=max(begin, listing_start) if listing_start is not None else begin
        rows.append({'security_id':sec,'code':code,
            'from_session':interval_start.strftime('%Y-%m-%d'),
            'to_session':finish.strftime('%Y-%m-%d'),
            'quality_status':'verified' if not reasons else 'unverified',
            # 上市日的**依据**（reported / listed_on_or_before_history_start / unknown）：
            # 不写出来的话，一条"下界"会被下游读成"上市日"，而那正是这类偏差的藏身处。
            'listing_basis':basis,
            'reason':';'.join(reasons)})
    return pd.DataFrame(rows).sort_values(['security_id','from_session']).reset_index(drop=True)


def main():
    p=argparse.ArgumentParser(description='构造 raw_asof 实验质量区间')
    p.add_argument('--audit',required=True);p.add_argument('--symbols',required=True)
    p.add_argument('--from-session',required=True);p.add_argument('--to-session',required=True)
    p.add_argument('--unresolved-security-id',action='append',default=[])
    p.add_argument('--incomplete-action-security-id',action='append',default=[])
    p.add_argument('--output',required=True);args=p.parse_args()
    out=build_intervals(read_frame(args.audit),read_frame(args.symbols),args.from_session,
                        args.to_session,args.unresolved_security_id,
                        args.incomplete_action_security_id)
    target=write_frame(out,args.output)
    print({'rows':len(out),'verified':int(out.quality_status.eq('verified').sum()),
           'rejected':int(out.quality_status.ne('verified').sum()),
           'audit_sha256':sha256_file(args.audit),'symbols_sha256':sha256_file(args.symbols),
           'output':str(target)})
    return 0


if __name__=='__main__':raise SystemExit(main())
