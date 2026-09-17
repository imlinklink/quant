"""R/L 双影子账户 CLI。

正式运行（设计 §9，决策与结算分离）：
    prepare-entry-reviews --session T      生产并冻结真实机会与证据包
    review-entries --execution-session T1  领取到期机会、调用模型、冻结动作
    settle-session --session T1            消费已冻结动作与当日行情，推进 R/L 并出报告

三者分开是为了让「看到当天结果后补作决策」在**结构上**不可能：settle-session 没有
模型，review-entries 也不读执行日的结果。

辅助命令：validate / freeze / import-evidence / replay / report。
`run-session` 与 `run-forward` 是历史夹具/重放工具，**禁止真实模型调用**（设计 §9）。

manifest.json 金额用美元（initial_cash 用美元）；schedule.json 为逐 session 的
bars/公司行动/机会排程。金额在进入引擎前转 int 微美元。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.live_trading.decision_ledger.event_store import stable_id

from .candidate_adapter import adapt_schedule, intents_for_session
from .evidence import (build_entry_packet, entry_collection_time, entry_market_cutoff,
                       entry_response_deadline, events_from_records, now_iso,
                       policy_for_mode, validate_evidence_cutoff)
from .entry_review import EntryReviewer
from .llm_overlay import FakeModel, RealModel
from .paper_engine import new_account_state, step
from .replay import replay
from .schema import Application, Manifest, to_micro
from .store import ShadowStore, state_from_dict


MANIFEST_FIELDS = ('experiment_id', 'status', 'parent_strategy_id', 'parent_version',
                   'parent_code_hash', 'universe_id', 'universe_hash', 'account_scopes',
                   'initial_cash', 'currency', 'risk_policy', 'execution_policy',
                   'llm_policy', 'calendar_version', 'data_hashes', 'evaluation_protocol',
                   'start_session')
MANIFEST_REQUIRED = ('experiment_id', 'parent_strategy_id', 'parent_version',
                     'parent_code_hash', 'universe_id', 'universe_hash', 'account_scopes',
                     'initial_cash', 'risk_policy', 'execution_policy', 'llm_policy',
                     'calendar_version')


def manifest_from_dict(d: dict) -> Manifest:
    """从 manifest.json 构造 Manifest。

    顶层键严格校验：拼错的键（如 llm_polcy）原先要么被静默忽略、要么抛裸 KeyError，
    都会让本该生效的安全门无声失效。嵌套策略字典的未知键由 `Manifest.validate()` 兜。
    """
    unknown = sorted(set(d) - set(MANIFEST_FIELDS))
    if unknown:
        raise ValueError(f'MANIFEST_UNKNOWN_FIELDS:{unknown}'
                         f'（允许：{sorted(MANIFEST_FIELDS)}）')
    missing = sorted(set(MANIFEST_REQUIRED) - set(d))
    if missing:
        raise ValueError(f'MANIFEST_MISSING_FIELDS:{missing}')
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
        # L 侧 overlay：走与 run-forward **相同**的编排（设计 §7：所有入口用共同编排，
        # 避免两个入口产生不同的决策路径）
        if overlay == 'entry_veto' and scope.endswith(':L'):
            kept = []
            for item, opp in zip(sess.get('opportunities', []), opportunities):
                packet = build_entry_packet(
                    opp, item.get('quote') or {}, item.get('events', []),
                    item.get('fundamentals', {}), deadline,
                    model_knowledge_cutoff=m.llm_policy.get('knowledge_cutoff'),
                    evidence={'evidence_mode': m.llm_policy.get('evidence_mode', 'strict'),
                              'source_packet_hash': item.get('source_packet_hash'),
                              'included_event_count': len(item.get('events', [])),
                              'exclusion_count': 0, 'exclusion_reasons': {}})
                cfg = item.get('model') or {}
                reviewer = make_reviewer(
                    store, scope, real_model, model_id='fixture',
                    model_factory=((lambda: real_model) if real_model is not None
                                   else (lambda cfg=cfg: FakeModel(
                                       action=cfg.get('action', 'PASS'),
                                       reason_code=cfg.get('reason_code', ''),
                                       evidence_ids=cfg.get('evidence_ids', []),
                                       status=cfg.get('status', 'OK'),
                                       completed_at=cfg.get('completed_at', deadline),
                                       cost_micro=cfg.get('cost_micro', 0)))))
                outcome = reviewer.review(opp, packet, deadline)
                if not outcome.frozen:
                    kept.append(opp)
                    continue
                d = outcome.decision
                cost += d.model_cost
                if d.cost_uncertain and d.attempt_id:
                    uncertain.append(d.attempt_id)
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
        _mark_applied(store, scope, res, scope_intents, args.session)
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
    from .report import (daily_report, decision_trace, paired_performance, render_markdown,
                         render_trace)
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    report = daily_report(store, m)
    paired = paired_performance(store, m)
    out_dir = Path(args.output) / m.experiment_id
    out_dir.mkdir(parents=True, exist_ok=True)
    traces = {oid: decision_trace(store, oid, m.account_scopes)
              for oid in (getattr(args, 'trace', None) or [])}
    payload = {'daily_report': report, 'paired_performance': paired,
               'entry_metrics': report.get('entry_metrics', {})}
    if traces:
        payload['decision_traces'] = traces
    (out_dir / 'summary.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    print(render_markdown(report, paired))
    for trace in traces.values():
        print()
        print(render_trace(trace))
    return 0


def _evidence_source(m, path):
    """按 manifest 固定的窗口/容量策略构造证据来源（设计 §5.2：二者固定在 manifest）。

    `path` 是**已导入**的规范证据存储（由 `evidence_source.import_evidence_jsonl` 产出），
    不是原始来源文件 —— 入库时刻必须在导入那一步写死，读取时才取当前时刻会让证据随重跑
    而"变得可得"。
    """
    if not path:
        return None
    from .evidence_source import JsonlEvidenceSource
    return JsonlEvidenceSource(
        path, window_days=int(m.llm_policy['evidence_window_days']),
        max_events=int(m.llm_policy['evidence_max_events']))


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


def entry_packet_for(opp, quote, fetch, as_of, *, evidence_mode='strict',
                     knowledge_cutoff=None):
    """构建某机会的 entry-veto 证据包（设计 §5.1）。

    `fetch` 是 `EvidenceFetchResult`（或 None 表示未接入证据源）。`as_of` 是**证据采集
    时刻**，同时用于证据可见性过滤与包内 as_of 字段。

    需要复算 `attempt_id` 的调用方（含测试）必须走同一条路径 —— 两边各造一次包，一旦有
    差异 `attempt_id` 就会错位，崩溃窗口守卫会静默失效。
    """
    if fetch is None:
        events, meta, status = [], {'evidence_mode': evidence_mode}, 'NOT_CONFIGURED'
    else:
        events, meta, status = list(fetch.events), dict(fetch.meta), fetch.status
        if meta.get('evidence_mode') not in (None, evidence_mode):
            # evidence_store 由策略反推的等级与 manifest 声明的不一致 = 两处定义漂移
            raise ValueError(f'EVIDENCE_MODE_DRIFT:{meta["evidence_mode"]}!={evidence_mode}')
    return build_entry_packet(opp, quote, events, {}, as_of,
                              model_knowledge_cutoff=knowledge_cutoff, evidence=meta,
                              fetch_status=status)


def freeze_entry_reviews(store, opportunities, *, quotes, source, market_cutoff, deadline,
                         evidence_mode='strict', knowledge_cutoff=None, collected_at=None):
    """账户无关阶段：一个机会一个冻结证据包，并做数据质量分流。

    返回 (reviews, blocked)，两者元素均为 (opportunity, packet)。

    证据截止（as_of）= **实际采集时刻**（设计 §3.2），必须落在 [信号日收盘, 决策截止]
    之间；包**首次写入即冻结**，重跑原样复用，绝不用今天的信息改写当时的决策依据。

    BLOCK 是**机会级**判定（关键行情/股票身份/规则计划缺失或无效），按设计必须同时
    禁止 R 与 L 的新风险，所以它必须在账户资格检查**之前**完成。
    """
    as_of = validate_evidence_cutoff(collected_at or now_iso(),
                                     market_cutoff=market_cutoff, deadline=deadline)
    reviews, blocked = [], []
    for opp in opportunities:
        packet = store.packet_for_opportunity(opp.opportunity_id())
        if packet is None:
            fetch = (None if source is None
                     else source.load_events(opp.security_id, as_of,
                                             evidence_mode=evidence_mode))
            packet = entry_packet_for(opp, quotes.get(opp.security_id, {}), fetch, as_of,
                                      evidence_mode=evidence_mode,
                                      knowledge_cutoff=knowledge_cutoff)
            store.put_packet(opp.opportunity_id(), packet)
        (blocked if packet['data_quality']['level'] == 'BLOCK' else reviews).append(
            (opp, packet))
    return reviews, blocked


def _mark_applied(store, scope, res, intents, session) -> None:
    """结算后标记该账户动作已实际应用（设计 §7：动作冻结 ≠ 成交）。

    被引擎拒掉的那些（missed 事件）不算 applied —— 冻结了但没成交，正是要区分开的两件事。

    R 侧没有模型决策，但账户动作仍要留痕（设计 §6 的账户级动作 `INTENT_CREATED`），
    所以这里在没有 Application 时补写一条，而不是要求调用方先造一个。
    """
    missed = {e.get('opportunity_id') for e in res.events if e.get('type') == 'missed'}
    for o in intents:
        if o.opportunity_id() in missed:
            continue
        if store.application(scope, o.opportunity_id()) is None:
            store.put_application(Application(
                scope=scope, opportunity_id=o.opportunity_id(), action='INTENT_CREATED',
                reason_code='PARENT_STRATEGY', decision_id='', as_of=session,
                decision_frozen=True, execution_applied=True))
        else:
            store.mark_execution_applied(scope, o.opportunity_id(), session)


def record_data_blocked(store, scopes, opp, packet, *, session):
    """记录机会级 BLOCK：终态 DATA_BLOCKED + 每个账户一条禁止新风险的账目。

    按设计 §5.3/§6，DATA_BLOCKED 对 R 与 L **同时**生效 —— 它不是某一侧的模型动作，
    而是机会本身不允许进入任何账户的新风险。
    """
    reason = 'DATA_BLOCK:' + ','.join(packet['data_quality']['critical_missing'])
    store.set_opportunity_terminal(opp.opportunity_id(), 'DATA_BLOCKED', session, note=reason)
    for scope in scopes:
        store.put_application(Application(
            scope=scope, opportunity_id=opp.opportunity_id(), action='DATA_BLOCKED',
            reason_code=reason, decision_id='', as_of=packet['as_of'],
            decision_frozen=True, execution_applied=False))


def make_reviewer(store, scope, real_model, *, model_id='', model_factory=None):
    """构造 L 侧评审编排者。所有入口共用同一条编排路径（设计 §7）。"""
    factory = model_factory or (
        lambda: real_model if real_model is not None else FakeModel(action='PASS',
                                                                   cost_micro=0))
    return EntryReviewer(store, scope=scope, model_factory=factory, model_id=model_id)


def apply_entry_reviews(store, scope, reviews, *, deadline, real_model, model_id='',
                        force_recall=False, reviewer=None):
    """账户相关阶段：对已冻结的证据包做三态评审。

    返回 (kept, known_cost_micro, uncertain_attempt_ids)。只有 L 侧会走到这里 ——
    R 账户执行父策略原计划，不经过模型。

    复用/领取/调用/冻结全部交给 `EntryReviewer`（设计 §7）：已冻结的决定原样复用；
    调用前原子领取；崩溃遗留的尝试判 UNKNOWN 而不是静默重发。
    """
    reviewer = reviewer or make_reviewer(store, scope, real_model, model_id=model_id)
    kept, known_cost, uncertain = [], 0, []
    for opp, packet in reviews:
        outcome = reviewer.review(opp, packet, deadline, force_recall=force_recall)
        if not outcome.frozen:
            # 尚未有最终动作（另一个 worker 持租约在调用）：不写账目、不放行也不否决
            kept.append(opp)
            continue
        d = outcome.decision
        known_cost += d.model_cost
        if d.cost_uncertain and d.attempt_id:
            uncertain.append(d.attempt_id)
        if d.action not in ('VETO', 'BLOCK'):
            kept.append(opp)
    return kept, known_cost, uncertain


def _market_data():
    """加载中型策略行情/行动/质量/ETF 日历（prepare 与 settle 共用）。"""
    import pandas as pd
    from scripts.medium_term.p2_selection_check import ACTIONS, ETF_RAW, QUALITY, load_panels
    prices, _ = load_panels()
    prices['session'] = pd.to_datetime(prices.session).dt.normalize()
    prices['security_id'] = prices.security_id.astype(str)
    actions = pd.read_csv(ACTIONS)
    actions['security_id'] = actions.security_id.astype(str)
    return prices, actions, QUALITY, ETF_RAW


def _opportunity_from_dict(d: dict):
    """`store.opportunities()` 返回的是 `_asdict` 过的 dict，还原成 Opportunity。"""
    from .schema import Opportunity
    payload = dict(d)
    payload['rule_reason_codes'] = tuple(payload.get('rule_reason_codes') or ())
    return Opportunity(**payload)


def _next_session(cal, session):
    """日历里的下一个交易日（设计 §3.1：T+1 由日历得到，不要求已有 T+1 行情）。"""
    import pandas as pd
    idx = int(cal.searchsorted(pd.Timestamp(session))) + 1
    return str(pd.Timestamp(cal[idx]).date()) if idx < len(cal) else None


def _sessions_upto(cal, start, end):
    import pandas as pd
    return [s for s in cal if start <= str(pd.Timestamp(s).date()) <= end]


def cmd_prepare_entry_reviews(args):
    """`prepare-entry-reviews --session T`：生产并冻结真实机会和证据（设计 §9）。

    只消费截至 T 的输入：**不要求已有 T+1 的行情**（设计 §3.1）。机会 T+1 执行、
    证据在收盘后采集（§3.2），动作冻结留给 `review-entries`。
    """
    import pandas as pd
    from scripts.medium_term.p2_selection_check import market_frame, trading_calendar
    from scripts.medium_term.risk_rule_experiment import _build_shared_audits
    from .candidate_adapter import IncrementalCandidateGenerator

    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    prices, actions, quality_path, etf_raw = _market_data()
    quality = pd.read_csv(quality_path)
    _, blocked = _build_shared_audits(prices, actions)
    cal = trading_calendar(etf_raw)
    gen = IncrementalCandidateGenerator(
        prices, market_frame(etf_raw), quality, actions, blocked, cal,
        experiment_id=m.experiment_id, parent_version=m.parent_version,
        parent_strategy_id=m.parent_strategy_id,
        exit_policy_id=m.execution_policy['exit_policy_id'],
        top_n=m.risk_policy.get('top_n', 5),
        max_wait_sessions=m.execution_policy.get('max_wait_sessions', 20),
        require_matured=False)

    target = args.session
    start = m.start_session or str(pd.Timestamp(cal[0]).date())
    # 增量生成器需要从更早的月末选股重放才能重建 pending；put_opportunity 首次写入即冻结，重放安全
    fresh = []
    for session in _sessions_upto(cal, start, target):
        got = gen.opportunities_for(session)
        for o in got:
            store.put_opportunity(o)
        if str(pd.Timestamp(session).date()) == target:
            fresh = got

    market_cutoff = entry_market_cutoff(target)
    exec_session = _next_session(cal, target)
    deadline = entry_response_deadline(exec_session or target)
    close_micro = {(pd.Timestamp(r.session), str(r.security_id)): to_micro(r.raw_close)
                   for r in prices.itertuples(index=False)}
    reviews, blocked_opps = freeze_entry_reviews(
        store, fresh,
        quotes={o.security_id: {'price': close_micro[(pd.Timestamp(target), o.security_id)],
                                'observed_at': market_cutoff} for o in fresh},
        source=_evidence_source(m, getattr(args, 'evidence', None)),
        market_cutoff=market_cutoff, deadline=deadline,
        evidence_mode=m.llm_policy.get('evidence_mode', 'strict'),
        knowledge_cutoff=m.llm_policy.get('knowledge_cutoff'),
        collected_at=entry_collection_time(market_cutoff, deadline))
    for opp, packet in blocked_opps:
        record_data_blocked(store, m.account_scopes, opp, packet, session=target)
    print(json.dumps({'session': target, 'execution_session': exec_session,
                      'opportunities': len(fresh), 'reviews': len(reviews),
                      'data_blocked': len(blocked_opps)}, ensure_ascii=False))
    return 0


def cmd_review_entries(args):
    """`review-entries --execution-session T1 --model real`：领取到期机会并冻结动作（§9）。

    这是唯一会调用模型的命令。动作在此冻结；`settle-session` 只消费已冻结的动作，
    结构上无法"看到当天结果后补作决策"。
    """
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    if m.llm_policy.get('overlay') != 'entry_veto':
        print(json.dumps({'reviewed': 0, 'note': 'overlay 非 entry_veto，无需评审'},
                         ensure_ascii=False))
        return 0
    use_real = args.model == 'real'
    if use_real and not m.llm_policy.get('use_real_model'):
        raise ValueError('MANIFEST_DOES_NOT_ALLOW_REAL_MODEL:'
                         'manifest.llm_policy.use_real_model 未开启')
    real_model = _make_real_model(m) if use_real else None

    deadline = entry_response_deadline(args.execution_session)
    scope = next(s for s in m.account_scopes if s.endswith(':L'))
    reviewer = make_reviewer(store, scope, real_model,
                             model_id=(m.llm_policy.get('model_id') or
                                       ('real' if use_real else 'fixture')))
    due = [_opportunity_from_dict(o) for o in store.opportunities()
           if o.get('planned_execution_session') == args.execution_session]
    reviewed, in_flight = 0, 0
    for opp in due:
        packet = store.packet_for_opportunity(opp.opportunity_id())
        if packet is None:
            raise ValueError(f'PACKET_NOT_PREPARED:{opp.opportunity_id()}'
                             '（先跑 prepare-entry-reviews）')
        outcome = reviewer.review(opp, packet, deadline)
        reviewed += 1
        in_flight += 0 if outcome.frozen else 1
    print(json.dumps({'execution_session': args.execution_session, 'due': len(due),
                      'reviewed': reviewed, 'in_flight': in_flight,
                      'model': 'real' if use_real else 'fixture'}, ensure_ascii=False))
    return 0


def cmd_settle_session(args):
    """`settle-session --session T1`：消费已冻结动作与当日行情，推进 R/L 并出报告（§9）。

    这里**没有模型**：只用此前冻结的机会与动作，叠加当天行情机械模拟开盘成交与日内
    止损（设计 §3.4）。
    """
    import pandas as pd
    from .verify_parity import _shadow_actions
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    prices, actions, _, _ = _market_data()
    session = args.session
    bars = {sid: {'open': to_micro(r.raw_open), 'high': to_micro(r.raw_high),
                  'low': to_micro(r.raw_low), 'close': to_micro(r.raw_close)}
            for sid, r in prices[prices.session.eq(pd.Timestamp(session))]
            .set_index('security_id').iterrows()}
    acts = [a for a in _shadow_actions(actions) if a.get('ex_date') == session]
    due = [_opportunity_from_dict(o) for o in store.opportunities()
           if o.get('planned_execution_session') == session]
    executed = 0
    for scope in m.account_scopes:
        row = store.latest_state(scope)
        saved = row[1] if row else None
        state = state_from_dict(saved) if saved else new_account_state(scope, m.initial_cash)
        intents, cost, uncertain = [], 0, []
        for opp in due:
            app = store.application(scope, opp.opportunity_id())
            if app is None:
                if scope.endswith(':L') and m.llm_policy.get('overlay') == 'entry_veto':
                    # 未评审：不放行（设计 §3.3 未按期冻结最终动作时按确定规则处理）
                    continue
                intents.append(opp)          # R 侧无模型决策，直接执行父策略
                continue
            if app['action'] in ('DATA_BLOCKED', 'VETO'):
                continue
            intents.append(opp)
            if scope.endswith(':L'):
                cost += app.get('model_cost', 0)
                if app.get('cost_uncertain') and app.get('attempt_id'):
                    uncertain.append(app['attempt_id'])
        res = step(state, session=session, bars=bars, corporate_actions=acts,
                   intents=intents, manifest=m, model_cost=cost,
                   model_cost_uncertain=tuple(uncertain))
        if res.nav is None:
            continue
        store.save_state(scope, res.state, res.nav, res.events)
        _mark_applied(store, scope, res, intents, session)
        executed += len(intents)
    print(json.dumps({'session': session, 'due': len(due), 'intents_applied': executed},
                     ensure_ascii=False))
    return 0


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
    if m.llm_policy.get('use_real_model'):
        # 设计 §9：run-forward 是历史夹具/重放工具，必须禁止真实模型调用 ——
        # 过去的执行日不能用今天生成的模型结果补填前瞻记录。正式运行走三命令。
        raise ValueError(
            'RUN_FORWARD_FORBIDS_REAL_MODEL:正式运行请用 prepare-entry-reviews / '
            'review-entries / settle-session')
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
    source = _evidence_source(m, getattr(args, 'evidence', None))

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

        # 账户无关阶段：冻结证据包 + 数据质量分流。
        # 行情/规则输入的截止 = 信号日收盘；证据的 as_of = 实际采集时刻（两者不同，
        # 见 evidence.entry_market_cutoff 的说明）。BLOCK 是机会级判定，必须同时
        # 禁止 R 与 L 的新风险，故在账户循环之前处理。
        market_cutoff = entry_market_cutoff(session)
        deadline = entry_response_deadline(exec_next)
        reviews, blocked = freeze_entry_reviews(
            store, fresh,
            quotes={o.security_id: {'price': close_micro[(session, o.security_id)],
                                    'observed_at': market_cutoff} for o in fresh},
            source=source, market_cutoff=market_cutoff, deadline=deadline,
            evidence_mode=m.llm_policy.get('evidence_mode', 'strict'),
            knowledge_cutoff=m.llm_policy.get('knowledge_cutoff'),
            collected_at=entry_collection_time(market_cutoff, deadline))
        for opp, packet in blocked:
            record_data_blocked(store, m.account_scopes, opp, packet, session=sess_str)
            for scope in m.account_scopes:
                drop_from_schedule(scheduled[scope], exec_next, {opp.opportunity_id()})

        for scope in m.account_scopes:
            row = store.latest_state(scope)
            saved = row[1] if row else None
            state = state_from_dict(saved) if saved else new_account_state(scope, m.initial_cash)
            cost, uncertain = 0, []
            if overlay == 'entry_veto' and scope.endswith(':L'):
                kept, cost, uncertain = apply_entry_reviews(
                    store, scope, reviews, deadline=deadline, real_model=real_model)
                # 只从**本批**机会的队列里摘掉被否决的，别动其它执行日的队列
                drop_from_schedule(scheduled[scope], exec_next,
                                   {o.opportunity_id() for o, _ in reviews}
                                   - {o.opportunity_id() for o in kept})
            intents = intents_for_session(scheduled[scope].pop(sess_str, []), sess_str)
            res = step(state, session=sess_str, bars=bars, corporate_actions=acts,
                       intents=intents, manifest=m, model_cost=cost,
                       model_cost_uncertain=tuple(uncertain))
            if res.nav is None:
                continue
            store.save_state(scope, res.state, res.nav, res.events)
            _mark_applied(store, scope, res, intents, sess_str)
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


def cmd_import_evidence(args):
    """把真实事件 JSONL 导入为规范证据存储（设计 §5.2）。

    **入库时刻在这一步写死**：`observed_at` = 系统实际入库时间，不由来源文件追溯指定。
    之后每次 `load_events` 读到的都是同一个值 —— 否则重跑会让证据随重跑而「变得可得」。
    """
    from .evidence_source import import_evidence_jsonl
    print(json.dumps(import_evidence_jsonl(args.source, args.output,
                                           ingested_at=args.ingested_at),
                     ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='portfolio_shadow')
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name, fn in (('validate', cmd_validate), ('freeze', cmd_freeze),
                     ('run-session', cmd_run_session), ('run-forward', cmd_run_forward),
                     ('replay', cmd_replay), ('report', cmd_report),
                     ('import-evidence', cmd_import_evidence),
                     ('prepare-entry-reviews', cmd_prepare_entry_reviews),
                     ('review-entries', cmd_review_entries),
                     ('settle-session', cmd_settle_session)):
        p = sub.add_parser(name)
        if name == 'import-evidence':
            p.add_argument('--source', required=True, help='真实事件 JSONL')
            p.add_argument('--ingested-at', help='入库时刻（默认当前，测试用）')
            p.add_argument('--output', required=True, help='规范证据存储输出路径')
            p.set_defaults(fn=fn)
            continue
        p.add_argument('--manifest', required=True)
        if name == 'freeze':
            p.add_argument('--start-session', required=True)
        if name == 'run-session':
            p.add_argument('--session', required=True)
            p.add_argument('--schedule', required=True)
        if name == 'prepare-entry-reviews':
            p.add_argument('--session', required=True)
            p.add_argument('--evidence', help='已导入的规范证据存储')
        if name == 'review-entries':
            p.add_argument('--execution-session', required=True)
            p.add_argument('--model', choices=('real', 'fixture'), default='fixture')
        if name == 'settle-session':
            p.add_argument('--session', required=True)
        if name == 'report':
            p.add_argument('--trace', action='append',
                           help='要展开的 opportunity_id（可重复）')
        if name == 'run-forward':
            p.add_argument('--to-session', required=True)
            p.add_argument('--evidence',
                           help='已导入的规范证据存储（由 import-evidence 产出）')
        p.add_argument('--output', required=True)
        p.set_defaults(fn=fn)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == '__main__':
    raise SystemExit(main())
