"""Shared scanner hooks and bounded advisory-only model worker queue."""
import itertools
import logging
import queue
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .event_store import digest, stable_id, utc

logger = logging.getLogger(__name__)


def enabled(owner):
    return bool(owner.config.get('llm_decision', {}).get('enabled', True))


def prepare(owner):
    from scripts.live_trading.execution import service_for
    store = owner.approval_store
    if store is None:
        raise ValueError('缺少审批/计划存储')
    service = service_for(owner)
    if service.registry.namespace == 'unconfigured':
        if owner.dry_run:
            service.registry.configure('DRY-RUN')
        else:
            with owner.pool.get_trade_ctx() as ctx:
                service.account(ctx)
    if store.registry is not service.registry:
        raise ValueError('审批和执行必须使用同一账户数据库')
    return store


def bar_time(value, daily=False):
    if daily:
        # Signals only become available after the signal session closes.
        value = datetime.fromisoformat(str(value)[:10]).replace(hour=16, tzinfo=ZoneInfo('America/New_York'))
    elif isinstance(value, str):
        value = datetime.fromisoformat(value)
    return utc(value)


def news_evidence(context, subject_code=None):
    from mutifactor.llm.trade_review import evidence
    result = []
    for row in (context or {}).get('news', []):
        # Missing publication times are displayed as unverified context only.
        if row.get('url') and row.get('published_at') and row.get('observed_at') and row.get('title'):
            try:
                item = evidence(row['title'], row['url'], row['observed_at'], row['published_at'],
                                cluster_id=row.get('cluster_id'), kind='news')
                item['subject_code'] = subject_code
                result.append(item)
            except (ValueError, TypeError):
                logger.warning('新闻时间无效，保留为未验证上下文')
    return result


def account_totals(owner, positions=None):
    """账户权益与可用现金。dry-run 用配置里的虚拟权益，live 走 Futu。

    从 `risk_preview` 里抽出来共用：Portfolio 咨询（`_consult_portfolio`）也要这两个数，
    抄第二份就会漂移。
    """
    from scripts.live_trading.execution import service_for
    cfg = owner.config.get('risk_budget', {})
    if owner.dry_run:
        equity = float(cfg.get('dry_run_equity', 100000))
        held = positions
        if held is None:
            with prepare(owner).registry.transaction() as book:
                held = list(book['positions'].values())
        return equity, equity - sum(float(p['qty']) * float(p['entry_price']) for p in held)
    from futu import RET_OK, Currency
    service = service_for(owner)
    with owner.pool.get_trade_ctx() as ctx:
        args = service.account(ctx)
        ret, rows = ctx.accinfo_query(currency=Currency.USD, refresh_cache=True, **args)
        if ret != RET_OK or rows.empty:
            raise ValueError('无法取得账户权益/现金')
        return float(rows.iloc[0]['total_assets']), float(rows.iloc[0]['cash'])


def risk_preview(owner, code, price, stop, capital_cap, quantity_cap):
    """Read-only indicative sizing. Execution repeats every check using fresh data."""
    from scripts.live_trading.execution import risk_quantity
    store = prepare(owner)
    cfg = owner.config.get('risk_budget', {})
    with store.registry.transaction() as book:
        positions = list(book['positions'].values())
        orders = list(book['orders'].values())
    equity, cash = account_totals(owner, positions=positions)
    group = cfg.get('code_groups',{}).get(code)
    # 在途买入提案也占容量 —— 不折算进来，这一单会按"容量全空"给出数量，
    # 而那个数量是假的（同时挂着的其它提案并不会消失）。
    from scripts.live_trading.execution import proposal_reservations
    reservations = proposal_reservations(
        [p for p in store.active_buys() if p.get('stock_code') != code],
        code_groups=cfg.get('code_groups', {}), equity=equity)
    quantity, risk = risk_quantity(float(price),float(stop),equity,cash,positions,orders,cfg,group,
                                   min(capital_cap,quantity_cap*price),
                                   proposals=reservations)
    return dict(quantity=quantity, equity=equity, cash=cash, budget_risk=risk,
                planned_r=quantity*abs(price-stop),nav_fraction=quantity*price/equity,
                risk_nav_fraction=risk/equity,risk_group=group,
                existing_account_risk=sum(float(p.get('initial_risk') or 0) for p in positions),
                existing_group_risk=sum(float(p.get('initial_risk') or 0) for p in positions if p.get('risk_group')==group),
                observed_at=utc(),scope=store.events.scope)


def candidate(owner, code, mode, bar_end, passed, details, timeframe=None):
    if not enabled(owner):
        return {}
    store = prepare(owner)
    tf = timeframe or ('1d' if mode == 'donchian' else '15m')
    config_key = 'trend_breakout' if mode == 'donchian' else mode
    version = 'rules-v1:' + digest({'strategy': owner.config.get(config_key, {}),
                                  'timeframe': tf, 'cohort': 'scored-input-v1'})[:16]
    end = bar_time(bar_end, daily=tf == '1d')
    signal_id = stable_id('signal', store.events.scope, code, version, end, mode)
    payload = dict(stock_code=code, strategy=mode, strategy_version=version, timeframe=tf,
                   signal_bar_end=end, passed=bool(passed), details=details,
                   risk_group=owner.config.get('risk_budget', {}).get('code_groups', {}).get(code),
                   candidate_policy='scored-input-v1')
    # The candidate identity must not include volatile quote/context fields.
    kind = 'rule_candidate' if passed else 'rule_rejected'
    with store.events.transaction() as con:
        from .event_store import insert_event, make_event
        # Freeze the first observable classification of a given bar.
        existing = con.execute('SELECT 1 FROM decision_events WHERE event_id=?',
                               (stable_id('event', store.events.scope, kind, signal_id),)).fetchone()
        if not existing:
            insert_event(con, make_event(store.events.scope, kind, signal_id, payload, signal_id=signal_id))
    return dict(signal_id=signal_id, strategy_version=version, timeframe=tf,
                quote_observed_at=utc(), max_price_drift_pct=float(owner.config.get('trading', {}).get(
                    'live_trading', {}).get('human_approval', {}).get('max_price_drift_pct', .03)))


class ReviewQueue:
    def __init__(self, workers=2, capacity=32):
        self.jobs = queue.PriorityQueue(maxsize=capacity)
        self.seq = itertools.count()
        for i in range(workers):
            threading.Thread(target=self._run, daemon=True, name=f'plan-review-{i}').start()

    def _run(self):
        while True:
            _, _, task = self.jobs.get()
            try:
                task()
            except Exception:
                logger.exception('LLM复核处理失败；保持审批阻塞')
            finally:
                self.jobs.task_done()

    def put(self, priority, task):
        self.jobs.put_nowait((priority, next(self.seq), task))


_queue = None
_lock = threading.Lock()


def review_queue(cfg):
    global _queue
    with _lock:
        if _queue is None:
            _queue = ReviewQueue(max(1, int(cfg.get('workers', 2))), max(1, int(cfg.get('queue_capacity', 32))))
    return _queue


def _consult_portfolio(owner, item):
    """提案评审时的一次 Portfolio 容量咨询（设计 §6.3）。**失败不影响提案与 Entry 主链。**

    为什么放在这里：在途提案对 `risk_quantity` **不可见**（`pending` 不是订单），并发的待审
    提案互相看不见、各自独立通过定仓；真正的容量约束要到 `execution.py` 提交时以**拒绝**的
    形式出现，而那里一次只看得到一条。**提案评审时是唯一能看到竞争全貌的位置。**
    详见 `scripts/live_trading/portfolio_allocation.py` 的模块说明。
    """
    cfg = (owner.config.get('llm_decision') or {}).get('portfolio_review') or {}
    if not cfg.get('enabled', True):
        return None
    advisor = getattr(owner, 'llm_advisor', None)
    if advisor is None:
        return None            # 没有可用模型：不建包、不认领、不记决策
    try:
        from scripts.live_trading import portfolio_allocation
        equity, cash = account_totals(owner)
        return portfolio_allocation.consult(
            prepare(owner).registry, owner.approval_store, owner.config,
            equity=equity, cash=cash, advisor=advisor, as_of=utc(),
            session=datetime.now(ZoneInfo('America/New_York')).date().isoformat())
    except Exception:
        logger.exception('Portfolio 容量咨询失败（不影响提案与 Entry 主链）')
        return None


def start_review(owner, item):
    if not enabled(owner) or not item.get('plan_id'):
        return
    store = owner.approval_store
    request = store.begin_review(item['id'])
    if request is None:
        return
    cfg = owner.config.get('llm_decision', {})
    advisor = getattr(owner, 'llm_advisor', None)

    def run():
        raw, metadata = None, {}
        if item.get('side', 'buy') == 'buy':
            # 放在 try **之外**是刻意的：它自己吞掉全部异常。放进 try 里一旦抛错就会落到
            # 下面的 legacy 分支，把 Entry 主链带偏 —— 容量咨询绝不能有那种能力。
            _consult_portfolio(owner, item)
        try:
            if time.time() < request['expires_at'] and advisor is not None:
                snapshot = store.events.get_snapshot('input', request['input_snapshot_id'])
                from scripts.live_trading.decision_runtime import DecisionRuntime
                runtime = DecisionRuntime(store.registry, advisor, owner.config)
                if runtime.is_shadow('entry'):
                    from scripts.live_trading.decision_bridge import build_entry_packet
                    from mutifactor.llm.contracts.entry_v2 import build_review_trigger
                    plan = snapshot['plan']
                    constraints = plan.get('entry_constraints') or {}
                    risk = plan.get('risk') or {}
                    packet = build_entry_packet(
                        signal={
                            'signal_id': request['signal_id'],
                            'strategy': plan.get('strategy'),
                            'rule_baseline': 'execute_now',
                        },
                        plan=plan, evidence=list(snapshot.get('evidence') or []),
                        account_scope=store.registry.namespace,
                        subject_id=f"{request['signal_id']}:{request['plan_version']}",
                        as_of=snapshot.get('observed_at') or utc(),
                        standard_quantity=int(float(constraints.get('quantity_cap') or request['quantity'])),
                        entry_price=float(constraints.get('price') or request['price']),
                        initial_stop=float(risk.get('initial_stop')),
                        expires_at=utc(request['expires_at']),
                        review_triggers=[build_review_trigger(
                            f"{request['review_id']}:expiry", 'scheduled_time',
                            {'at': utc(request['expires_at'])})],
                        model={'provider': 'configured',
                               'model_id': getattr(advisor, 'model', ''),
                               'temperature': 0.0, 'timeout_seconds': 30})
                    result = runtime.engine().decide_entry(packet)
                    if result.status == 'validated':
                        try:
                            from .entry_counterfactual import EntryCounterfactualLedger
                            frozen = EntryCounterfactualLedger(store.registry).freeze(
                                packet, result, proposal_id=request['id'],
                                review_id=request['review_id'])
                            request['counterfactual_id'] = frozen['counterfactual_id']
                        except Exception:
                            logger.exception('Entry 反事实冻结失败；不影响评审主链')
                    store.complete_v2_review(
                        request, result, ttl=float(cfg.get('review_ttl_seconds', 180)))
                    store.events.export()
                    return
                raw = advisor.review_plan(snapshot, request.get('side', 'buy'))
                metadata = getattr(advisor, 'last_metadata', {})
        except Exception:
            logger.exception('结构化LLM调用失败')
        store.complete_review(request, raw, getattr(advisor, 'model', ''), metadata,
                              ttl=float(cfg.get('review_ttl_seconds', 180)), ttls=cfg.get('evidence_ttl_seconds'))
        store.events.export()

    try:
        review_queue(cfg).put(item.get('priority', 1), run)
    except queue.Full:
        store.complete_review(request, None, getattr(advisor, 'model', ''))
        logger.warning('LLM队列已满，提案保持阻塞')
