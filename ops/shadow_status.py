#!/usr/bin/env python3
"""判定某次每日运行「到底完成了什么」。

**为什么需要它**：`exit 0` 和「进程启动了」都不足以证明 LLM 每日闭环正常 ——
无候选、无证据（模型根本没被调用）、模型失败、错过截止、真正做出了决策，
这五种情况在退出码上长得一模一样。本脚本把它们区分开，并把结果写成一行。

分类（按严重度从"没事发生"到"完成了"）：

    NO_OPPORTUNITIES  当天没有候选（设计 §4：不是失败，也不制造候选）
    NO_EVIDENCE       有候选但证据包都是 LLM_INSUFFICIENT —— 模型根本没被调用
    DEADLINE_MISSED   评审窗口已过，动作被冻结为 ABSTAIN/DECISION_DEADLINE_MISSED
    MODEL_FAILED      发起了模型调用但尝试以 FAILED/TIMED_OUT/UNKNOWN 收场
    DECIDED           动作已冻结（真实模型或夹具，看 model_id）
    SETTLED           该 session 的成交已结算入账

用法：
    python3 ops/shadow_status.py --manifest <冻结 manifest> --output <实验根> [--run-json <f>]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def classify(manifest_path: str, output: str, run_json: str | None = None) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'quant_us-main'))
    from scripts.portfolio_shadow.cli import manifest_from_dict, _opportunity_from_dict
    from scripts.portfolio_shadow.store import ShadowStore

    m = manifest_from_dict(json.loads(Path(manifest_path).read_text()))
    store = ShadowStore(Path(output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    run = _load(run_json) if run_json else None
    steps = (run or {}).get('steps', {})
    # 一次日运行做两件不同的事，**不能只看一个 session**：
    #   prepare 在信号日 T 发机会，机会排在 T+1 执行；
    #   review  评的是 T+1 那批（T 发的）。
    # 原先只看 run['session']（= T）去数 planned_execution_session==T 的机会，
    # 于是「今天发了 1 个候选」被报成「无候选」——正是它要防的那种误报。
    signal_session = (run or {}).get('session') or (steps.get('prepare') or {}).get('session')
    exec_session = ((run or {}).get('execution_session')
                    or (steps.get('review') or {}).get('execution_session'))
    if exec_session is None:
        exec_session = signal_session

    prepared = [_opportunity_from_dict(o) for o in store.opportunities()
                if o.get('signal_session') == signal_session]
    due = [_opportunity_from_dict(o) for o in store.opportunities()
           if o.get('planned_execution_session') == exec_session]
    l_scope = next((s for s in m.account_scopes if s.endswith(':L')), None)
    terminals = store.opportunity_terminals()

    kinds, model_ids, attempt_statuses, frozen = set(), set(), set(), 0
    no_evidence = 0
    for opp in due:
        oid = opp.opportunity_id()
        packet = store.packet_for_opportunity(oid) or {}
        level = (packet.get('data_quality') or {}).get('level')
        if level == 'LLM_INSUFFICIENT':
            no_evidence += 1
        app = store.application(l_scope, oid) if l_scope else None
        if app is None:
            continue
        kinds.add(app.get('reason_code') or '')
        if app.get('decision_frozen'):
            frozen += 1
        attempt = store.job_run(app['decision_id']) if app.get('decision_id') else None
        if attempt:
            attempt_statuses.add(attempt.get('status'))
            if attempt.get('model_id'):
                model_ids.add(str(attempt['model_id']))

    if not due:
        status = 'NO_OPPORTUNITIES'
    elif frozen == 0:
        status = 'NO_EVIDENCE' if no_evidence == len(due) else 'NO_DECISION'
    elif 'DECISION_DEADLINE_MISSED' in kinds:
        status = 'DEADLINE_MISSED'
    elif attempt_statuses & {'FAILED', 'TIMED_OUT', 'UNKNOWN'}:
        status = 'MODEL_FAILED'
    else:
        status = 'DECIDED'

    settled = (bool(store.executed_opportunities(m.account_scopes[0], exec_session))
               if exec_session else False)
    if status == 'DECIDED' and settled:
        status = 'SETTLED'

    return {
        'status': status, 'signal_session': signal_session,
        'execution_session': exec_session,
        'prepared_for_next': len(prepared), 'due': len(due),
        'frozen_applications': frozen,
        'no_evidence_packets': no_evidence,
        'l_actions': sorted(k for k in kinds if k),
        'attempt_statuses': sorted(attempt_statuses),
        'model_ids': sorted(model_ids),
        'review_skipped': ((run or {}).get('steps', {}).get('review') or {}).get('skipped', ''),
    }


def main(argv=None):
    p = argparse.ArgumentParser(description='判定某次每日运行的完成情况')
    p.add_argument('--manifest', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--run-json')
    p.add_argument('--json', action='store_true', help='输出完整 JSON 而非一行摘要')
    args = p.parse_args(argv)
    result = classify(args.manifest, args.output, args.run_json)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{result['status']} 信号日={result['signal_session']} "
              f"发文={result['prepared_for_next']} 执行日={result['execution_session']} "
              f"应评审={result['due']} "
              f"冻结={result['frozen_applications']} 包无证据={result['no_evidence_packets']} "
              f"模型={result['model_ids'] or '-'} 尝试={result['attempt_statuses'] or '-'}"
              f"{' 评审跳过=' + result['review_skipped'] if result['review_skipped'] else ''}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
