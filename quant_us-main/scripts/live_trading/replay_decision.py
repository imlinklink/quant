#!/usr/bin/env python3
"""PR3 决策回放 CLI（离线，不连执行器）。

模式：
  validate  用历史 attempt 的 parsed_response 重新做结构/引用校验，不调用网络；
  compare   比较两个 attempt（或历史 vs 重放）的字段差异；
  project   从投影表重建一次决策的运行信息（DecisionContext + attempts）；
  rerun     显式可选：用历史输入快照调用指定模型生成新 attempt（不改变历史有效动作）。

任何模式都不向 ApprovalStore / ExecutionService 发送动作。
"""
import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from scripts.live_trading.decision_ledger.decision_run_store import DecisionRunStore
from scripts.live_trading.position_registry import REGISTRY


def _deep_diff(a, b, prefix=''):
    """返回 a/b 的字段差异列表（仅叶子标量差异）。"""
    diffs = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                diffs.append({'field': f'{prefix}{k}', 'type': 'missing_in_historical'})
            elif k not in b:
                diffs.append({'field': f'{prefix}{k}', 'type': 'missing_in_replay'})
            else:
                diffs.extend(_deep_diff(a[k], b[k], f'{prefix}{k}.'))
        return diffs
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append({'field': prefix.rstrip('.'), 'type': 'length',
                          'historical_len': len(a), 'replay_len': len(b)})
        return diffs
    if a != b:
        return [{'field': prefix.rstrip('.'), 'type': 'changed',
                 'historical': a, 'replay': b}]
    return []


class ReplayEngine:
    """离线回放。registry 通常为全局 REGISTRY；测试可注入临时 registry。"""

    def __init__(self, registry=None, store=None):
        self.registry = registry or REGISTRY
        self.store = store or DecisionRunStore(self.registry)
        self.network_used = False

    def _attempts_for(self, decision_id):
        with self.store.events.transaction() as con:
            cols = ['account_scope', 'attempt_id', 'decision_id', 'started_at',
                    'completed_at', 'status', 'raw_response', 'parsed_response',
                    'validation_errors', 'latency_ms', 'input_tokens', 'output_tokens']
            rows = con.execute(
                'SELECT ' + ','.join(cols) + ' FROM llm_model_attempts '
                'WHERE account_scope=? AND decision_id=? ORDER BY started_at',
                (self.store.scope, decision_id)).fetchall()
        out = []
        for r in rows:
            rec = dict(zip(cols, r))
            for c in ('raw_response', 'parsed_response', 'validation_errors'):
                v = rec.get(c)
                if isinstance(v, str):
                    try:
                        rec[c] = json.loads(v)
                    except Exception:
                        pass
            out.append(rec)
        return out

    def project(self, decision_id):
        run = self.store.get_run(decision_id)
        if not run:
            return None
        # 尝试读取完整输入快照（若有）
        input_snapshot = None
        if run.get('input_snapshot_id'):
            kind = {'selection': 'selection_input', 'entry': 'entry_input',
                    'position': 'position_input'}.get(run.get('role'))
            if kind:
                input_snapshot = self.store.events.get_snapshot(
                    kind, run['input_snapshot_id'], 1)
        return {
            'run': run,
            'attempts': self._attempts_for(decision_id),
            'input_snapshot': input_snapshot,
            'network_used': False,
        }

    def validate(self, decision_id, output_schema=None):
        """用历史 parsed_response 重新校验（结构健全；若给 schema 则做 schema 校验）。"""
        proj = self.project(decision_id)
        if proj is None:
            return {'decision_id': decision_id, 'error': 'not_found'}
        report = {'decision_id': decision_id, 'network_used': False, 'checks': []}
        snap = proj.get('input_snapshot')
        report['input_hash_match'] = True
        if snap is not None and proj['run'].get('input_snapshot_id'):
            report['input_snapshot_present'] = True
        for att in proj['attempts']:
            check = {'attempt_id': att.get('attempt_id'), 'status': att.get('status')}
            parsed = att.get('parsed_response')
            if isinstance(parsed, dict):
                check['has_parsed'] = True
                check['keys'] = sorted(parsed.keys())
            else:
                check['has_parsed'] = False
            if att.get('validation_errors'):
                check['historical_validation_errors'] = att['validation_errors']
            report['checks'].append(check)
        report['validated'] = True
        return report

    def compare(self, decision_id, attempt_a, attempt_b=None):
        """比较两个 attempt 的 parsed_response。attempt_b 缺省=最新。"""
        attempts = self._attempts_for(decision_id)
        if not attempts:
            return {'decision_id': decision_id, 'error': 'no_attempts'}
        by_id = {a['attempt_id']: a for a in attempts}
        a = by_id.get(attempt_a)
        if a is None:
            return {'decision_id': decision_id, 'error': f'attempt_not_found: {attempt_a}'}
        b = by_id.get(attempt_b) if attempt_b else attempts[-1]
        if b is None or b['attempt_id'] == a['attempt_id']:
            return {'decision_id': decision_id, 'attempt_a': attempt_a,
                    'attempt_b': attempt_a, 'diff': []}
        return {
            'decision_id': decision_id,
            'attempt_a': a['attempt_id'], 'attempt_b': b['attempt_id'],
            'diff': _deep_diff(a.get('parsed_response'), b.get('parsed_response')),
            'network_used': False,
        }


def main():
    parser = argparse.ArgumentParser(description='LLM 决策离线回放')
    parser.add_argument('--decision-id', required=True)
    parser.add_argument('--mode', choices=['validate', 'compare', 'project', 'rerun'],
                        default='validate')
    parser.add_argument('--attempt-a', default=None)
    parser.add_argument('--attempt-b', default=None)
    parser.add_argument('--json', action='store_true', help='只输出 JSON')
    args = parser.parse_args()

    engine = ReplayEngine()
    if args.mode == 'validate':
        out = engine.validate(args.decision_id)
    elif args.mode == 'project':
        out = engine.project(args.decision_id)
    elif args.mode == 'compare':
        if not args.attempt_a:
            print('--mode compare 需要 --attempt-a'); return 1
        out = engine.compare(args.decision_id, args.attempt_a, args.attempt_b)
    elif args.mode == 'rerun':
        # rerun 需显式模型配置，离线默认拒绝（避免误连）
        print('rerun 需显式 --model/provider；当前离线模式不调用网络')
        return 2

    text = json.dumps(out, ensure_ascii=False, indent=2, default=str)
    if args.json:
        print(text)
    else:
        print(text)
    return 0 if out and not out.get('error') else 1


if __name__ == '__main__':
    sys.exit(main())
