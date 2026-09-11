#!/usr/bin/env python3
"""不可覆盖的买入实验 manifest：冻结代码、配置、universe 和数据文件哈希。"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REQUIRED = ('experiment_id', 'document_version', 'git_commit', 'random_seed',
            'git_dirty', 'created_at', 'config', 'universe', 'data_files',
            'quality_files', 'periods', 'costs')

# 实验分组按固定顺序排列；只允许作为有序子集出现在命令行与 manifest 中。
GROUP_ORDER = ('A', 'B', 'C', 'D')
# 缺少 D 组（无真实历史时点 LLM 标签）时 manifest 固定登记的原因。
LLM_UNAVAILABLE_REASON = 'historical_point_in_time_labels_unavailable'


def parse_groups(spec):
    """把 `--groups` 的字符串解析为有序子集元组；非法输入抛 ValueError。"""
    if spec is None:
        return GROUP_ORDER
    if isinstance(spec, (list, tuple)):
        spec = ''.join(str(g) for g in spec)
    text = str(spec).strip().upper().replace(',', '').replace(' ', '')
    if not text:
        raise ValueError('--groups 不能为空')
    seen = []
    for char in text:
        if char not in GROUP_ORDER:
            raise ValueError(f'--groups 含非法分组 {char!r}，只允许 A/B/C/D')
        if char in seen:
            raise ValueError(f'--groups 含重复分组 {char!r}')
        seen.append(char)
    ordered = [g for g in GROUP_ORDER if g in seen]
    if seen != ordered:
        raise ValueError(f'--groups 必须按 A,B,C,D 顺序排列，收到 {text!r}')
    return tuple(seen)


def manifest_groups(manifest: dict):
    """读取 manifest 声明的实验分组；未登记时按历史默认 ABCD 处理。"""
    raw = (manifest or {}).get('experiment_groups')
    if not raw:
        return GROUP_ORDER
    return tuple(str(g).upper() for g in raw)


def default_llm_evaluation(groups):
    """D 组缺失时 LLM 增量不可判定；D 存在时标记为可评估。"""
    if 'D' in tuple(groups):
        return {'status': 'evaluated', 'reason': ''}
    return {'status': 'inconclusive', 'reason': LLM_UNAVAILABLE_REASON}


def check_groups_consistent(groups, manifest: dict):
    """命令行的分组必须与 manifest 冻结的分组完全一致。"""
    declared = manifest_groups(manifest)
    if tuple(groups) != declared:
        raise SystemExit(
            f'--groups {"".join(groups)} 与 manifest experiment_groups '
            f'{"".join(declared)} 不一致')


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def git_commit(root='.') -> str:
    result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def git_is_dirty(root='.') -> bool:
    result = subprocess.run(['git', 'status', '--porcelain'], cwd=root, check=True,
                            capture_output=True, text=True)
    return bool(result.stdout.strip())


def file_record(path) -> dict:
    p = Path(path).resolve()
    return {'path': str(p), 'size': p.stat().st_size, 'sha256': sha256_file(p)}


def build_manifest(experiment_id: str, config_path, universe_path,
                   data_paths: Iterable, periods: dict, quality_paths=(),
                   costs=(.001, .002, .005, .01),
                   random_seed=20260910, root='.',
                   experiment_groups=GROUP_ORDER, llm_evaluation=None) -> dict:
    groups = parse_groups(experiment_groups)
    if llm_evaluation is None:
        llm_evaluation = default_llm_evaluation(groups)
    return {
        'experiment_id': experiment_id,
        'document_version': 'buy-strategy-validation-plan-2026-09-10',
        'git_commit': git_commit(root), 'git_dirty': git_is_dirty(root),
        'random_seed': int(random_seed),
        'created_at': datetime.now(timezone.utc).isoformat(),
        'config': file_record(config_path), 'universe': file_record(universe_path),
        'data_files': [file_record(p) for p in sorted(map(str, data_paths))],
        'quality_files': [file_record(p) for p in sorted(map(str, quality_paths))],
        'periods': periods, 'costs': list(map(float, costs)),
        'experiment_groups': list(groups), 'llm_evaluation': llm_evaluation,
    }


def validate_manifest(manifest: dict, root='.', require_git=True) -> list:
    errors = [f'MISSING_{key}' for key in REQUIRED if key not in manifest]
    for key in ('config', 'universe'):
        record = manifest.get(key) or {}
        path = record.get('path')
        if not path or not Path(path).is_file():
            errors.append(f'{key.upper()}_MISSING')
        elif sha256_file(path) != record.get('sha256'):
            errors.append(f'{key.upper()}_HASH_MISMATCH')
    for record in manifest.get('data_files') or []:
        path = record.get('path')
        if not path or not Path(path).is_file():
            errors.append('DATA_FILE_MISSING')
        elif sha256_file(path) != record.get('sha256'):
            errors.append('DATA_HASH_MISMATCH')
    if not manifest.get('quality_files'):
        errors.append('QUALITY_FILES_MISSING')
    for record in manifest.get('quality_files') or []:
        path = record.get('path')
        if not path or not Path(path).is_file(): errors.append('QUALITY_FILE_MISSING')
        elif sha256_file(path) != record.get('sha256'): errors.append('QUALITY_HASH_MISMATCH')
    periods = manifest.get('periods') or {}
    if periods.get('development_end', '') >= periods.get('validation_start', '9999'):
        errors.append('PERIOD_OVERLAP_DEVELOPMENT_VALIDATION')
    if periods.get('validation_end', '') >= periods.get('test_start', '9999'):
        errors.append('PERIOD_OVERLAP_VALIDATION_TEST')
    if set(map(float, manifest.get('costs') or [])) != {.001, .002, .005, .01}:
        errors.append('COST_MATRIX_INCOMPLETE')
    if 'experiment_groups' in manifest:
        raw_groups = manifest.get('experiment_groups') or []
        try:
            parsed = parse_groups(raw_groups)
        except ValueError:
            errors.append('EXPERIMENT_GROUPS_INVALID')
        else:
            if list(parsed) != [str(g).upper() for g in raw_groups]:
                errors.append('EXPERIMENT_GROUPS_NOT_ORDERED_SUBSET')
            elif 'D' not in parsed:
                llm = manifest.get('llm_evaluation')
                if not isinstance(llm, dict):
                    errors.append('LLM_EVALUATION_MISSING')
                elif llm.get('status') != 'inconclusive':
                    errors.append('LLM_EVALUATION_MUST_BE_INCONCLUSIVE_WITHOUT_D')
    if require_git:
        try:
            if git_commit(root) != manifest.get('git_commit'):
                errors.append('GIT_COMMIT_MISMATCH')
            if manifest.get('git_dirty') or git_is_dirty(root):
                errors.append('GIT_WORKTREE_DIRTY')
        except Exception:
            errors.append('GIT_COMMIT_UNAVAILABLE')
    return sorted(set(errors))


def write_new_manifest(output_dir, manifest: dict) -> Path:
    out = Path(output_dir)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f'实验目录非空，禁止覆盖: {out}')
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'manifest.json'
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return path


def create_frozen_experiment(output_dir, experiment_id, config_path, universe_path,
                             data_paths, periods, root='.', quality_paths=(),
                             experiment_groups=GROUP_ORDER, llm_evaluation=None) -> Path:
    """复制小型冻结输入到实验目录，再基于最终路径生成 manifest。"""
    out=Path(output_dir)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f'实验目录非空，禁止覆盖: {out}')
    if git_is_dirty(root):
        raise RuntimeError('Git 工作区非干净状态，拒绝冻结正式实验')
    out.mkdir(parents=True,exist_ok=True)
    frozen_config=out/'config.yaml';frozen_universe=out/'universe.csv'
    shutil.copy2(config_path,frozen_config);shutil.copy2(universe_path,frozen_universe)
    manifest=build_manifest(experiment_id,frozen_config,frozen_universe,data_paths,periods,
                            quality_paths=quality_paths,root=root,
                            experiment_groups=experiment_groups,llm_evaluation=llm_evaluation)
    path=out/'manifest.json'
    path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return path


def main():
    p = argparse.ArgumentParser(description='创建或验证不可覆盖实验 manifest')
    p.add_argument('--validate')
    p.add_argument('--experiment-id', default='BUY-V2-EXP-001')
    p.add_argument('--config'); p.add_argument('--universe'); p.add_argument('--data', nargs='*')
    p.add_argument('--quality', nargs='+')
    p.add_argument('--groups', default='ABCD',
                   help='实验分组，A/B/C/D 的有序子集，默认 ABCD（例如 ABC）')
    p.add_argument('--output-dir'); p.add_argument('--root', default='.')
    args = p.parse_args()
    if args.validate:
        manifest = json.loads(Path(args.validate).read_text(encoding='utf-8'))
        errors = validate_manifest(manifest, args.root)
        print(json.dumps({'valid': not errors, 'errors': errors}, ensure_ascii=False, indent=2))
        return 0 if not errors else 1
    if not all((args.config, args.universe, args.output_dir, args.quality)):
        p.error('创建模式需要 --config --universe --quality --output-dir')
    try:
        groups = parse_groups(args.groups)
    except ValueError as exc:
        p.error(str(exc))
    periods = {'development_start': '2016-01-01', 'development_end': '2020-12-31',
               'validation_start': '2021-01-01', 'validation_end': '2023-12-31',
               'test_start': '2024-01-01', 'test_end': '2026-08-31'}
    try:
        path=create_frozen_experiment(args.output_dir,args.experiment_id,args.config,
                                      args.universe,args.data or [],periods,args.root,args.quality,
                                      experiment_groups=groups)
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    print(path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
