"""P0/P1 CLI. Every run consumes a frozen local snapshot, never an LLM."""
import argparse
import json
from pathlib import Path

from . import manifest
from .experiments import audit, run


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', help='create an explicit DRAFT; never auto-select production experiment')
    for arg in ('study-id', 'baseline', 'start', 'end', 'output'):
        init.add_argument('--' + arg, required=True)
    init.add_argument('--prices', nargs='+', required=True)
    for arg in ('market', 'actions', 'quality'):
        init.add_argument('--' + arg, required=True)
    init.add_argument('--fee-bp', type=int, default=10)
    init.add_argument('--window-rationale', required=True,
                      help='必填：窗口为何是这个范围（§4.1 要求声明历史数据使用范围）')
    for cmd in ('audit', 'freeze', 'run', 'compare', 'report'):
        parser = sub.add_parser(cmd)
        parser.add_argument('--study', required=True)
        if cmd == 'freeze':
            parser.add_argument('--output', default=str(manifest.ROOT / 'data/strategy_diagnostics'))
        if cmd == 'run':
            parser.add_argument('--variant', choices=['baseline'], default='baseline')
    args = p.parse_args(argv)
    try:
        if args.command == 'init':
            if Path(args.output).exists():
                raise ValueError('DRAFT_ALREADY_EXISTS')
            data = manifest.draft(args.study_id, args.baseline,
                                  {'prices': args.prices, 'market': [args.market],
                                   'quality': [args.quality], 'actions': [args.actions]},
                                  args.start, args.end, fee_bp=args.fee_bp,
                                  window_rationale=args.window_rationale)
            if errors := manifest.audit_draft(data):
                raise ValueError(';'.join(errors))
            manifest.write_json(args.output, data)
            out = {'status': 'DRAFT_CREATED', 'path': str(Path(args.output).resolve())}
        elif args.command == 'freeze':
            out = {'status': 'FROZEN', 'path': str(manifest.freeze(manifest.read(args.study), args.output))}
        elif args.command == 'audit':
            data = manifest.read(args.study)
            if data.get('status') == 'DRAFT':
                errors = manifest.audit_draft(data)
                out = {'status': 'DRAFT_AUDIT', 'errors': errors}
                if errors:
                    print(json.dumps(out, ensure_ascii=False)); return 1
            else:
                out = audit(args.study)
        elif args.command == 'run':
            result = run(args.study, args.variant)
            out = {k: result[k] for k in ('status', 'study_id', 'sessions', 'verdict')}
        else:
            manifest.verify(args.study)
            root = Path(args.study).resolve().parent
            if not (root / 'complete.json').exists():
                raise ValueError('RUN_NOT_COMPLETE')
            completion = manifest.read(root / 'complete.json')
            if completion.get('comparison_hash') != manifest.file_hash(root / 'comparison.json'):
                raise ValueError('RESULT_CHANGED')
            result = manifest.read(root / 'comparison.json')
            if args.command == 'report':
                from .report import render
                print(render(result)); return 0
            out = {'verdict': result['verdict'],
                   'phase_conclusion': result.get('phase_conclusion'),
                   'checks': result['checks'],
                   'reason': 'P0/P1只有基线；无challenger，不能计算增量或晋级。'}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({'status': 'ENGINEERING_BLOCKED', 'reason': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
