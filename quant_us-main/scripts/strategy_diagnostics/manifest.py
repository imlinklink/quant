"""Freeze copies of inputs, never read or migrate a production ledger."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from scripts.live_trading.decision_ledger.event_store import digest
from scripts.portfolio_shadow.cli import manifest_from_dict

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = 'strategy-diagnostic-v1'


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def code_hashes():
    folders = ('scripts/strategy_diagnostics', 'scripts/portfolio_shadow',
               'scripts/medium_term', 'scripts/data', 'scripts/live_trading/decision_ledger')
    paths = sorted({p for folder in folders for p in (ROOT / folder).rglob('*.py')})
    paths += [ROOT / 'scripts/live_trading/position_registry.py']
    return {str(p.relative_to(ROOT)): file_hash(p) for p in paths}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n'
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     delete=False) as stream:
        stream.write(data)
        temp = Path(stream.name)
    temp.replace(path)


def draft(study_id, baseline, inputs, start, end, *, fee_bp=10, window_rationale=''):
    """P0/P1 is historical reconstruction; validation has not been scheduled.

    `window_rationale` 不是装饰：§4.1 要求在冻结时声明**历史数据使用范围**。没有它，
    manifest 里只有两个日期，读的人无法判断这个窗口是"按共同覆盖范围选的"还是随手定的
    （2026-09-20 实测的前一个 study 就只覆盖 19 个交易日，产出 1 笔交易 —— 诊断没有检验力，
    而 manifest 里看不出那是刻意的还是失误）。
    """
    return {'schema_version': SCHEMA, 'study_id': study_id, 'status': 'DRAFT',
            'phase': 'P0_P1', 'baseline_manifest_path': str(Path(baseline).resolve()),
            'inputs': {k: [str(Path(p).resolve()) for p in v] for k, v in inputs.items()},
            'research_window': {'start': start, 'end': end},
            'research_window_rationale': window_rationale,
            'validation_window': None, 'validation_kind': 'not_scheduled',
            'prior_exposure_to_validation': 'historical_data_is_exploratory',
            'initial_state_policy': 'flat_common_start',
            'primary_metric': 'full_cost_terminal_return_delta',
            'cost_policy': {'fee_bp': fee_bp, 'slippage_bp': 0,
                            'note': '影子基线固定每腿费用；未建模滑点，不是可上线收益证明'},
            'statistical_protocol': {'mode': 'descriptive_only_no_promotion'},
            'trial_registry_path': 'trial_registry.jsonl'}


def audit_draft(data):
    import pandas as pd
    errors = []
    if data.get('schema_version') != SCHEMA or data.get('phase') != 'P0_P1':
        errors.append('UNSUPPORTED_STUDY_SCHEMA_OR_PHASE')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', str(data.get('study_id', ''))):
        errors.append('INVALID_STUDY_ID')
    if data.get('initial_state_policy') != 'flat_common_start':
        errors.append('UNSUPPORTED_INITIAL_STATE')
    if data.get('validation_kind') != 'not_scheduled' or data.get('validation_window') is not None:
        errors.append('P0_P1_FORBIDS_VALIDATION_CLAIM')
    try:
        window = data['research_window']
        start, end = pd.Timestamp(window['start']), pd.Timestamp(window['end'])
        if pd.isna(start) or pd.isna(end) or start > end or start.tzinfo or end.tzinfo:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        errors.append('INVALID_WINDOW')
    # §4.1 要求在冻结时声明**历史数据使用范围**。不给理由的窗口是个无法复核的选择 ——
    # 前一个 study 只覆盖 19 个交易日、产出 1 笔交易，而 manifest 里看不出原因。
    if not str(data.get('research_window_rationale') or '').strip():
        errors.append('RESEARCH_WINDOW_RATIONALE_MISSING')
    cost = data.get('cost_policy', {})
    if type(cost.get('fee_bp')) is not int or cost.get('fee_bp', -1) < 0 or cost.get('slippage_bp') != 0:
        errors.append('INVALID_OR_UNSUPPORTED_COST')
    inputs = data.get('inputs', {})
    if set(inputs) != {'prices', 'market', 'quality', 'actions'}:
        errors.append('INPUT_KINDS_MISMATCH')
    for kind, paths in inputs.items():
        if not isinstance(paths, list) or not paths or (kind != 'prices' and len(paths) != 1):
            errors.append(f'INVALID_INPUT_LIST:{kind}')
            continue
        for path in paths:
            if not Path(path).is_file():
                errors.append(f'INPUT_MISSING:{path}')
    try:
        baseline = manifest_from_dict(read(data['baseline_manifest_path']))
        errors.extend(baseline.validate())
        if baseline.execution_policy.get('entry_rule') != 'b3':
            errors.append('BASELINE_ENTRY_UNSUPPORTED')
        if baseline.execution_policy.get('exit_policy_id') != f"H{baseline.execution_policy.get('horizon')}":
            errors.append('BASELINE_EXIT_UNSUPPORTED')
    except (KeyError, ValueError, TypeError, OSError) as exc:
        errors.append(f'BASELINE_INVALID:{exc}')
    return errors


def freeze(data, output):
    errors = audit_draft(data)
    if errors:
        raise ValueError(';'.join(errors))
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / data['study_id']
    if target.exists():
        raise ValueError(f'STUDY_ALREADY_EXISTS:{target}')
    # Construct everything privately, then publish the whole frozen snapshot at once.
    with tempfile.TemporaryDirectory(prefix='.freeze-', dir=output) as temp:
        stage = Path(temp) / data['study_id']
        stage.mkdir()
        frozen = dict(data, status='FROZEN')
        records = {}
        sources = dict(data['inputs'], baseline=[data['baseline_manifest_path']])
        for kind, paths in sources.items():
            records[kind] = []
            for i, source in enumerate(paths):
                source = Path(source)
                relative = f'inputs/{kind}/{i}-{source.name}'
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                before = file_hash(source)
                shutil.copyfile(source, destination)
                if file_hash(destination) != before or file_hash(source) != before:
                    raise ValueError('INPUT_CHANGED_DURING_FREEZE')
                records[kind].append({'path': relative, 'sha256': before})
        frozen['input_index'] = records
        frozen['code_hashes'] = code_hashes()
        frozen['working_tree_hash'] = digest(frozen['code_hashes'])
        frozen['code_revision'] = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        baseline = read(stage / records['baseline'][0]['path'])
        frozen['baseline_manifest_hash'] = records['baseline'][0]['sha256']
        frozen['risk_policy'] = baseline['risk_policy']
        frozen['execution_policy'] = baseline['execution_policy']
        frozen['entry_policy'] = baseline['execution_policy']['entry_rule']
        frozen['universe_snapshot_hash'] = digest(records['prices'])
        frozen['calendar_hash'] = digest(records['market'])
        for kind, field in [('prices', 'prices_hash'), ('quality', 'quality_hash'),
                            ('actions', 'corporate_actions_hash')]:
            frozen[field] = digest(records[kind])
        frozen['manifest_hash'] = digest(frozen)
        write_json(stage / 'study_manifest.json', frozen)
        write_json(stage / 'input_index.json', records)
        (stage / 'trial_registry.jsonl').write_text(json.dumps(
            {'event': 'study_frozen', 'study_id': data['study_id'],
             'variant': 'baseline', 'manifest_hash': frozen['manifest_hash']}) + '\n')
        stage.rename(target)
    return target / 'study_manifest.json'


def verify(path):
    path = Path(path).resolve()
    data = read(path)
    if data.get('status') != 'FROZEN':
        raise ValueError('STUDY_NOT_FROZEN')
    if digest({k: v for k, v in data.items() if k != 'manifest_hash'}) != data.get('manifest_hash'):
        raise ValueError('STUDY_MANIFEST_TAMPERED')
    if data['code_hashes'] != code_hashes():
        raise ValueError('STUDY_CODE_CHANGED:freeze a new study')
    for records in data['input_index'].values():
        for item in records:
            source = (path.parent / item['path']).resolve()
            if path.parent not in source.parents or file_hash(source) != item['sha256']:
                raise ValueError(f'STUDY_INPUT_CHANGED:{item["path"]}')
    return data
