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
        listing=pd.to_datetime(record.get('listing_date'),errors='coerce')
        if pd.isna(listing) or listing.year<=1970:
            reasons.append('UNKNOWN_LISTING_DATE')
        elif not is_verified:
            reasons.append('LISTING_DATE_UNVERIFIED')
        if sec in unresolved or code in unresolved:
            reasons.append('CORPORATE_ACTION_UNRESOLVED')
        if sec in incomplete_actions or code in incomplete_actions:
            reasons.append('ACTION_HISTORY_INCOMPLETE')
        interval_start=max(begin,listing.normalize()) if pd.notna(listing) and listing.year>1970 else begin
        rows.append({'security_id':sec,'code':code,
            'from_session':interval_start.strftime('%Y-%m-%d'),
            'to_session':finish.strftime('%Y-%m-%d'),
            'quality_status':'verified' if not reasons else 'unverified',
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
