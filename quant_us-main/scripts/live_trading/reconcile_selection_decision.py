#!/usr/bin/env python3
"""Selection shadow 自动对账；只读数据库和研究批次，不调用模型或执行器。"""
import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from scripts.live_trading.decision_ledger.decision_run_store import DecisionRunStore
from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.replay_decision import ReplayEngine

REQUIRED_SNAPSHOTS = ('selection_input', 'permission_snapshot', 'validated_decision')
FORBIDDEN_SIDE_EFFECTS = {'order_intent_created', 'order_submitted', 'fill_received',
                          'trade_opened', 'position_opened'}


def _codes(batch):
    values = []
    for key in ('candidates', 'exclusions'):
        for item in batch.get(key) or []:
            if isinstance(item, dict) and item.get('code'):
                values.append(item['code'])
    return values


def reconcile_selection(registry, batch: dict, decision_id: str = '') -> dict:
    decision_id = decision_id or batch.get('decision_id') or ''
    store = DecisionRunStore(registry)
    run = store.get_run(decision_id) if decision_id else None
    universe = list(dict.fromkeys(batch.get('universe') or []))
    ranked = _codes(batch)
    with store.events.transaction() as con:
        attempts = con.execute(
            'SELECT status FROM llm_model_attempts WHERE account_scope=? AND decision_id=?',
            (registry.namespace, decision_id)).fetchall()
        snapshot_counts = {}
        for kind in REQUIRED_SNAPSHOTS:
            snapshot_counts[kind] = int(con.execute(
                'SELECT COUNT(*) FROM decision_snapshots WHERE account_scope=? AND kind=? '
                'AND (id=? OR id=?)',
                (registry.namespace, kind, decision_id,
                 (run or {}).get('input_snapshot_id', ''))).fetchone()[0])
        event_rows = con.execute(
            'SELECT event_type,body FROM decision_events WHERE account_scope=?',
            (registry.namespace,)).fetchall()
    side_effects = []
    for event_type, body in event_rows:
        if event_type not in FORBIDDEN_SIDE_EFFECTS:
            continue
        try:
            event = json.loads(body)
        except Exception:
            continue
        if decision_id in json.dumps(event, ensure_ascii=False):
            side_effects.append(event_type)
    replay = ReplayEngine(registry=registry, store=store).validate(decision_id) if decision_id else {}
    completed = sum(1 for (status,) in attempts if status == 'completed')
    checks = {
        'batch_linked': bool(run and batch.get('decision_id') == decision_id and
                             run.get('subject_id') == batch.get('research_batch_id')),
        'role_validated': bool(run and run.get('role') == 'selection' and
                               run.get('status') == 'validated'),
        'universe_covered': bool(universe and len(ranked) == len(universe) and
                                 set(ranked) == set(universe) and len(ranked) == len(set(ranked))),
        'single_completed_attempt': len(attempts) == 1 and completed == 1,
        'snapshots_complete': all(snapshot_counts[k] == 1 for k in REQUIRED_SNAPSHOTS),
        'replay_valid': bool(replay.get('input_hash_match') and replay.get('validated') and
                             replay.get('network_used') is False),
        'shadow_effective_action': bool(
            batch.get('permission_level') == 'shadow' and
            batch.get('effective_action') == 'rule_ranking'),
        'no_order_side_effects': not side_effects,
    }
    return {
        'decision_id': decision_id,
        'research_batch_id': batch.get('research_batch_id'),
        'universe_count': len(universe), 'ranked_count': len(ranked),
        'attempt_count': len(attempts), 'completed_attempt_count': completed,
        'snapshots': snapshot_counts, 'replay': replay,
        'order_side_effects': len(side_effects),
        'order_side_effect_types': sorted(set(side_effects)),
        'checks': checks, 'passed': all(checks.values()),
    }


def _registry(config_path: str):
    import yaml
    with open(config_path, encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}
    engine = config.get('llm_decision', {}).get('engine_v2', {})
    return PositionRegistry(namespace=engine.get('account_scope', 'DRY-RUN'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Selection shadow 决策自动对账')
    parser.add_argument('--decision-id')
    parser.add_argument('--config', default=str(BASE_DIR / 'config.yaml'))
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    from scripts.live_trading.llm_suggestions.store import load_research_batches
    batches = load_research_batches()
    batch = next((b for b in reversed(batches)
                  if not args.decision_id or b.get('decision_id') == args.decision_id), None)
    if batch is None:
        result = {'decision_id': args.decision_id, 'error': 'research_batch_not_found',
                  'passed': False}
    else:
        result = reconcile_selection(_registry(args.config), batch, args.decision_id or '')
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get('passed') else 1


if __name__ == '__main__':
    sys.exit(main())
