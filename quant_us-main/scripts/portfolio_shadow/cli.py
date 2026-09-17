"""R/L 双影子账户 CLI：validate / freeze / run-session / replay / report。

manifest.json 金额用美元（initial_cash 用美元）；schedule.json 为逐 session 的
bars/公司行动/机会排程。金额在进入引擎前转 int 微美元。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.live_trading.decision_ledger.event_store import stable_id

from .candidate_adapter import adapt_schedule, intents_for_session
from .evidence import (build_entry_packet, entry_decision_cutoff, entry_response_deadline,
                       events_from_records)
from .llm_overlay import (FakeModel, OverlayDecision, RealModel, decide_overlay,
                          model_call_expected)
from .paper_engine import new_account_state, step
from .replay import replay
from .schema import Application, Manifest, to_micro
from .store import ShadowStore, state_from_dict


def manifest_from_dict(d: dict) -> Manifest:
    return Manifest(
        experiment_id=d['experiment_id'], status=d.get('status', 'DRAFT'),
        parent_strategy_id=d['parent_strategy_id'], parent_version=d['parent_version'],
        parent_code_hash=d['parent_code_hash'], universe_id=d['universe_id'],
        universe_hash=d['universe_hash'], account_scopes=tuple(d['account_scopes']),
        initial_cash=to_micro(d['initial_cash']), currency=d.get('currency', 'USD'),
        risk_policy=d['risk_policy'], execution_policy=d['execution_policy'],
        llm_policy=d['llm_policy'], calendar_version=d['calendar_version'],
        data_hashes=d.get('data_hashes', {}), evaluation_protocol=d.get('evaluation_protocol', {}),
        start_session=d.get('start_session'))


def _manifest_to_public(m: Manifest) -> dict:
    d = dict(m.__dict__)
    d['account_scopes'] = list(m.account_scopes)
    d['initial_cash'] = float(m.initial_cash) / 1_000_000
    return d


def cmd_validate(args):
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    errors = m.validate()
    print(json.dumps({'experiment_id': m.experiment_id, 'errors': errors}, ensure_ascii=False))
    return 1 if errors else 0


def cmd_freeze(args):
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    frozen = m.freeze(args.start_session)
    out_dir = Path(args.output) / frozen.experiment_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'manifest.json').write_text(
        json.dumps(_manifest_to_public(frozen), ensure_ascii=False, indent=2) + '\n')
    ShadowStore(out_dir / 'ledger.sqlite3', frozen.experiment_id).save_experiment(frozen)
    print(json.dumps({'experiment_id': frozen.experiment_id, 'status': frozen.status,
                      'manifest_hash': frozen.manifest_hash()}, ensure_ascii=False))
    return 0


def cmd_run_session(args):
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    data = json.loads(Path(args.schedule).read_text())
    sess = next(s for s in data['sessions'] if s['session'] == args.session)
    opportunities = adapt_schedule(m.experiment_id, m.parent_version,
                                   m.execution_policy['entry_rule'],
                                   m.execution_policy['exit_policy_id'],
                                   sess.get('opportunities', []))
    for o in opportunities:
        store.put_opportunity(o)
    intents = intents_for_session(opportunities, args.session)
    bars = {sid: {k: to_micro(v) for k, v in b.items()} for sid, b in sess['bars'].items()}
    deadline = sess.get('deadline', args.session + 'T13:20:00+00:00')
    overlay = m.llm_policy.get('overlay', 'fixed_pass')
    results = {}
    for scope in m.account_scopes:
        row = store.latest_state(scope)
        saved = row[1] if row else None
        state = state_from_dict(saved) if saved else new_account_state(scope, m.initial_cash)
        scope_intents = list(intents)
        cost = 0
        uncertain = []
        # 真实模型（DeepSeek）：llm_policy.use_real_model 时从 config.yaml 构造
        real_model = None
        if overlay == 'entry_veto' and m.llm_policy.get('use_real_model'):
            import yaml
            from mutifactor.llm import LLMAdvisor
            cfg_path = Path(__file__).resolve().parents[2] / 'config.yaml'
            llm_cfg = (yaml.safe_load(cfg_path.read_text()) or {}).get('llm', {})
            real_model = RealModel(LLMAdvisor(llm_cfg),
                                   knowledge_cutoff=m.llm_policy.get('knowledge_cutoff'))
        # L 侧 overlay：entry_veto 时构建 packet → 复用或决定 → VETO/BLOCK 剔除 + 计成本
        if overlay == 'entry_veto' and scope.endswith(':L'):
            kept = []
            for item, opp in zip(sess.get('opportunities', []), opportunities):
                packet = build_entry_packet(
                    opp, item.get('quote') or {}, item.get('events', []),
                    item.get('fundamentals', {}), deadline,
                    model_knowledge_cutoff=m.llm_policy.get('knowledge_cutoff'))
                attempt_id = stable_id('llm_attempt', scope, opp.opportunity_id(),
                                       packet['packet_id'])
                cfg = item.get('model') or {}
                d = _reuse_or_decide(
                    store, scope, opp, packet, attempt_id, deadline,
                    lambda cfg=cfg: real_model if real_model is not None else FakeModel(
                        action=cfg.get('action', 'PASS'), reason_code=cfg.get('reason_code', ''),
                        evidence_ids=cfg.get('evidence_ids', []),
                        status=cfg.get('status', 'OK'),
                        completed_at=cfg.get('completed_at', deadline),
                        cost_micro=cfg.get('cost_micro', 0)),
                    force_recall=False)
                cost += d.model_cost
                if d.cost_uncertain and d.attempt_id:
                    uncertain.append(d.attempt_id)
                store.put_application(Application(
                    scope=scope, opportunity_id=opp.opportunity_id(), action=d.action,
                    reason_code=d.reason_code, decision_id='', as_of=deadline, applied=True,
                    model_cost=d.model_cost, raw_action=d.raw_action,
                    late_response_observed=d.late_response_observed,
                    cost_uncertain=d.cost_uncertain, attempt_id=d.attempt_id))
                if d.action not in ('VETO', 'BLOCK'):
                    kept.append(opp)
            scope_intents = kept
        res = step(state, session=args.session, bars=bars,
                   corporate_actions=sess.get('corporate_actions', []),
                   intents=scope_intents, manifest=m, model_cost=cost,
                   model_cost_uncertain=tuple(uncertain))
        if res.nav is None:
            # 幂等：该 session 已处理
            results[scope] = {'status': 'already_processed', 'session': args.session,
                              'model_cost': res.state.model_cost,
                              'positions': list(res.state.positions)}
            continue
        store.save_state(scope, res.state, res.nav, res.events)
        results[scope] = {'equity': res.nav['equity'], 'full_cost_equity': res.nav['full_cost_equity'],
                          'model_cost': res.state.model_cost, 'positions': list(res.state.positions)}
    print(json.dumps(results, ensure_ascii=False))
    return 0


def cmd_replay(args):
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    out = {}
    for scope in m.account_scopes:
        state = replay(scope, m.initial_cash, store.events(scope))
        row = store.latest_state(scope)
        saved = row[1] if row else None
        saved_hash = state_from_dict(saved).state_hash() if saved else None
        out[scope] = {'replayed_hash': state.state_hash(), 'saved_hash': saved_hash,
                      'match': saved_hash is not None and state.state_hash() == saved_hash}
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_report(args):
    from .report import daily_report, paired_performance, render_markdown
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    report = daily_report(store, m)
    paired = paired_performance(store, m)
    out_dir = Path(args.output) / m.experiment_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'summary.json').write_text(
        json.dumps({'daily_report': report, 'paired_performance': paired},
                   ensure_ascii=False, indent=2) + '\n')
    print(render_markdown(report, paired))
    return 0


TEXT_COLUMNS = ('summary', 'summary_text', 'text', 'headline', 'title')


def _load_evidence(path):
    """加载证据记录（CSV/JSONL）并规整为 evidence_store schema；未提供返回 None。

    `normalize_evidence` 只保留 `summary_hash`，会把正文剥掉；而入场否决必须让模型看到
    正文才能判断，所以这里把正文列按行回挂（normalize 逐行 map，不改变行序）。
    """
    if not path:
        return None
    import pandas as pd
    from scripts.evidence.evidence_store import normalize_evidence
    p = Path(path)
    frame = (pd.read_json(p, lines=True) if p.suffix in ('.jsonl', '.ndjson')
             else pd.read_csv(p))
    normalized = normalize_evidence(frame)
    for column in TEXT_COLUMNS:
        if column in frame.columns:
            normalized[column] = list(frame[column])
    return normalized


def _make_real_model(m):
    import yaml
    from mutifactor.llm import LLMAdvisor
    cfg_path = Path(__file__).resolve().parents[2] / 'config.yaml'
    return RealModel(LLMAdvisor((yaml.safe_load(cfg_path.read_text()) or {}).get('llm', {})),
                     knowledge_cutoff=m.llm_policy.get('knowledge_cutoff'))


def drop_from_schedule(schedule: dict, exec_session: str, dropped: set) -> None:
    """从**指定执行日**的队列里摘掉被否决/BLOCK 的机会。

    只动这一天：被否决的只是本批机会，其它执行日的待执行队列必须原样保留。
    """
    if not dropped:
        return
    schedule[exec_session] = [o for o in schedule.get(exec_session, [])
                              if o.opportunity_id() not in dropped]


def _assert_same_packet(recorded_attempt_id, attempt_id, opp) -> None:
    """重跑时证据包变了就不能复用旧决定 —— 那是回答了另一个问题的答案。"""
    if recorded_attempt_id and recorded_attempt_id != attempt_id:
        raise ValueError(f'PACKET_CHANGED_ON_RERUN:{opp.opportunity_id()}')


def _reuse_or_decide(store, scope, opp, packet, attempt_id, deadline, model_factory,
                     force_recall):
    """返回该机会的 overlay 决定：优先复用已落库记录，否则按需发起调用。"""
    job = store.job_run(attempt_id)
    if job is not None and job['status'] in ('PENDING', 'ABANDONED'):
        # 上一次调用已发起、结果未知。钱可能已经花了且金额不可知 —— 绝不重试付费。
        # 这一步不受 force_recall 影响：绕过它等于用一次重复调用掩盖成本缺口。
        return OverlayDecision('ABSTAIN', 'RECALL_ABANDONED', 0, '', False, True, attempt_id)
    if not force_recall:
        recorded = store.application(scope, opp.opportunity_id())
        if recorded:
            _assert_same_packet(recorded.get('attempt_id'), attempt_id, opp)
            return OverlayDecision(recorded['action'], recorded['reason_code'],
                                   recorded.get('model_cost', 0), recorded.get('raw_action', ''),
                                   recorded.get('late_response_observed', False),
                                   recorded.get('cost_uncertain', False),
                                   recorded.get('attempt_id', ''))
        if job is not None and job['status'] == 'COMPLETED':
            # 调用完成但 Application 未落库（崩溃窗口的另一半）：按记录恢复，不重调
            return OverlayDecision(job['action'], job['reason_code'], job.get('model_cost', 0),
                                   job.get('raw_action', ''),
                                   job.get('late_response_observed', False),
                                   job.get('cost_uncertain', False), attempt_id)
    if not model_call_expected(packet):
        return decide_overlay(packet, None, deadline, attempt_id=attempt_id)
    store.put_job_run(attempt_id, 1, 'PENDING',
                      {'opportunity_id': opp.opportunity_id(),
                       'packet_id': packet['packet_id']})
    d = decide_overlay(packet, model_factory(), deadline, attempt_id=attempt_id)
    store.put_job_run(attempt_id, 1, 'COMPLETED', {
        'opportunity_id': opp.opportunity_id(), 'packet_id': packet['packet_id'],
        'action': d.action, 'reason_code': d.reason_code, 'model_cost': d.model_cost,
        'cost_uncertain': d.cost_uncertain, 'raw_action': d.raw_action,
        'late_response_observed': d.late_response_observed})
    return d


def run_entry_overlay(store, scope, opportunities, *, session, exec_session, quotes, records,
                      real_model, knowledge_cutoff=None, force_recall=False):
    """对一个 session 新生成的机会跑 L 侧 overlay。

    返回 (kept, known_cost_micro, uncertain_attempt_ids)。

    时序：决策在信号日 t 收盘后做出（证据 as_of = t 收盘，回复截止 = 次日开盘前 10 分钟）。
    BLOCK → 不调模型、不计成本，直接剔除（关键数据不可用/未来，成交本身就是前视）。

    重复运行安全：已落库的决定原样复用；`shadow_job_runs` 记录堵住「模型已扣费但决定
    未落库」的崩溃窗口（见 `_reuse_or_decide`）。
    """
    kept, known_cost, uncertain = [], 0, []
    cutoff = entry_decision_cutoff(session)
    deadline = entry_response_deadline(exec_session)
    for opp in opportunities:
        events = [] if records is None else events_from_records(
            records, opp.security_id, cutoff)[0]
        packet = build_entry_packet(opp, quotes.get(opp.security_id, {}), events, {}, cutoff,
                                    model_knowledge_cutoff=knowledge_cutoff)
        attempt_id = stable_id('llm_attempt', scope, opp.opportunity_id(), packet['packet_id'])
        d = _reuse_or_decide(
            store, scope, opp, packet, attempt_id, deadline,
            lambda: real_model if real_model is not None else FakeModel(action='PASS',
                                                                       cost_micro=0),
            force_recall)
        store.put_application(Application(
            scope=scope, opportunity_id=opp.opportunity_id(), action=d.action,
            reason_code=d.reason_code, decision_id='', as_of=cutoff, applied=True,
            model_cost=d.model_cost, raw_action=d.raw_action,
            late_response_observed=d.late_response_observed,
            cost_uncertain=d.cost_uncertain, attempt_id=d.attempt_id))
        known_cost += d.model_cost
        if d.cost_uncertain and d.attempt_id:
            uncertain.append(d.attempt_id)
        if d.action not in ('VETO', 'BLOCK'):
            kept.append(opp)
    return kept, known_cost, uncertain


def cmd_run_forward(args):
    """逐日增量信号生成 + 前向运行 R/L 账户（真实候选 + 可选真实 LLM）。

    时序（无前视）：
      session t   生成信号 → 机会的 planned_execution_session = next(t)
                  同时做 L 侧否决决策（证据 as_of = t 收盘）
      session t+1 认领昨日决策、按计划执行；引擎校验执行日必须等于当前 session
    循环结束后仍未被认领的机会标记 MISSED_EXECUTION —— 绝不按历史开盘价补成交。
    """
    import pandas as pd
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    from scripts.medium_term.p2_selection_check import (ACTIONS, ETF_RAW, QUALITY, load_panels,
                                                        market_frame, trading_calendar)
    from scripts.medium_term.risk_rule_experiment import _build_shared_audits
    from .candidate_adapter import IncrementalCandidateGenerator
    from .verify_parity import _shadow_actions
    prices, _ = load_panels()
    prices['session'] = pd.to_datetime(prices.session).dt.normalize()
    prices['security_id'] = prices.security_id.astype(str)
    quality = pd.read_csv(QUALITY)
    actions = pd.read_csv(ACTIONS)
    actions['security_id'] = actions.security_id.astype(str)
    audits, blocked = _build_shared_audits(prices, actions)
    cal = trading_calendar(ETF_RAW)
    gen = IncrementalCandidateGenerator(
        prices, market_frame(ETF_RAW), quality, actions, blocked, cal,
        experiment_id=m.experiment_id, parent_version=m.parent_version,
        exit_policy_id=m.execution_policy['exit_policy_id'],
        top_n=m.risk_policy.get('top_n', 5),
        max_wait_sessions=m.execution_policy.get('max_wait_sessions', 20),
        require_matured=False)  # 真实前向：不得据未来 bar / 未来行动丢弃候选
    # 不调 gen.precompute_signals()：它用日历最后一天做 as_of 算复权价，
    # 等于让未来拆股/分红影响历史信号，与本次去前视的目标冲突。

    overlay = m.llm_policy.get('overlay', 'fixed_pass')
    real_model = (_make_real_model(m)
                  if overlay == 'entry_veto' and m.llm_policy.get('use_real_model') else None)
    records = _load_evidence(getattr(args, 'evidence', None))

    start = m.start_session or str(pd.Timestamp(cal[0]).date())
    end = args.to_session
    sessions = [s for s in cal if start <= str(pd.Timestamp(s).date()) <= end]
    close_micro = {(pd.Timestamp(r.session), str(r.security_id)): to_micro(r.raw_close)
                   for r in prices.itertuples(index=False)}
    scheduled = {scope: {} for scope in m.account_scopes}
    executed, missed = 0, 0

    for session in sessions:
        sess_str = str(pd.Timestamp(session).date())
        fresh = gen.opportunities_for(session)
        for o in fresh:
            store.put_opportunity(o)
            for scope in m.account_scopes:
                scheduled[scope].setdefault(o.planned_execution_session, []).append(o)
        bars = {sid: {'open': to_micro(r.raw_open), 'high': to_micro(r.raw_high),
                      'low': to_micro(r.raw_low), 'close': to_micro(r.raw_close)}
                for sid, r in prices[prices.session.eq(session)].set_index('security_id').iterrows()}
        acts = [a for a in _shadow_actions(actions) if a.get('ex_date') == sess_str]
        # 本 session 新生成的机会一律计划在下一个交易日执行
        nxt = int(gen.calendar.searchsorted(session)) + 1
        exec_next = (str(pd.Timestamp(gen.calendar[nxt]).date())
                     if nxt < len(gen.calendar) else sess_str)

        for scope in m.account_scopes:
            row = store.latest_state(scope)
            saved = row[1] if row else None
            state = state_from_dict(saved) if saved else new_account_state(scope, m.initial_cash)
            cost, uncertain = 0, []
            if overlay == 'entry_veto' and scope.endswith(':L'):
                kept, cost, uncertain = run_entry_overlay(
                    store, scope, fresh, session=session, exec_session=exec_next,
                    quotes={o.security_id: {'price': close_micro[(session, o.security_id)],
                                            'observed_at': entry_decision_cutoff(session)}
                            for o in fresh},
                    records=records, real_model=real_model,
                    knowledge_cutoff=m.llm_policy.get('knowledge_cutoff'))
                # 只从**本批**机会的队列里摘掉被否决/被 BLOCK 的，别动其它执行日的队列
                dropped = ({o.opportunity_id() for o in fresh}
                           - {o.opportunity_id() for o in kept})
                drop_from_schedule(scheduled[scope], exec_next, dropped)
            intents = intents_for_session(scheduled[scope].pop(sess_str, []), sess_str)
            res = step(state, session=sess_str, bars=bars, corporate_actions=acts,
                       intents=intents, manifest=m, model_cost=cost,
                       model_cost_uncertain=tuple(uncertain))
            if res.nav is None:
                continue
            store.save_state(scope, res.state, res.nav, res.events)
            for o in intents:
                store.set_opportunity_terminal(o.opportunity_id(), 'EXECUTED', sess_str)
                executed += 1

    # 收尾：仍未认领的机会（停机/循环末尾）明确记 MISSED_EXECUTION，不补按历史开盘价成交
    for scope in m.account_scopes:
        for exec_sess, lst in scheduled[scope].items():
            for o in lst:
                store.set_opportunity_terminal(o.opportunity_id(), 'MISSED_EXECUTION',
                                               exec_sess, note='UNCLAIMED_AT_RUN_END')
                missed += 1
    print(json.dumps({'experiment': m.experiment_id, 'sessions': len(sessions),
                      'to_session': end, 'executed': executed, 'missed_execution': missed},
                     ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='portfolio_shadow')
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name, fn in (('validate', cmd_validate), ('freeze', cmd_freeze),
                     ('run-session', cmd_run_session), ('run-forward', cmd_run_forward),
                     ('replay', cmd_replay), ('report', cmd_report)):
        p = sub.add_parser(name)
        p.add_argument('--manifest', required=True)
        if name == 'freeze':
            p.add_argument('--start-session', required=True)
        if name == 'run-session':
            p.add_argument('--session', required=True)
            p.add_argument('--schedule', required=True)
        if name == 'run-forward':
            p.add_argument('--to-session', required=True)
            p.add_argument('--evidence', help='证据记录文件（CSV/JSONL，evidence_store schema）')
        p.add_argument('--output', required=True)
        p.set_defaults(fn=fn)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == '__main__':
    raise SystemExit(main())
