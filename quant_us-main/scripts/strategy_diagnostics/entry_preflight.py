"""H-A preflight: audit frozen evidence without computing counterfactual returns."""
import argparse
import json
from pathlib import Path

from .manifest import file_hash, read, write_json
from scripts.live_trading.decision_ledger.event_store import digest


def evaluate(registration, study, checks, observations):
    blockers = []
    if study.get('study_id') != registration['study_basis'] or study.get('manifest_hash') != registration['study_manifest_hash']:
        blockers.append('REGISTERED_BASELINE_MISMATCH')
    if checks.get('baseline_parity', {}).get('status') != 'VERIFIED':
        blockers.append('BASELINE_PARITY_NOT_VERIFIED')
    cash = checks.get('uncovered_by_parity', {}).get('dividend_receivable_outstanding')
    if cash is None:
        blockers.append('DIVIDEND_SETTLEMENT_AUDIT_MISSING')
    elif cash.get('stuck_within_window'):
        blockers.append('DIVIDENDS_STUCK_ON_NON_SESSION_PAY_DATES')
    cutoff = registration['maturity']['common_candidate_cutoff']
    executed, rejected = set(), set()
    for row in observations:
        if row['stage'] != 'account_execution' or row['candidate_round'] > cutoff:
            continue
        (executed if row['result'] == 'pass' else rejected).add(row['candidate_id'])
    return {'registration_id': registration['registration_id'],
            'status': 'ENGINEERING_BLOCKED' if blockers else 'PREFLIGHT_PASSED',
            'blockers': blockers, 'dividend_settlement': cash,
            'sample_inventory': {'baseline_account_executed': len(executed),
                                 'baseline_account_rejected': len(rejected),
                                 'baseline_ready': len(executed | rejected)},
            'population_note': '账户成交子集仅用于成交复现控制；机会级总体必须包含账户拒绝的 READY 候选。',
            'returns_computed': False, 'account_experiment_started': False,
            'note': '归档证据审计不等于当前代码重放。工程阻塞消除后须重冻并重新验证基线。'}


def run(registration_path, study_dir, output):
    registration_path, root, output = map(Path, (registration_path, study_dir, output))
    if output.exists():
        raise ValueError('PREFLIGHT_OUTPUT_EXISTS')
    registration, study = read(registration_path), read(root / 'study_manifest.json')
    if digest({k:v for k,v in study.items() if k != 'manifest_hash'}) != study['manifest_hash']:
        raise ValueError('STUDY_MANIFEST_TAMPERED')
    for records in study['input_index'].values():
        for item in records:
            source = (root / item['path']).resolve()
            if root.resolve() not in source.parents or file_hash(source) != item['sha256']:
                raise ValueError('STUDY_INPUT_CHANGED')
    result = evaluate(registration, study, read(root/'checks.json'), read(root/'funnel_observations.json'))
    result['evidence_sha256'] = {str(p):file_hash(p) for p in
        (registration_path, root/'study_manifest.json',root/'checks.json',root/'funnel_observations.json')}
    write_json(output,result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registration', required=True)
    parser.add_argument('--study', required=True)
    parser.add_argument('--out', required=True)
    args=parser.parse_args()
    result=run(args.registration,args.study,args.out)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 1 if result['blockers'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
