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
from .position_overlay import (PATH_CHANGING_ACTIONS, POSITION_ABSTAIN,
                               POSITION_ACTION_SCHEMA_VERSION, FakePositionModel,
                               PositionRealModel, reduce_tier, subject_key_for)
from .position_packet import build_position_packet
from .position_review import PositionReviewer, PositionSubject
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


def verify_manifest_frozen(store, m) -> None:
    """运行时校验：盘上的 manifest 必须与冻结时逐字段一致。

    **冻结不是仪式**：改一个风险参数或起始日，实验身份没变、账本里的哈希也没变，但跑出来
    的数字已经不是同一把尺子了。所有运行入口都必须在**调用模型与写账本之前**过这一关；
    找不到冻结记录同样拒绝 —— 默认放行等于没有冻结。

    只校验不可变字段（`manifest_hash` 的输入）；`status` 是运行状态，单独判 PAUSED/CLOSED。
    """
    stored = store.frozen_manifest_hash()
    if stored is None:
        raise ValueError(f'EXPERIMENT_NOT_FROZEN:{m.experiment_id}（先 freeze 再运行）')
    incoming = m.manifest_hash()
    if stored != incoming:
        raise ValueError(
            f'MANIFEST_CHANGED_SINCE_FREEZE:{m.experiment_id}:'
            f'stored={stored[:16]}:incoming={incoming[:16]}'
            '（参数变更必须新建 experiment_id，不能顶着同一身份改尺子）')
    status = (store.get_experiment() or {}).get('status')
    if status in ('PAUSED', 'CLOSED'):
        raise ValueError(f'EXPERIMENT_{status}:{m.experiment_id}')


def cmd_validate(args):
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    errors = m.validate()
    print(json.dumps({'experiment_id': m.experiment_id, 'errors': errors}, ensure_ascii=False))
    return 1 if errors else 0


def cmd_freeze(args):
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    frozen = m.freeze(args.start_session)
    out_dir = Path(args.output) / frozen.experiment_id
    store = ShadowStore(out_dir / 'ledger.sqlite3', frozen.experiment_id)
    # 先校验后落盘：原实现先覆盖 manifest.json 再校验，于是「拒绝重复 freeze」时
    # 盘上的冻结文件已经被改掉了 —— 拒绝反而造成了破坏。
    store.save_experiment(frozen)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'manifest.json').write_text(
        json.dumps(_manifest_to_public(frozen), ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'experiment_id': frozen.experiment_id, 'status': frozen.status,
                      'manifest_hash': frozen.manifest_hash()}, ensure_ascii=False))
    return 0


def cmd_run_session(args):
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    verify_manifest_frozen(store, m)
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


def _make_real_model(m, *, allow_historical=False, cls=None):
    """构造真实模型客户端。

    设计 §7：模型总超时 60 秒，且**关闭客户端隐藏重试** —— 本层只发一次请求，
    重试策略由我们的领取/租约机制决定，不能让客户端在背后悄悄重发。
    `allow_historical` 仅供 `historical_debug`（§9）使用。
    `cls` 换角色契约（持仓评审用 `PositionRealModel`），前置门与成本语义共用。
    """
    import yaml
    from mutifactor.llm import LLMAdvisor
    cfg_path = Path(__file__).resolve().parents[2] / 'config.yaml'
    advisor = LLMAdvisor((yaml.safe_load(cfg_path.read_text()) or {}).get('llm', {}))
    advisor.max_retries = 1
    advisor.timeout = 60
    return (cls or RealModel)(advisor,
                              knowledge_cutoff=m.llm_policy.get('knowledge_cutoff'),
                              allow_historical=allow_historical)


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


def make_reviewer(store, scope, real_model, *, model_id='', model_factory=None,
                  debug=False):
    """构造 L 侧评审编排者。所有入口共用同一条编排路径（设计 §7）。"""
    factory = model_factory or (
        lambda: real_model if real_model is not None else FakeModel(action='PASS',
                                                                   cost_micro=0))
    return EntryReviewer(store, scope=scope, model_factory=factory, model_id=model_id,
                         debug=debug)


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


def _market_data(etf_raw=None):
    """加载中型策略行情/行动/质量/ETF 日历（prepare 与 settle 共用）。

    `etf_raw` 可指向一份**活的** ETF 快照（交易日历与市场门都来自它）。默认仍是冻结的
    回测产物 —— 前向运行需要当日日历时必须显式给出新快照，不能就地改审计产物。
    """
    import pandas as pd
    from scripts.medium_term.p2_selection_check import ACTIONS, QUALITY, load_panels
    from scripts.medium_term.p2_selection_check import ETF_RAW as DEFAULT_ETF_RAW
    prices, _ = load_panels()
    prices['session'] = pd.to_datetime(prices.session).dt.normalize()
    prices['security_id'] = prices.security_id.astype(str)
    actions = pd.read_csv(ACTIONS)
    actions['security_id'] = actions.security_id.astype(str)
    return prices, actions, QUALITY, (etf_raw or DEFAULT_ETF_RAW)


def _opportunity_from_dict(d: dict):
    """`store.opportunities()` 返回的是 `_asdict` 过的 dict，还原成 Opportunity。"""
    from .schema import Opportunity
    payload = dict(d)
    payload['rule_reason_codes'] = tuple(payload.get('rule_reason_codes') or ())
    return Opportunity(**payload)


def _forward_calendar(price_cal, session):
    """前向运行用的交易日历 = 价格序列 ∪ NYSE 规则历。

    **价格序列推不出「明天」**：它只包含已有行情的日子。设计 §3.1 说「T+1 由日历得到，
    不要求已取得 T+1 开盘价」——所以前向确定 T+1 必须靠交易日规则历。规则历覆盖未来，
    实际日线覆盖临时休市（`trading_calendar` 模块自己注明了这个分工）。
    """
    import pandas as pd
    from scripts.data.trading_calendar import sessions as rule_sessions
    centre = pd.Timestamp(session).normalize()
    rule = pd.DatetimeIndex(
        rule_sessions(centre - pd.Timedelta(days=10),
                      centre + pd.Timedelta(days=15))['session_date']).normalize()
    return pd.DatetimeIndex(sorted(set(pd.DatetimeIndex(price_cal).normalize()) | set(rule)))


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
    verify_manifest_frozen(store, m)
    prices, actions, quality_path, etf_raw = _market_data(getattr(args, 'etf_raw', None))
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
    # 增量生成器需要从更早的月末选股重放才能重建 pending 状态；但**只落库 T 自己的机会** ——
    # 本命令的契约是「准备 T 的评审」。把重放沿途的候选一并写进机会表，会让它们以 READY
    # 出现在漏斗里，看起来像「已准备但没评审」，实际是「从未准备」。沿途跳过的数量如实报出。
    fresh, skipped = [], 0
    for session in _sessions_upto(cal, start, target):
        got = gen.opportunities_for(session)
        if str(pd.Timestamp(session).date()) == target:
            fresh = got
        else:
            skipped += len(got)
    for o in fresh:
        store.put_opportunity(o)

    market_cutoff = entry_market_cutoff(target)
    # 用前向日历（规则历 ∪ 实际日线）确定 T+1 —— 价格序列只知道过去
    exec_session = _next_session(_forward_calendar(cal, target), target)
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
                      'data_blocked': len(blocked_opps),
                      # >0 说明本实验从更早的日期起就没有逐日准备（重放沿途的候选未落库）
                      'skipped_earlier_sessions': skipped}, ensure_ascii=False))
    return 0


def cmd_review_entries(args):
    """`review-entries --execution-session T1 --model real`：领取到期机会并冻结动作（§9）。

    这是唯一会调用模型的命令。动作在此冻结；`settle-session` 只消费已冻结的动作，
    结构上无法"看到当天结果后补作决策"。
    """
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    verify_manifest_frozen(store, m)
    if m.llm_policy.get('overlay') != 'entry_veto':
        print(json.dumps({'reviewed': 0, 'note': 'overlay 非 entry_veto，无需评审'},
                         ensure_ascii=False))
        return 0
    debug = args.model == 'historical_debug'
    use_real = args.model == 'real'
    if use_real and not m.llm_policy.get('use_real_model'):
        raise ValueError('MANIFEST_DOES_NOT_ALLOW_REAL_MODEL:'
                         'manifest.llm_policy.use_real_model 未开启')
    real_model = (_make_real_model(m, allow_historical=debug)
                  if (use_real or debug) else None)
    fixture_action = getattr(args, 'fixture_action', 'PASS')

    deadline = entry_response_deadline(args.execution_session)
    scope = next(s for s in m.account_scopes if s.endswith(':L'))
    reviewer = make_reviewer(
        store, scope, real_model,
        model_id=(m.llm_policy.get('model_id')
                  or ('real' if use_real else ('historical_debug' if debug else 'fixture'))),
        model_factory=((lambda: FakeModel(
            action=fixture_action, cost_micro=0, evidence_from_packet=True,
            reason_code=('MATERIAL_COMPANY_EVENT_RISK' if fixture_action == 'VETO' else '')))
            if (not use_real and not debug) else None),
        debug=debug)
    due = [_opportunity_from_dict(o) for o in store.opportunities()
           if o.get('planned_execution_session') == args.execution_session]
    reviewed, in_flight, traces = 0, 0, []
    for opp in due:
        packet = store.packet_for_opportunity(opp.opportunity_id())
        if packet is None:
            raise ValueError(f'PACKET_NOT_PREPARED:{opp.opportunity_id()}'
                             '（先跑 prepare-entry-reviews）')
        outcome = reviewer.review(opp, packet, deadline)
        reviewed += 1
        if not debug:
            in_flight += 0 if outcome.frozen else 1
        else:
            attempt = store.job_run(outcome.decision_id) or {}
            model_action = ''
            try:
                model_action = json.loads(attempt.get('raw_output') or '{}').get('action', '')
            except ValueError:
                model_action = ''
            traces.append({
                'security_id': opp.security_id,
                'decision_id': outcome.decision_id,
                'attempt_status': attempt.get('status'),
                # 模型自己的动作 vs 本层解析出的动作：调试时要能看到两者的差别
                'model_action': model_action,
                'raw_output': attempt.get('raw_output'),
                'validation_errors': attempt.get('validation_errors'),
                'resolved_action': outcome.decision.action,
                'resolved_reason': outcome.decision.reason_code,
                'late_response_observed': outcome.decision.late_response_observed,
                # 未知成本必须显示为 null 而不是 0 —— 0 会被读成「免费调用」
                'model_cost_usd': (None if outcome.decision.cost_uncertain
                                   else outcome.decision.model_cost / 1e6),
                'cost_uncertain': outcome.decision.cost_uncertain})
    summary = {'execution_session': args.execution_session, 'due': len(due),
               'reviewed': reviewed, 'in_flight': in_flight,
               'model': args.model}
    if debug:
        # 设计 §9：调试调用只留痕，不写 Application；如实打印请求证据与回复
        summary['historical_debug'] = True
        summary['applications_written'] = 0
        summary['traces'] = traces
    print(json.dumps(summary, ensure_ascii=False, indent=2 if debug else None))
    return 0


def _ensure_reviewed(store, scope, opp, deadline, now):
    """L 侧到期机会必须有已冻结动作；没有时按截止是否已过分别处理（设计 §3.3）。

    - 截止已过 → **明确冻结 ABSTAIN 并留下原因**（采用父策略，与 ABSTAIN 语义一致）；
    - 截止未到或状态无法确认 → **阻塞结算**。

    静默跳过是不行的：那会把一次调度失败/调用崩溃，在账户结果上表现成一次**没有记录的
    否决**，而 R 仍按规则买入 —— 两账户的差异就此失去归因。
    """
    app = store.application(scope, opp.opportunity_id())
    if app is not None:
        return app
    if str(now) <= str(deadline):
        raise ValueError(f'SETTLEMENT_BLOCKED_DECISION_PENDING:{opp.opportunity_id()}:'
                         f'deadline={deadline}:now={now}')
    store.put_application(Application(
        scope=scope, opportunity_id=opp.opportunity_id(), action='ABSTAIN',
        reason_code='DECISION_DEADLINE_MISSED', decision_id='', as_of=deadline,
        decision_frozen=True, execution_applied=False))
    return store.application(scope, opp.opportunity_id())


def _settle_terminals(store, scopes, due, session) -> int:
    """收口机会终态：EXECUTED = 至少一个账户真的成交；否则 MISSED_EXECUTION。

    某账户被 VETO 与否是**账户级**细节（记在 applications 里），不改变共享漏斗终态 ——
    同一个机会被 L 否决但 R 成交，它依然是 EXECUTED。
    """
    done = set()
    for scope in scopes:
        done |= store.executed_opportunities(scope, session)
    for opp in due:
        oid = opp.opportunity_id()
        store.set_opportunity_terminal(
            oid, 'EXECUTED' if oid in done else 'MISSED_EXECUTION', session,
            note='' if oid in done else 'NO_ACCOUNT_EXECUTED')
    return len(due)


def _settle_cost(resolved: dict) -> tuple:
    """该 session 该账户的模型成本与待补记尝试。

    **与是否成交无关**：设计 §8 要求 L 承担所有模型调用成本，包含失败和弃权。
    原先这段写在「非 VETO 才继续」之后，于是否决越多、漏算越多 —— 抽成独立函数并直接
    对已解析的动作集合计算，杜绝再次被某个 `continue` 绕过。
    """
    cost, uncertain = 0, []
    for app in resolved.values():
        if not app:
            continue
        cost += app.get('model_cost', 0)
        if app.get('cost_uncertain') and app.get('attempt_id'):
            uncertain.append(app['attempt_id'])
    return cost, uncertain


def _settle_intents(due, resolved) -> list:
    """进入引擎的机会：终态为 DATA_BLOCKED / VETO 的不执行。"""
    return [opp for opp in due
            if (resolved.get(opp.opportunity_id()) or {}).get('action')
            not in ('DATA_BLOCKED', 'VETO')]


def _settle_marks(store, scope, res, intents, session) -> list:
    """要落的「动作已应用」标记；被引擎拒掉（missed）的不算成交。"""
    missed = {e.get('opportunity_id') for e in res.events if e.get('type') == 'missed'}
    marks = []
    for o in intents:
        if o.opportunity_id() in missed:
            continue
        create = None
        if store.application(scope, o.opportunity_id()) is None:
            # R 侧无模型决策，但账户动作仍要留痕（设计 §6 的 INTENT_CREATED）
            create = Application(
                scope=scope, opportunity_id=o.opportunity_id(), action='INTENT_CREATED',
                reason_code='PARENT_STRATEGY', decision_id='', as_of=session,
                decision_frozen=True, execution_applied=True)
        marks.append({'opportunity_id': o.opportunity_id(), 'create': create})
    return marks


# ---- 持仓评审（影子侧；设计 §13 P1 的连续持仓路径）----
#
# 三者与入场三段式同构，分开的理由也相同：结算命令里**没有模型**，评审命令也不读
# 执行日的结果 —— 让「看到当天结果后补作决策」在结构上不可能。
#
# 评审主体是 **R 账户**的持仓：规则决定的，与 L 的历史无关。若主体取 L 自己，模型每次
# 看到的是「上一次自己决策的结果」，动作序列互相叠加，就无法独立统计了。L 已平掉的
# 代码在应用时按档位相对计算（见 `paper_engine.step` 的 2.5 阶段），持仓不足 1 股则
# 由引擎记 `POSITION_SIZE_ZERO` 的 missed 事件 —— 「为什么没应用」必须入账，不得静默丢弃。

def position_overlay_on(m) -> bool:
    return m.llm_policy.get('position_overlay') == 'position_action'


def reviewable_positions(store, m) -> tuple:
    """R 账户在最近一个已结算 session 的持仓，以及那个 session。

    返回 `({security_id: Position}, last_session)`。`last_session` 由 `settle` 推进；
    `run-daily` 必须先结算 T 再来准备 T 的评审，否则拿出来的是更早状态的仓位。
    """
    r_scope = next(s for s in m.account_scopes if s.endswith(':R'))
    row = store.latest_state(r_scope)
    if not row:
        return {}, None
    state = state_from_dict(row[1])
    return dict(state.positions), state.last_session


def freeze_position_reviews(store, m, *, session, execution_session, closes, source,
                            market_cutoff, as_of, evidence_mode, knowledge_cutoff):
    """为 R 账户每只持仓冻结一个评审包（首次写入即冻结，重跑原样复用）。

    返回 `(frozen, blocked)`；`blocked` 是质量门判为 BLOCK 的条数。BLOCK 的包**照样评审**
    —— 门会在调用模型之前短路成 `POSITION_ABSTAIN`（零成本，采用父策略），那正是关键数据
    不可用时应有的安全结果；真正不放行的是「照常持仓」，而弃权恰好就是照常持仓。
    """
    positions, _ = reviewable_positions(store, m)
    l_scope = next(s for s in m.account_scopes if s.endswith(':L'))
    frozen, blocked = [], 0
    for sid, pos in sorted(positions.items()):
        key = subject_key_for(pos.opportunity_id, execution_session)
        packet = store.packet_for_opportunity(key)
        if packet is None:
            fetch = (None if source is None
                     else source.load_events(sid, as_of, evidence_mode=evidence_mode))
            events, meta, status = ((list(fetch.events), dict(fetch.meta), fetch.status)
                                    if fetch is not None
                                    else ([], {'evidence_mode': evidence_mode},
                                          'NOT_CONFIGURED'))
            packet = build_position_packet(
                security_id=sid, trade={'entry_session': pos.entry_session},
                protection={'active_stop': pos.stop_micro,
                            'initial_stop': pos.initial_stop_micro,
                            'hard_exit_authoritative': True},
                events=events, as_of=as_of, execution_session=execution_session,
                account_scope=l_scope, experiment_id=m.experiment_id,
                opportunity_id=pos.opportunity_id, reviewed_session=session,
                shares=pos.shares, entry_price_micro=pos.entry_price_micro,
                mark_price_micro=closes.get(sid), evidence=meta,
                model_knowledge_cutoff=knowledge_cutoff, fetch_status=status,
                market_context={'observed_at': market_cutoff})
            store.put_packet(key, packet)
        frozen.append((key, packet))
        if packet['data_quality']['level'] == 'BLOCK':
            blocked += 1
    return frozen, blocked


def make_position_reviewer(store, m, *, scope, model, real_model=None, debug=False,
                           model_id=''):
    """构造 L 侧持仓评审编排者。与入场共用同一条编排路径（设计 §7）。"""
    if real_model is not None or model == 'real' or debug:
        factory = (lambda: real_model) if real_model is not None else None
    else:
        model_id = model_id or 'fixture'
        factory = (lambda: FakePositionModel(action=model_parts(model)[0],
                                             template_id=model_parts(model)[1],
                                             cost_micro=0, evidence_from_packet=True))
    return PositionReviewer(store, scope=scope, model_factory=factory, model_id=model_id,
                            debug=debug)


def model_parts(action: str) -> tuple:
    """fixture 动作 → (Position v2 动作, 模板档位后缀)。"""
    return {'hold': ('hold', ''), 'exit': ('exit', ''),
            'reduce_25': ('reduce', ':25'), 'reduce_50': ('reduce', ':50')}.get(
        action, ('hold', ''))


def _subject_from_packet(key: str, packet: dict) -> PositionSubject:
    """从冻结包还原评审主体。主体键自带执行日，包内 provenance 留了评审日。"""
    return PositionSubject(
        opportunity_id=(packet.get('trade') or {}).get('opportunity_id') or '',
        security_id=(packet.get('identity') or {}).get('security_id') or '',
        reviewed_session=(packet.get('provenance') or {}).get('reviewed_session') or '',
        execution_session=(packet.get('provenance') or {}).get('execution_session') or '')


def cmd_prepare_position_reviews(args):
    """`prepare-position-reviews --session T`：冻结 T 收盘时 R 账户每个持仓的评审包。

    只消费截至 T 的输入（**不要求已有 T+1 行情**，设计 §3.1）。执行日 T+1 在此确定，
    主体键按它定，结算时按执行日直接取回。
    """
    import pandas as pd
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    verify_manifest_frozen(store, m)
    if not position_overlay_on(m):
        print(json.dumps({'session': args.session, 'positions': 0, 'frozen': 0,
                          'note': 'position_overlay 未开启'}, ensure_ascii=False))
        return 0
    positions, held_session = reviewable_positions(store, m)
    if held_session != args.session:
        # 结算没跑到 T，R 的持仓状态不在 T 收盘 —— 按更早的状态冻结评审包，等于用
        # 当时看不到的仓位做决策。如实报出，不猜。
        print(json.dumps({'session': args.session, 'positions': len(positions),
                          'frozen': 0, 'held_session': held_session,
                          'skipped': 'POSITION_STATE_LAG'}, ensure_ascii=False))
        return 0
    if not positions:
        print(json.dumps({'session': args.session, 'positions': 0, 'frozen': 0,
                          'execution_session': None}, ensure_ascii=False))
        return 0
    prices, _, _, etf_raw = _market_data(getattr(args, 'etf_raw', None))
    cal = _forward_calendar(sorted(pd.DatetimeIndex(prices.session.unique())), args.session)
    exec_session = _next_session(cal, args.session)
    market_cutoff = entry_market_cutoff(args.session)
    deadline = entry_response_deadline(exec_session or args.session)
    as_of = entry_collection_time(market_cutoff, deadline)
    closes = {str(r.security_id): to_micro(r.raw_close)
              for r in prices[prices.session.eq(pd.Timestamp(args.session))].itertuples(
                  index=False)}
    frozen, blocked = freeze_position_reviews(
        store, m, session=args.session, execution_session=exec_session or args.session,
        closes=closes, source=_evidence_source(m, getattr(args, 'evidence', None)),
        market_cutoff=market_cutoff, as_of=as_of,
        evidence_mode=m.llm_policy.get('evidence_mode', 'strict'),
        knowledge_cutoff=m.llm_policy.get('knowledge_cutoff'))
    print(json.dumps({'session': args.session, 'execution_session': exec_session,
                      'positions': len(frozen), 'frozen': len(frozen),
                      'data_blocked': blocked}, ensure_ascii=False))
    return 0


def cmd_review_positions(args):
    """`review-positions --execution-session T1 --model ...`：持仓评审唯一调模型处。"""
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    verify_manifest_frozen(store, m)
    if not position_overlay_on(m):
        print(json.dumps({'execution_session': args.execution_session, 'due': 0,
                          'note': 'position_overlay 未开启'}, ensure_ascii=False))
        return 0
    debug = args.model == 'historical_debug'
    use_real = args.model == 'real'
    if use_real and not m.llm_policy.get('use_real_model'):
        raise ValueError('MANIFEST_DOES_NOT_ALLOW_REAL_MODEL:'
                         'manifest.llm_policy.use_real_model 未开启')
    real_model = (_make_real_model(m, allow_historical=debug, cls=PositionRealModel)
                  if (use_real or debug) else None)
    scope = next(s for s in m.account_scopes if s.endswith(':L'))
    deadline = entry_response_deadline(args.execution_session)
    reviewer = make_position_reviewer(
        store, m, scope=scope, model=getattr(args, 'fixture_action', 'hold'),
        real_model=real_model, debug=debug,
        model_id=(m.llm_policy.get('model_id')
                  or ('real' if use_real else ('historical_debug' if debug else 'fixture'))))
    due = store.packets_matching(f'@pos:{args.execution_session}')
    reviewed, in_flight, changed, blocked, insufficient = 0, 0, 0, 0, 0
    traces = []
    for key, packet in due:
        level = (packet.get('data_quality') or {}).get('level')
        blocked += level == 'BLOCK'
        insufficient += level == 'LLM_INSUFFICIENT'
        outcome = reviewer.review(_subject_from_packet(key, packet), packet, deadline)
        reviewed += 1
        if not outcome.frozen:
            in_flight += 1
        elif outcome.decision.action in PATH_CHANGING_ACTIONS:
            changed += 1
        if debug:
            attempt = store.job_run(outcome.decision_id) or {}
            traces.append({'subject_key': key, 'decision_id': outcome.decision_id,
                           'attempt_status': attempt.get('status'),
                           'raw_output': attempt.get('raw_output'),
                           'validation_errors': attempt.get('validation_errors'),
                           'resolved_action': outcome.decision.action,
                           'resolved_reason': outcome.decision.reason_code,
                           'model_cost_usd': (None if outcome.decision.cost_uncertain
                                              else outcome.decision.model_cost / 1e6),
                           'cost_uncertain': outcome.decision.cost_uncertain})
    summary = {'execution_session': args.execution_session, 'due': len(due),
               'reviewed': reviewed, 'in_flight': in_flight,
               'path_changed': changed, 'data_blocked': blocked,
               'llm_insufficient': insufficient, 'model': args.model}
    if debug:
        summary['historical_debug'] = True
        summary['applications_written'] = 0
        summary['traces'] = traces
    print(json.dumps(summary, ensure_ascii=False, indent=2 if debug else None))
    return 0


def _ensure_position_reviewed(store, scope, subject, deadline, now):
    """L 侧到期持仓主体必须有已冻结动作；没有时按截止是否已过分别处理。

    与入场的 `_ensure_reviewed` 同一纪律：**绝不静默跳过**。一次调度失败若不留记录，
    在账户结果上就表现成一次没有记录的「持有」，而 R 也没有对应的动作可以对比。
    """
    app = store.application(scope, subject.key())
    if app is not None:
        return app
    if str(now) <= str(deadline):
        raise ValueError(f'SETTLEMENT_BLOCKED_POSITION_PENDING:{subject.key()}:'
                         f'deadline={deadline}:now={now}')
    store.put_application(Application(
        scope=scope, opportunity_id=subject.key(), action=POSITION_ABSTAIN,
        reason_code='DECISION_DEADLINE_MISSED', decision_id='', as_of=deadline,
        decision_frozen=True, execution_applied=False))
    return store.application(scope, subject.key())


def settle_position_intents(store, scope, session, deadline, now) -> dict:
    """把为 `session` 冻结的持仓动作解析成引擎参数 `{security_id: {...}}`。

    只有真正改变路径的动作（`POSITION_REDUCE_*` / `POSITION_EXIT`）才进入引擎；弃权、
    HOLD、以及空操作的 tighten 都不改变 L 的持仓（采用父策略）。档位从动作名解析 ——
    冻结的绝对数量是按**评审主体**（R）的剩余量算的，拿它去卖 L 会过量。
    """
    intents, suppressed = {}, {}
    for key, packet in store.packets_matching(f'@pos:{session}'):
        subject = _subject_from_packet(key, packet)
        app = _ensure_position_reviewed(store, scope, subject, deadline, now)
        action = (app or {}).get('action') or ''
        if action not in PATH_CHANGING_ACTIONS:
            suppressed[subject.security_id] = action or 'NO_ACTION'
            continue
        intents[subject.security_id] = {
            'action': 'exit' if action == 'POSITION_EXIT' else 'reduce',
            'tier': reduce_tier(action),
            'decision_id': (app or {}).get('decision_id') or '',
            'subject_key': key}
    return {'intents': intents, 'suppressed': suppressed}


def position_marks(store, scope, res, session) -> list:
    """结算后要标记为已应用的持仓主体键（设计 §7：动作冻结 ≠ 成交）。

    判据是**已落库的成交事件**里有没有这次决策对应的 `fill` —— 引擎拒掉的那些（缺行情、
    档位不足 1 股）带有各自的 `missed` 事件，不算成交。冻结了但没成交，正是要区分的两件事。
    """
    applied = {e.get('decision_id') for e in res.events
               if e.get('type') == 'fill' and e.get('side') == 'SELL'
               and e.get('decision_id')}
    marks = []
    for key, _packet in store.packets_matching(f'@pos:{session}'):
        app = store.application(scope, key)
        if app and app.get('decision_id') and app['decision_id'] in applied:
            marks.append({'opportunity_id': key, 'create': None})
    return marks


# ---- 容量分配评审（Portfolio，设计 §6.3）----
#
# 三段式与入场/持仓相同，**调用模型不能放进 settle** —— 那条命令的性质是"没有模型"，
# §9 靠它保证"看到当天结果后补作决策在结构上不可能"。

def portfolio_review_on(m) -> bool:
    return m.llm_policy.get('portfolio_review') == 'portfolio_action'


def portfolio_subject_key(execution_session: str) -> str:
    from .portfolio_review import subject_key
    return subject_key(execution_session)


def cmd_prepare_portfolio_review(args):
    """`prepare-portfolio-review --session T`：冻结 T 的容量分配评审包（**不调模型**）。

    只消费截至 T 的输入（不要求已有 T+1 行情）。执行日 T+1 在此确定，主体键按它定，
    结算时按执行日直接取回。
    """
    import pandas as pd
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    verify_manifest_frozen(store, m)
    if not portfolio_review_on(m):
        print(json.dumps({'session': args.session, 'consult_required': False,
                          'note': 'portfolio_review 未开启'}, ensure_ascii=False))
        return 0
    prices, _, _, etf_raw = _market_data(getattr(args, 'etf_raw', None))
    cal = _forward_calendar(sorted(pd.DatetimeIndex(prices.session.unique())), args.session)
    exec_session = _next_session(cal, args.session)
    if exec_session is None:
        print(json.dumps({'session': args.session, 'execution_session': None,
                          'skipped': 'NO_NEXT_SESSION'}, ensure_ascii=False))
        return 0
    rows = prices[prices.session.eq(pd.Timestamp(args.session))]
    bars = {str(r.security_id): {'open': to_micro(r.raw_open), 'high': to_micro(r.raw_high),
                                 'low': to_micro(r.raw_low), 'close': to_micro(r.raw_close)}
            for r in rows.itertuples(index=False)}
    l_scope = next(s for s in m.account_scopes if s.endswith(':L'))
    row = store.latest_state(l_scope)
    state = state_from_dict(row[1]) if row else new_account_state(l_scope, m.initial_cash)
    from .portfolio_review import prepare
    packet = prepare(store, m, session=args.session, execution_session=exec_session,
                     bars=bars, state=state)
    print(json.dumps({'session': args.session, 'execution_session': exec_session,
                      'candidates': len(packet['candidates']),
                      'templates': [t['template_id'] for t in packet['templates']],
                      'consult_required': packet['consult_required'],
                      'limits_unavailable': sorted(packet.get('limits_unavailable') or {})},
                     ensure_ascii=False))
    return 0


def cmd_review_portfolio(args):
    """`review-portfolio --execution-session T1 --model ...`：容量分配的唯一调模型处。"""
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    verify_manifest_frozen(store, m)
    if not portfolio_review_on(m):
        print(json.dumps({'execution_session': args.execution_session, 'reviewed': 0,
                          'note': 'portfolio_review 未开启'}, ensure_ascii=False))
        return 0
    key = portfolio_subject_key(args.execution_session)
    packet = store.packet_for_opportunity(key)
    if packet is None:
        raise ValueError(f'PACKET_NOT_PREPARED:{key}（先跑 prepare-portfolio-review）')
    if not packet.get('consult_required'):
        # §6.3：没有容量冲突时默认不调用，避免制造无意义决策。
        print(json.dumps({'execution_session': args.execution_session, 'reviewed': 0,
                          'skipped': 'NO_CAPACITY_CONFLICT'}, ensure_ascii=False))
        return 0
    l_scope = next(s for s in m.account_scopes if s.endswith(':L'))
    use_real = args.model == 'real'
    if use_real and not m.llm_policy.get('use_real_model'):
        raise ValueError('MANIFEST_DOES_NOT_ALLOW_REAL_MODEL:'
                         'manifest.llm_policy.use_real_model 未开启')
    from scripts.live_trading.decision_engine import DecisionEngine
    engine_kwargs = {}
    if use_real:
        import yaml
        from mutifactor.llm import LLMAdvisor
        cfg_path = Path(__file__).resolve().parents[2] / 'config.yaml'
        advisor = LLMAdvisor((yaml.safe_load(cfg_path.read_text()) or {}).get('llm', {}))
        advisor.max_retries = 1
        advisor.timeout = 60
        engine_kwargs['advisor'] = advisor
    else:
        def fixture(_contract, packet_inner):
            return {'schema_version': 'portfolio-v1',
                    'packet_id': packet_inner.get('packet_id'), 'status': 'complete',
                    'chosen_template_id': 'keep_rule_allocation', 'reason_codes': [],
                    'facts': [], 'inferences': [], 'counterevidence': [],
                    'missing_information': ['夹具模型：采用规则分配']}
        engine_kwargs['call_model'] = fixture
    result = DecisionEngine(store.registry, config={}, **engine_kwargs).decide_portfolio(packet)
    applied = result.effective_action
    if result.status == 'validated' and applied:
        store.put_application(Application(
            scope=l_scope, opportunity_id=key, action=applied,
            reason_code='PORTFOLIO_ALLOCATION', decision_id=result.decision_id,
            as_of=packet['context']['as_of'], decision_frozen=True,
            execution_applied=False, raw_action=result.model_action or '',
            attempt_id=result.attempt_id or ''))
    summary = {'execution_session': args.execution_session, 'reviewed': 1,
               'status': result.status, 'model_action': result.model_action,
               'effective_action': applied, 'permission_level': result.permission_level,
               'validation_errors': list(result.validation_errors or ()), 'model': args.model}
    print(json.dumps(summary, ensure_ascii=False))
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
    verify_manifest_frozen(store, m)
    prices, actions, _, _ = _market_data(getattr(args, 'etf_raw', None))
    session = args.session
    now = now_iso()
    deadline = entry_response_deadline(session)
    bars = {sid: {'open': to_micro(r.raw_open), 'high': to_micro(r.raw_high),
                  'low': to_micro(r.raw_low), 'close': to_micro(r.raw_close)}
            for sid, r in prices[prices.session.eq(pd.Timestamp(session))]
            .set_index('security_id').iterrows()}
    acts = [a for a in _shadow_actions(actions) if a.get('ex_date') == session]
    due = [_opportunity_from_dict(o) for o in store.opportunities()
           if o.get('planned_execution_session') == session]
    executed = 0
    portfolio_dropped_total = 0
    for scope in m.account_scopes:
        row = store.latest_state(scope)
        saved = row[1] if row else None
        state = state_from_dict(saved) if saved else new_account_state(scope, m.initial_cash)
        is_l = scope.endswith(':L') and m.llm_policy.get('overlay') == 'entry_veto'
        # 持仓 overlay 与入场 overlay 是两个独立开关（角色级权限，设计 §12）
        pos_overlay = scope.endswith(':L') and position_overlay_on(m)
        # 1) 先解析每个到期机会的最终动作：L 侧缺动作时按截止冻结 ABSTAIN 或阻塞结算
        resolved = {
            opp.opportunity_id(): (
                _ensure_reviewed(store, scope, opp, deadline, now) if is_l
                else store.application(scope, opp.opportunity_id()))
            for opp in due}
        # 2) 成本与是否成交无关，独立计算
        cost, uncertain = _settle_cost(resolved) if is_l else (0, [])
        # 3) 计划进入引擎的机会
        intents = _settle_intents(due, resolved)
        # 3.5) 容量分配：按已冻结的 Portfolio 分配筛选 L 侧 intents。
        #      **没有冻结分配时原样放行**（未启用/无冲突/窗口错过）—— 那不该表现成
        #      "模型选择少买"。Portfolio 权限为 shadow 时 effective_action 是父策略，
        #      与不开启逐字节相同（由测试钉死）。
        portfolio_dropped = 0
        if scope.endswith(':L') and portfolio_review_on(m):
            from .portfolio_review import filter_intents
            intents, dropped = filter_intents(store, scope, session, intents)
            portfolio_dropped = len(dropped)
            portfolio_dropped_total += portfolio_dropped
        # 4) 持仓评审动作：同样先确保「到期必须有已冻结动作」，再交给引擎按档位应用。
        #    必须在 step 之前解析（阻塞/冻结纪律与入场一致：绝不静默跳过）。
        position_actions = (settle_position_intents(store, scope, session, deadline,
                                                    now)['intents']
                            if pos_overlay else {})
        res = step(state, session=session, bars=bars, corporate_actions=acts,
                   intents=intents, manifest=m, model_cost=cost,
                   model_cost_uncertain=tuple(uncertain),
                   position_actions=position_actions or None)
        pos_marks = (position_marks(store, scope, res, session) if pos_overlay else [])
        if res.nav is None:
            # 已处理过：账户不重复推进，但**补做归因**（上次可能崩在状态与标记之间）。
            # 用**已落库的成交事件**判断谁真的成交了，而不是拿内存里的 intents 猜。
            done = store.executed_opportunities(scope, session)
            executed += len(done)
            if done or pos_marks:
                store.save_state(scope, res.state, None, [], session=session,
                                 applied_marks=[{'opportunity_id': oid, 'create': None}
                                                for oid in sorted(done)] + pos_marks)
            continue
        # 状态、事件与归因标记**同事务**提交，不留「已成交但未标记」的窗口
        store.save_state(scope, res.state, res.nav, res.events, session=session,
                         applied_marks=(_settle_marks(store, scope, res, intents, session)
                                        + pos_marks))
        executed += len(intents)
    # 机会终态在结算时收口（run-forward 有，settle 原先漏了）
    _settle_terminals(store, m.account_scopes, due, session)
    print(json.dumps({'session': session, 'due': len(due), 'intents_applied': executed,
                      'portfolio_dropped': portfolio_dropped_total},
                     ensure_ascii=False))
    return 0


def sessions_to_settle(target: str, last_settled: str | None, *, data_sessions,
                       floor: str | None = None) -> list:
    """该由本次运行结算的 session：`(max(last_settled, floor), target]` 内的**全部**交易日。

    **不能只取「有机会排期」的日子**（原实现取 `{o.planned_execution_session}`）：`step`
    每会话才推进一次持有计数，并检查跳空止损/拆股/分红/时间退出。漏掉的日子会让 60 会话
    时间退出整体推迟，并把那些日子里本该触发的退出整段丢掉。

    2026-09-19 实测的形态：生产实验的 R/L 都停在 `last_session=2026-09-17` 且持有 LITE，
    而 09-18 已收盘、数据就绪门也是 READY —— 那天没有入场排期，于是整个交易日没人处理它。

    下界有两个，语义不同：`last_settled` 是**已结算**（严格晚于它才做），`floor` 是
    「不要早于这里」（**含**端点 —— 传实验的 `start_session`，它的账户就是从那天起算的，
    排在当天的机会必须能被结算）。首次运行时由 `floor` 收住，不会把全部历史各扫一遍。
    """
    return [s for s in data_sessions
            if s <= target
            and (last_settled is None or s > last_settled)
            and (floor is None or s >= floor)]


def last_settled_session(store, scopes) -> str | None:
    last = None
    for scope in scopes:
        row = store.latest_state(scope)
        if row and row[1].get('last_session'):
            last = max(last or '', row[1]['last_session'])
    return last


def _data_gate(explicit_session) -> dict:
    """T 的就绪判定（见 `data_readiness`）。

    显式 `--session` 是操作者的覆盖：不拦，只记录 —— 回放与补做本来就要指定历史日。
    """
    from .data_readiness import gate
    if explicit_session:
        return {'state': 'SKIPPED_EXPLICIT_SESSION', 'blocking': False, 'sources': {},
                'expected_session': None,
                'reason': f'显式指定 --session {explicit_session}，由操作者负责'}
    return gate()


def cmd_run_daily(args):
    """每日前向运行（设计 §3 的时序，一次做完三件事）。

    窗口是「T 收盘后 → T+1 开盘前」，所以一次运行按顺序做：

        settle(T)    执行昨天为 T 冻结的动作（需要 T 的行情，故必须在收盘后）
        prepare(T)   冻结 T 的机会与证据，机会排在 T+1 执行
        review(T+1)  为 T+1 冻结动作 —— **必须在 T+1 开盘前截止之前**

    `review` 错过截止时不补：`settle(T+1)` 会按设计 §3.3 明确冻结
    `ABSTAIN/DECISION_DEADLINE_MISSED`，而不是静默跳过或事后补一个动作。

    **T 先过数据就绪门**：T 必须等于规则历里收盘已过的最新 session。数据没到就只等待并留痕，
    不拿一个过期会话当今天的任务 —— 那会按早已过去的截止时间冻结机会、评审必然错过，而退出码
    与日志全都正常（2026-09-18 事故正是这个形态）。门只拦前向工作，结算照做（那是有行情的
    已有 session，幂等）。
    """
    from .data_readiness import READY
    from types import SimpleNamespace
    import pandas as pd
    m = manifest_from_dict(json.loads(Path(args.manifest).read_text()))
    store = ShadowStore(Path(args.output) / m.experiment_id / 'ledger.sqlite3', m.experiment_id)
    verify_manifest_frozen(store, m)
    etf_raw = getattr(args, 'etf_raw', None)
    prices, _, _, etf_path = _market_data(etf_raw)
    cal = _forward_calendar(sorted(pd.DatetimeIndex(prices.session.unique())),
                            args.session or prices.session.max())
    data_sessions = [str(pd.Timestamp(x).date()) for x in
                     sorted(pd.DatetimeIndex(prices.session.unique()))]
    target = args.session or data_sessions[-1]
    if target not in data_sessions:
        raise ValueError(f'NO_MARKET_DATA_FOR_SESSION:{target}')
    exec_session = _next_session(cal, target)
    deadline = entry_response_deadline(exec_session) if exec_session else None
    now = now_iso()

    result = {'session': target, 'execution_session': exec_session,
              'now': now, 'phase_deadline': deadline, 'steps': {}}

    # ① 结算：补做上次运行以来所有有行情但未结算的 session（settle 本身幂等）
    #    必须是**全部交易日**而非仅有入场排期的日子 —— 每个 session 都要推进持有计数并
    #    检查跳空止损/拆股/分红/时间退出，漏掉的日子会让这些全部错位。
    to_settle = sessions_to_settle(target, last_settled_session(store, m.account_scopes),
                                   data_sessions=data_sessions, floor=m.start_session)
    for day in to_settle:
        _capture(cmd_settle_session, manifest=args.manifest, output=args.output,
                 session=day, etf_raw=etf_raw)
    result['steps']['settled'] = to_settle

    # ② 数据就绪门：不做「拿过期会话当今天任务」这件事
    readiness = _data_gate(args.session)
    result['steps']['gate'] = readiness
    if readiness['state'] != READY:
        result['steps']['skipped_by_gate'] = {'state': readiness['state'],
                                              'reason': readiness['reason']}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        # 等待不算故障（明天再来）；故障必须看得见
        return 1 if readiness.get('blocking') else 0

    # ③ 准备 T 的机会与证据（排在 T+1 执行）
    prepared = _capture(cmd_prepare_entry_reviews, manifest=args.manifest, output=args.output,
                        session=target, evidence=getattr(args, 'evidence', None),
                        etf_raw=etf_raw)
    result['steps']['prepare'] = prepared
    if prepared.get('opportunities') == 0:
        # 无候选不是失败，但必须如实记录，不能制造候选或强行调用模型（设计 §4）
        result['steps']['no_opportunities'] = True

    # ③.5 持仓评审：与入场共用同一决策窗口与截止。动作同样在 T 收盘冻结、T+1 开盘应用；
    #      错过截止不补，留给 settle 按 §3.3 冻结 POSITION_ABSTAIN/DECISION_DEADLINE_MISSED。
    if not position_overlay_on(m):
        result['steps']['position_review'] = {'skipped': 'OVERLAY_OFF'}
    elif exec_session is None:
        result['steps']['position_review'] = {'skipped': 'NO_NEXT_SESSION'}
    elif now > deadline:
        result['steps']['position_review'] = {
            'skipped': 'DECISION_WINDOW_MISSED', 'deadline': deadline,
            'note': 'settle 会按 §3.3 冻结 POSITION_ABSTAIN/DECISION_DEADLINE_MISSED'}
    else:
        result['steps']['position_review'] = {
            'prepare': _capture(cmd_prepare_position_reviews, manifest=args.manifest,
                                output=args.output, session=target,
                                evidence=getattr(args, 'evidence', None), etf_raw=etf_raw),
            'review': _capture(cmd_review_positions, manifest=args.manifest,
                               output=args.output, execution_session=exec_session,
                               model=args.model,
                               fixture_action=getattr(args, 'position_fixture_action',
                                                      'hold'))}

    # ③.6 容量分配评审：与入场/持仓共用同一决策窗口与截止。三层顺序有意义 ——
    #      先由 prepare-entry-reviews(T) 落好 T+1 的机会，Portfolio 才有候选可分。
    if not portfolio_review_on(m):
        result['steps']['portfolio_review'] = {'skipped': 'OVERLAY_OFF'}
    elif exec_session is None:
        result['steps']['portfolio_review'] = {'skipped': 'NO_NEXT_SESSION'}
    elif now > deadline:
        result['steps']['portfolio_review'] = {
            'skipped': 'DECISION_WINDOW_MISSED', 'deadline': deadline,
            'note': 'Portfolio 无冻结分配时 settle 原样放行（不改变 L 的行为）'}
    else:
        result['steps']['portfolio_review'] = {
            'prepare': _capture(cmd_prepare_portfolio_review, manifest=args.manifest,
                                output=args.output, session=target, etf_raw=etf_raw),
            'review': _capture(cmd_review_portfolio, manifest=args.manifest,
                               output=args.output, execution_session=exec_session,
                               model=args.model)}

    # ④ 评审 T+1：只在截止之前做；错过就留给 settle 按规则冻结 ABSTAIN
    if exec_session is None:
        result['steps']['review'] = {'skipped': 'NO_NEXT_SESSION'}
    elif now > deadline:
        result['steps']['review'] = {'skipped': 'DECISION_WINDOW_MISSED',
                                     'deadline': deadline,
                                     'note': 'settle 会按 §3.3 冻结 ABSTAIN/DECISION_DEADLINE_MISSED'}
    else:
        result['steps']['review'] = _capture(
            cmd_review_entries, manifest=args.manifest, output=args.output,
            execution_session=exec_session, model=args.model,
            fixture_action=getattr(args, 'fixture_action', 'PASS'))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _capture(fn, **kwargs):
    """调用子命令并捕获它打印的 JSON（子命令各自打印一行 JSON 摘要）。"""
    import contextlib
    import io
    from types import SimpleNamespace
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(SimpleNamespace(**kwargs))
    for line in reversed(buf.getvalue().strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {}


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
    verify_manifest_frozen(store, m)
    if m.llm_policy.get('use_real_model'):
        # 设计 §9：run-forward 是历史夹具/重放工具，必须禁止真实模型调用 ——
        # 过去的执行日不能用今天生成的模型结果补填前瞻记录。正式运行走三命令。
        raise ValueError(
            'RUN_FORWARD_FORBIDS_REAL_MODEL:正式运行请用 prepare-entry-reviews / '
            'review-entries / settle-session')
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
    print(json.dumps(import_evidence_jsonl(
        args.source, args.output, ingested_at=args.ingested_at,
        observed_at_policy=args.observed_at_policy,
        append=args.append), ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='portfolio_shadow')
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name, fn in (('validate', cmd_validate), ('freeze', cmd_freeze),
                     ('run-session', cmd_run_session), ('run-forward', cmd_run_forward),
                     ('replay', cmd_replay), ('report', cmd_report),
                     ('import-evidence', cmd_import_evidence),
                     ('run-daily', cmd_run_daily),
                     ('prepare-entry-reviews', cmd_prepare_entry_reviews),
                     ('review-entries', cmd_review_entries),
                     ('prepare-position-reviews', cmd_prepare_position_reviews),
                     ('review-positions', cmd_review_positions),
                     ('prepare-portfolio-review', cmd_prepare_portfolio_review),
                     ('review-portfolio', cmd_review_portfolio),
                     ('settle-session', cmd_settle_session)):
        p = sub.add_parser(name)
        if name == 'import-evidence':
            p.add_argument('--source', required=True, help='真实事件 JSONL')
            p.add_argument('--ingested-at', help='入库时刻（默认当前，测试用）')
            p.add_argument('--observed-at-policy', choices=('ingest', 'unknown'),
                           default='ingest',
                           help='unknown = 留空（第三方历史档案，无观测记录）')
            p.add_argument('--append', action='store_true',
                           help='与已有存储合并（首次导入为准，重跑不刷新 observed_at）')
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
            p.add_argument('--model', choices=('real', 'fixture', 'historical_debug'),
                           default='fixture')
            p.add_argument('--fixture-action', choices=('PASS', 'VETO', 'ABSTAIN'),
                           default='PASS',
                           help='fixture 模型的动作（设计 §11 的确定性 VETO 验收用）')
        if name == 'prepare-position-reviews':
            p.add_argument('--session', required=True)
            p.add_argument('--evidence', help='已导入的规范证据存储')
        if name == 'review-positions':
            p.add_argument('--execution-session', required=True)
            p.add_argument('--model', choices=('real', 'fixture', 'historical_debug'),
                           default='fixture')
            p.add_argument('--fixture-action',
                           choices=('hold', 'reduce_25', 'reduce_50', 'exit'),
                           default='hold',
                           help='fixture 模型的持仓动作（确定性验收用）')
        if name == 'prepare-portfolio-review':
            p.add_argument('--session', required=True)
        if name == 'review-portfolio':
            p.add_argument('--execution-session', required=True)
            p.add_argument('--model', choices=('real', 'fixture'), default='fixture')
        if name == 'settle-session':
            p.add_argument('--session', required=True)
        if name in ('prepare-entry-reviews', 'prepare-position-reviews',
                    'prepare-portfolio-review', 'settle-session', 'run-daily'):
            p.add_argument('--etf-raw', help='活的 ETF 快照（交易日历与市场门来源）')
        if name == 'run-daily':
            p.add_argument('--session', help='默认取最新有行情的 session')
            p.add_argument('--evidence', help='已导入的规范证据存储')
            p.add_argument('--model', choices=('real', 'fixture'), default='real')
            p.add_argument('--fixture-action', choices=('PASS', 'VETO', 'ABSTAIN'),
                           default='PASS')
            p.add_argument('--position-fixture-action',
                           choices=('hold', 'reduce_25', 'reduce_50', 'exit'),
                           default='hold', help='持仓评审的 fixture 动作')
        if name == 'report':
            p.add_argument('--trace', action='append',
                           help='要展开的 opportunity_id（可重复）')
        if name == 'run-forward':
            p.add_argument('--to-session', required=True)
            p.add_argument('--evidence',
                           help='已导入的规范证据存储（由 import-evidence 产出）')
        if name != 'validate':          # validate 不写任何东西，不该要求 --output
            p.add_argument('--output', required=True)
        p.set_defaults(fn=fn)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == '__main__':
    raise SystemExit(main())
