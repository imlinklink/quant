"""R/L 双影子账户 CLI：validate / freeze / run-session / replay / report。

manifest.json 金额用美元（initial_cash 用美元）；schedule.json 为逐 session 的
bars/公司行动/机会排程。金额在进入引擎前转 int 微美元。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .candidate_adapter import adapt_schedule, intents_for_session
from .evidence import build_entry_packet
from .llm_overlay import FakeModel, RealModel, resolve_overlay
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
        # 真实模型（DeepSeek）：llm_policy.use_real_model 时从 config.yaml 构造
        real_model = None
        if overlay == 'entry_veto' and m.llm_policy.get('use_real_model'):
            import yaml
            from mutifactor.llm import LLMAdvisor
            cfg_path = Path(__file__).resolve().parents[2] / 'config.yaml'
            llm_cfg = (yaml.safe_load(cfg_path.read_text()) or {}).get('llm', {})
            real_model = RealModel(LLMAdvisor(llm_cfg))
        # L 侧 overlay：entry_veto 时构建 packet → resolve → VETO 剔除 + 计成本
        if overlay == 'entry_veto' and scope.endswith(':L'):
            kept = []
            for item, opp in zip(sess.get('opportunities', []), opportunities):
                packet = build_entry_packet(opp, item.get('quote') or {}, item.get('events', []),
                                            item.get('fundamentals', {}), deadline)
                if real_model is not None:
                    mr = real_model.call(packet, deadline)
                else:
                    cfg = item.get('model') or {}
                    fake = FakeModel(action=cfg.get('action', 'PASS'),
                                     reason_code=cfg.get('reason_code', ''),
                                     evidence_ids=cfg.get('evidence_ids', []),
                                     status=cfg.get('status', 'OK'),
                                     completed_at=cfg.get('completed_at', deadline),
                                     cost_micro=cfg.get('cost_micro', 0))
                    mr = fake.call(packet, deadline)
                d = resolve_overlay(packet, mr, deadline)
                cost += d.model_cost
                store.put_application(Application(
                    scope=scope, opportunity_id=opp.opportunity_id(), action=d.action,
                    reason_code=d.reason_code, decision_id='', as_of=deadline, applied=True,
                    model_cost=d.model_cost, raw_action=d.raw_action,
                    late_response_observed=d.late_response_observed))
                if d.action != 'VETO':
                    kept.append(opp)
            scope_intents = kept
        res = step(state, session=args.session, bars=bars,
                   corporate_actions=sess.get('corporate_actions', []),
                   intents=scope_intents, manifest=m, model_cost=cost)
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


def cmd_run_forward(args):
    """逐日增量信号生成 + 前向运行 R/L 账户（真实候选 + 可选真实 LLM）。"""
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
        max_wait_sessions=m.execution_policy.get('max_wait_sessions', 20))
    real_model = None
    if m.llm_policy.get('overlay') == 'entry_veto' and m.llm_policy.get('use_real_model'):
        import yaml
        from mutifactor.llm import LLMAdvisor
        cfg_path = Path(__file__).resolve().parents[2] / 'config.yaml'
        real_model = RealModel(LLMAdvisor((yaml.safe_load(cfg_path.read_text()) or {}).get('llm', {})))

    start = m.start_session or str(pd.Timestamp(cal[0]).date())
    end = args.to_session
    sessions = [s for s in cal if start <= str(pd.Timestamp(s).date()) <= end]
    for session in sessions:
        sess_str = str(pd.Timestamp(session).date())
        intents = gen.opportunities_for(session)
        for o in intents:
            store.put_opportunity(o)
        bars = {sid: {'open': to_micro(r.raw_open), 'high': to_micro(r.raw_high),
                      'low': to_micro(r.raw_low), 'close': to_micro(r.raw_close)}
                for sid, r in prices[prices.session.eq(session)].set_index('security_id').iterrows()}
        acts = [a for a in _shadow_actions(actions) if a.get('ex_date') == sess_str]
        for scope in m.account_scopes:
            row = store.latest_state(scope)
            saved = row[1] if row else None
            state = state_from_dict(saved) if saved else new_account_state(scope, m.initial_cash)
            scope_intents = list(intents)
            cost = 0
            if m.llm_policy.get('overlay') == 'entry_veto' and scope.endswith(':L'):
                kept = []
                for opp in intents:
                    quote = {'price': bars[opp.security_id]['open'],
                             'observed_at': f'{sess_str}T00:00:00+00:00'}
                    packet = build_entry_packet(opp, quote, [], {}, f'{sess_str}T13:20:00+00:00')
                    model = real_model or FakeModel(action='PASS')
                    d = resolve_overlay(packet, model.call(packet, f'{sess_str}T13:20:00+00:00'),
                                        f'{sess_str}T13:20:00+00:00')
                    cost += d.model_cost
                    store.put_application(Application(
                        scope=scope, opportunity_id=opp.opportunity_id(), action=d.action,
                        reason_code=d.reason_code, decision_id='', as_of=f'{sess_str}T13:20:00+00:00',
                        applied=True, model_cost=d.model_cost, raw_action=d.raw_action,
                        late_response_observed=d.late_response_observed))
                    if d.action != 'VETO':
                        kept.append(opp)
                scope_intents = kept
            res = step(state, session=sess_str, bars=bars, corporate_actions=acts,
                       intents=scope_intents, manifest=m, model_cost=cost)
            if res.nav is not None:
                store.save_state(scope, res.state, res.nav, res.events)
    print(json.dumps({'experiment': m.experiment_id, 'sessions': len(sessions),
                      'to_session': end}, ensure_ascii=False))
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
        p.add_argument('--output', required=True)
        p.set_defaults(fn=fn)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == '__main__':
    raise SystemExit(main())
