"""Reproducible first-milestone report. No invented counterfactual prices."""
import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def seconds(start, end):
    return max(0, (datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds())


def build_funnel(events):
    unique = {}
    legacy = 0
    for event in events:
        if not event.get('event_id') or event.get('schema_version') != 1:
            legacy += 1
            continue
        old = unique.get(event['event_id'])
        if old and old['payload_hash'] != event['payload_hash']:
            raise ValueError('事件导出内容冲突')
        unique[event['event_id']] = event
    events = sorted(unique.values(), key=lambda e: (e['observed_at'], e['event_id']))
    candidates = {e['signal_id']: e for e in events if e['event_type'] in ('rule_candidate','rule_rejected')}
    by_signal = defaultdict(list)
    for e in events:
        if e.get('signal_id'):
            by_signal[e['signal_id']].append(e)
    stages, cohorts, cross = Counter(), Counter(), Counter()
    latencies = defaultdict(list)
    rows = []
    for sid, candidate in sorted(candidates.items()):
        p = candidate['payload']
        cohort = '|'.join(str(v) for v in (candidate['account_scope'], p['strategy'], p['strategy_version'], p['timeframe']))
        cohorts[cohort] += 1
        evs = by_signal[sid]
        reviews = [e for e in evs if e['event_type'] in ('llm_completed','llm_failed')]
        props = [e for e in evs if e['event_type'] == 'proposal_created']
        humans = [e for e in evs if e['event_type'] == 'human_decision']
        intents = [e for e in evs if e['event_type'] == 'order_intent_created']
        fills = [e for e in evs if e['event_type'] == 'fill_received']
        expired = [e for e in evs if e['event_type'] == 'proposal_expired']
        orders = [e for e in evs if e['event_type'] in ('order_submitted','order_unknown','order_rejected')]
        rec = reviews[-1]['payload'].get('recommendation') if reviews else 'missing'
        action = humans[-1]['payload'].get('status') if humans else 'unhandled'
        execution = orders[-1]['payload']['status'] if orders else 'order_pending_or_unfilled'
        outcome = (execution if fills and execution=='filled' else execution+'_with_fill' if fills else
                   execution if intents else 'expired' if expired else action)
        stages['rule_passed' if p['passed'] else 'rule_rejected'] += 1
        stages['with_proposal'] += bool(props)
        stages['with_llm_result'] += bool(reviews)
        stages['llm_failed_or_stale'] += bool(reviews and reviews[-1]['payload']['status'] in ('failed','stale'))
        stages['llm_insufficient'] += bool(reviews and reviews[-1]['payload']['status'] == 'insufficient_information')
        stages['human_approved'] += action == 'approved'
        stages['human_rejected'] += action == 'rejected'
        stages['unhandled'] += bool(props and not humans)
        stages['with_order'] += bool(intents)
        stages['with_fill'] += bool(fills)
        stages['proposal_expired'] += bool(expired)
        stages['unknown_order'] += execution == 'unknown'
        stages['unfilled_terminal_order'] += bool(intents and not fills and execution in ('cancelled','rejected'))
        if p['passed']:
            cross[f'{rec}|{action}'] += 1
        for r in reviews:
            if r['payload'].get('latency_seconds') is not None:
                latencies['llm_seconds'].append(r['payload']['latency_seconds'])
        if humans and props:
            latencies['human_wait_seconds'].append(seconds(props[0]['observed_at'], humans[0]['observed_at']))
        if intents and fills:
            latencies['submit_to_first_fill_seconds'].append(seconds(intents[0]['observed_at'], fills[0]['observed_at']))
        rows.append(dict(signal_id=sid, cohort=cohort, llm=rec, human=action, outcome=outcome))
    trades = {}
    for event in events:
        if event['event_type'] == 'trade_accounted':
            trades[event['trade_id']] = event['payload']
    closed = [t for t in trades.values() if t['status'] == 'closed']
    r_values = [t['net_realized_pnl']/t['initial_r0'] for t in closed
                if t.get('net_realized_pnl') is not None and (t.get('initial_r0') or 0) > 0]
    costs = [e['payload'].get('cost_usd') for e in events if e['event_type'] in ('llm_completed','llm_failed')]
    linked = [e for e in events if e['event_type'] in ('proposal_created','llm_completed','llm_failed','human_decision','order_intent_created','fill_received')]
    complete = sum(bool(e.get('signal_id') in candidates and e.get('plan_id') and e.get('plan_version') and
                       (e.get('review_id') or e['event_type']=='proposal_created')) for e in linked)
    return dict(schema_version=1, scope='条件性对照；本报告不估算LLM因果贡献',
        candidates=len(candidates), stages=dict(sorted(stages.items())), cohorts=dict(sorted(cohorts.items())),
        legacy_unknown_events=legacy, unique_events=len(events),
        linkage={'complete': complete, 'total': len(linked), 'rate': complete/len(linked) if linked else None},
        latencies={k: {'samples':len(v),'mean':sum(v)/len(v),'max':max(v)} for k,v in sorted(latencies.items())},
        cross_groups=dict(sorted(cross.items())), rows=rows,
        actual={'open': sum(t['status']=='open' for t in trades.values()),
                'partially_closed': sum(t['status']=='partially_closed' for t in trades.values()), 'closed':len(closed),
                'net_expectancy_r':sum(r_values)/len(r_values) if r_values else None, 'r_samples':len(r_values),
                'net_pnl':sum(t['net_realized_pnl'] for t in closed) if closed and all(t.get('net_realized_pnl') is not None for t in closed) else None,
                'missing_fees':sum(not t['fee_complete'] for t in trades.values())},
        llm_cost={'known_usd':sum(c for c in costs if c is not None), 'unknown_calls':sum(c is None for c in costs)},
        conclusion='暂无法判断LLM是否提高收益；需要锁定实验版本、历史可得行情及受资金约束的样本外A/B/C/D重放。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', required=True, help='events-v1.jsonl export')
    parser.add_argument('--output')
    args = parser.parse_args()
    with open(args.events, encoding='utf-8') as f:
        events = [json.loads(line) for line in f if line.strip()]
    result = json.dumps(build_funnel(events), ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(result+'\n', encoding='utf-8')
    else:
        print(result)


if __name__ == '__main__':
    main()
