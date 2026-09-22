"""逐页数据装配：把账本事实装成需求 §3 六个页面要的 section。

**分工**：算式一律来自 `scripts/portfolio_shadow/report.py`（同一份定义，两个 checkout
逐字节相同），本模块只负责「取哪些、怎么包、缺什么要说明」。词表来自 `vocabulary.py`
（自带超集），而不是包的默认词表 —— 那是本包存在的理由之一，见包 docstring。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import contract as C
from .manifest_view import ManifestView
from .vocabulary import PINNED_VOCABULARY, VOCABULARY_VERSION, classify_unknown

CHECKOUT = Path(__file__).resolve().parents[2]
US_ROOT = CHECKOUT / 'quant_us-main'
PANEL_DIR = 'survivor_sample_audit/asof_panels'


def _report():
    """延迟导入 `report.py`（算式唯一来源）。

    放在函数里是为了让 `import ops.analytics_export.sections` 在 sys.path 未就绪时也不炸。
    """
    if str(US_ROOT) not in sys.path:
        sys.path.insert(0, str(US_ROOT))
    from scripts.portfolio_shadow import report as rp
    return rp


# ---- 行情：只读面板（与引擎同一份输入，不另算一套）----------------------------
def panel_closes(base: Path, security_ids, session: str) -> dict:
    """`{security_id: 原始收盘(微美元)}`，取自 as-of 面板里该 session 那一行。

    面板是引擎自己的输入（`raw_close` 是不复权原始价、与执行价同尺度），所以这里读它
    与账本里的成交/止损同尺度。取不到就**不放进结果** —— 缺价由调用方显示「未采集」，
    绝不拿旧价冒充当日收盘。
    """
    if not session or not security_ids:
        return {}
    out = {}
    for sid in security_ids:
        code = 'US_' + str(sid).replace('SEC-US-', '')
        path = base / PANEL_DIR / f'{code}.csv.gz'
        if not path.exists():
            continue
        import pandas as pd
        df = pd.read_csv(path, usecols=['session', 'raw_close'])
        df['session'] = df['session'].astype(str)
        hit = df[df['session'] == str(session)]
        if len(hit):
            out[sid] = int(round(float(hit['raw_close'].iloc[0]) * 1_000_000))
    return out


# ---- 信封与来源 ---------------------------------------------------------------
def source_ref(entry: dict, *, schema_on_disk, extra=None) -> dict:
    ref = {
        'kind': entry['kind'],
        'run_dir': entry.get('run_dir'),
        'manifest': entry.get('manifest'),
        'ledger': entry.get('ledger'),
        'ledger_schema_on_disk': schema_on_disk,
        'ledger_schema_declared': entry.get('ledger_schema_expected'),
        'writer_checkout': entry.get('writer_checkout'),
        'writer_pin': entry.get('writer_pin'),
        'read_mode': 'sqlite mode=ro（本包唯一的打开方式）',
        'vocabulary_version': VOCABULARY_VERSION,
    }
    if extra:
        ref.update(extra)
    return ref


def strategy_version(manifest_view, entry) -> dict:
    d = manifest_view.raw if manifest_view else {}
    out = {
        'parent_strategy_id': d.get('parent_strategy_id'),
        'parent_version': d.get('parent_version'),
        'parent_code_hash': d.get('parent_code_hash'),
        'exit_policy_id': (d.get('execution_policy') or {}).get('exit_policy_id'),
        'entry_rule': (d.get('execution_policy') or {}).get('entry_rule'),
        'llm_overlay': (d.get('llm_policy') or {}).get('overlay'),
        'position_overlay': (d.get('llm_policy') or {}).get('position_overlay'),
        'manifest_hash': entry.get('manifest_hash'),
    }
    out.update(entry.get('strategy_version_extra') or {})
    return out


# ---- paper scope 的四个 section ------------------------------------------------
def _account_summary(store, manifest) -> list:
    out = []
    for scope in manifest.account_scopes:
        row = store.latest_state(scope)
        if row is None:
            out.append({'account_id': scope, 'status': C.NO_OBJECT})
            continue
        _seq, body = row
        navs = store.daily_nav(scope)
        latest = navs[-1] if navs else {}
        equity = latest.get('equity')
        full = latest.get('full_cost_equity')
        high = body.get('high_water')
        drawdown = (1.0 - full / high) if (full and high) else None
        positions = body.get('positions') or {}
        out.append({
            'account_id': scope, 'status': C.OK,
            'session': body.get('last_session'),
            'equity': C.metric(equity / 1e6 if equity is not None else None, 'USD'),
            'full_cost_equity': C.metric(full / 1e6 if full is not None else None, 'USD'),
            'cash_available': C.metric((body.get('cash_available') or 0) / 1e6, 'USD'),
            'drawdown': C.metric(drawdown * 100 if drawdown is not None else None, '%'),
            'positions_count': len(positions),
            'valuation_status': body.get('valuation_status'),
            'risk_state': body.get('risk_state'),
            'cost_status': body.get('cost_status'),
            'model_cost_uncertain_count': body.get('model_cost_uncertain_count', 0),
        })
    return out


def _positions_section(rows, scopes) -> dict:
    positions = []
    for scope in scopes:
        acct = (rows.get('accounts') or {}).get(scope) or {}
        for p in acct.get('positions') or []:
            p = dict(p)
            p['account_label'] = scope.rsplit(':', 1)[-1]
            positions.append(p)
    # 排序键（需求 §5.2）：显式给出，页面只按给定键排，**不含任何打分**
    for p in positions:
        gb = p.get('giveback') or {}
        p.setdefault('sort_keys', {})
        p['sort_keys'] = {
            'giveback_pp': gb.get('value_pp'),
            'holding_sessions': p.get('holding_sessions'),
            'reviewed': (p.get('review') or {}).get('reviewed'),
        }
    return {'positions': positions,
            'not_collected': [
                {'field': 'capacity_allocation',
                 'status': C.NOT_COLLECTED,
                 'why': '纸面引擎没有容量分配记录（五仓为运行时拒绝），不得由成交集合差异反推挤占'},
            ]}


def _opportunities_section(store, entry_metrics) -> dict:
    terminals = {}
    for o in store.opportunities():
        t = o.get('terminal', 'WAITING')
        terminals[t] = terminals.get(t, 0) + 1
    total = sum(terminals.values())
    # 未成交原因：**照实统计应用动作与原因码**（第二批要求的事后归因）
    by_reason = {}
    for a in store.applications():
        code = a.get('reason_code') or a.get('action') or '(无)'
        by_reason[code] = by_reason.get(code, 0) + 1
    em = entry_metrics
    stages = [
        {'stage': '候选机会', 'count': total, 'status': C.OK if total else C.NO_OBJECT},
        {'stage': '应评审', 'count': em['eligible_for_review'], 'status': C.OK},
        {'stage': '数据拦截', 'count': em['data_blocked'], 'status': C.OK},
        {'stage': '质量弃权', 'count': em['quality_abstain'], 'status': C.OK},
        {'stage': '模型可评审', 'count': em['callable_for_model'], 'status': C.OK},
        {'stage': '已评审', 'count': em['reviewed'], 'status': C.OK},
        {'stage': '实际改变计划', 'count': em['plan_change_count'], 'status': C.OK},
        # 容量分配：纸面侧**没有采集**这个阶段 —— 显示「该阶段未采集」而不是 0
        {'stage': '容量分配', 'count': None, 'status': C.NOT_COLLECTED,
         'why': '纸面引擎按运行时「五仓/重复持仓」拒绝，不产生分配记录'},
    ]
    return {
        'funnel': {'total': total, 'by_terminal': terminals, 'stages': stages},
        'entry_metrics': em,
        'by_reason_code': by_reason,
        'not_collected': [
            {'field': 'capacity_allocation', 'status': C.NOT_COLLECTED,
             'why': '纸面引擎按运行时「五仓/重复持仓」拒绝，不产生分配记录 ⇒ '
                    '不得由成交集合差异反推挤占（需求 §5.3）'},
            {'field': 'bottom_signal_state', 'status': C.NOT_COLLECTED,
             'why': '纸面前向路径不跑抄底信号（那是研究侧的单向对照实验，未接入前向）'},
        ],
        'ranking_used': '规则排名（selection 的 portfolio_rank 仅在 Portfolio 角色启用且有冲突时参与）',
    }


def _llm_impact_section(store, manifest, entry_metrics, position_metrics, paired) -> dict:
    """角色、决策清单、参与漏斗、R/L 配对、成本预算。

    **`portfolio`/`review` 两个角色标「未采集」**：现有 metrics 接口只覆盖
    selection/entry/position（需求 §2 明写「不能直接假设覆盖全部角色」）。填 0 会把
    「没有这个数据源」显示成「这个角色什么都没做」。
    """
    llm = manifest.llm_policy or {}
    roles = []
    for role, note in (('selection', '实盘决策层，与影子实验无关'),
                       ('entry', '本实验的入场否决（overlay=entry_veto）'),
                       ('position', '持仓评审（overlay=position_action）'),
                       ('portfolio', None), ('review', None)):
        roles.append({
            'role': role,
            'enabled': role in ('entry', 'position') or role == 'selection',
            'note': note,
            'status': C.OK if note else C.NOT_COLLECTED,
            'why': '' if note else '现有 metrics 接口只覆盖 selection/entry/position，本角色无读取来源',
        })
    apps = store.applications()
    table = []
    for a in sorted(apps, key=lambda x: (x.get('scope', ''), x.get('opportunity_id', ''))):
        raw = a.get('raw_action') or ''
        table.append({
            'opportunity_id': a.get('opportunity_id'),
            'scope': a.get('scope'), 'role': 'entry',
            'action': a.get('action'), 'raw_action': raw or None,
            'degraded_from': raw if raw and raw != a.get('action') else None,
            'reason_code': a.get('reason_code'),
            'decision_frozen': a.get('decision_frozen'),
            'execution_applied': a.get('execution_applied'),
            'model_cost_usd': (a.get('model_cost') or 0) / 1e6,
            'cost_uncertain': a.get('cost_uncertain', False),
            # 需求 §5.4：结果成熟状态
            'maturity': 'PENDING_SETTLEMENT' if not a.get('execution_applied') else 'IMMATURE',
        })
    # 成本预算：口径与 `overlay_review.budget_usage` 同义，但**自己数在飞的尝试**
    # （pin 的 store 没有 `in_flight_attempts`，见包 docstring）
    budget_micro = llm.get('model_budget_micro')
    reserve_micro = llm.get('model_call_reserve_micro') or 0
    l_scope = next((s for s in manifest.account_scopes if s.endswith(':L')), None)
    spent = unsettled = inflight = 0
    if l_scope:
        for a in store.applications(l_scope):
            spent += a.get('model_cost') or 0
            if a.get('cost_uncertain'):
                unsettled += 1
        # 在飞 = 状态为 CALL_STARTED 的尝试（**原始 SQL 计数**：pin 的 store 没有
        # `in_flight_attempts`，见包 docstring）
        inflight = len(_call_started_keys(store))
    reserved = (unsettled + inflight) * reserve_micro
    cost = {
        'known_cost_usd': spent / 1e6,
        'unknown_cost_count': unsettled,
        'in_flight_count': inflight,
        'reserved_usd': reserved / 1e6,
        'budget_usd': (budget_micro / 1e6) if budget_micro is not None else None,
        'remaining_usd': ((budget_micro - spent - reserved) / 1e6)
        if budget_micro is not None else None,
        'status': C.OK if budget_micro is not None else C.NOT_COLLECTED,
        'why': '' if budget_micro is not None else '该实验未声明模型调用预算',
        'cost_basis': '实际已知费用 + (金额未知 + 在飞) × 单次预留',
    }
    nav_r, nav_l = [], []
    if len(manifest.account_scopes) >= 2:
        r_scope, l_scope2 = manifest.account_scopes[0], manifest.account_scopes[1]
        r_navs = {n['session']: n for n in store.daily_nav(r_scope)}
        l_navs = {n['session']: n for n in store.daily_nav(l_scope2)}
        initial = manifest.initial_cash
        for s in sorted(set(r_navs) & set(l_navs)):
            r_full = r_navs[s].get('full_cost_equity')
            l_full = l_navs[s].get('full_cost_equity')
            if r_full is None or l_full is None:
                continue
            nav_r.append({'session': s, 'return_pct': (r_full - initial) / initial * 100})
            nav_l.append({'session': s, 'return_pct': (l_full - initial) / initial * 100})
    return {
        'roles': roles,
        'decision_table': table,
        'participation_funnel': {'entry': entry_metrics, 'position': position_metrics},
        'paired': paired,
        'nav_r': nav_r, 'nav_l': nav_l,
        'cost_budget': cost,
        # 第二批要求「最有帮助与最有损害的决策都可下钻」与「提前退出的收益与损害案例」。
        # **没有样本就不排序**：全项目当前 1 笔成交、0 条成熟结果，按 R 差值排序等于在
        # 噪声上排名次。宁可显示未采集 + 原因（需求 §5.4 末段：无实际干预样本时如实显示）。
        'cases': {
            'best': [], 'worst': [],
            'status': C.NOT_COLLECTED,
            'why': '逐笔 L−R 损益差需要已成熟的结果；当前没有足够的终结决策可排序',
            'early_exit_harm': {
                'status': C.NOT_COLLECTED,
                'why': '提前退出的收益/损害案例取自 S1 同机会对照（见「策略与实验」页），'
                       '本实验的前向路径尚无样本',
            },
        },
        'vocabulary_version': VOCABULARY_VERSION,
    }


def _call_started_keys(store) -> list:
    """状态为 CALL_STARTED 的尝试键（**只读原始 SQL**；pin 的 ORM 没有这个方法）。"""
    from .rosqlite import open_ro
    path = getattr(store, 'path', None)
    exp = getattr(store, 'experiment_id', None)
    if not path or not exp:
        return []
    with open_ro(path) as con:
        return [r[0] for r in con.execute(
            "SELECT job_key FROM shadow_job_runs WHERE experiment_id=? AND status='CALL_STARTED'",
            (exp,))]


def _overview_section(manifest, accounts, entry_metrics, position_metrics, entry) -> dict:
    llm = manifest.llm_policy or {}
    protocol = manifest.evaluation_protocol or {}
    period = {
        'opportunities': entry_metrics['eligible_for_review'],
        'reviewed': entry_metrics['reviewed'],
        'model_valid_reviews': entry_metrics['real_model_reviews'],
        'plan_changed': entry_metrics['plan_change_count'],
        'position_path_changed': position_metrics['path_changed'],
        'matured_samples': None,
        'maturity_status': C.NOT_COLLECTED,
        'maturity_why': '结果成熟度按登记口径统计，当前尚无已成熟样本来源接入本页',
    }
    return {
        'accounts': accounts,
        'period_activity': period,
        'llm_scope': {
            'overlay': llm.get('overlay'), 'position_overlay': llm.get('position_overlay'),
            'use_real_model': llm.get('use_real_model'),
            'evidence_mode': llm.get('evidence_mode'),
            'permission_note': ('主链 LLM 权限为 shadow（只记录不改变执行路径）；'
                                '本实验是**独立纸面账户**，权限与主链配置是两件事'),
        },
        'in_progress': [{
            'what': protocol.get('main_metric') or 'L_minus_R_return',
            'enrollment_window': protocol.get('enrollment_window'),
            'review_date': protocol.get('review_date'),
            'min_decisions': protocol.get('min_decisions'),
            'status': 'RUNNING',
        }],
        'forward_start_session': entry.get('forward_start_session'),
        'boundary_source': entry.get('boundary_source'),
        'todo': [],   # 纸面侧当前没有需要人工处理的事项；人工确认入口在 /approvals
    }


def paper_scope_sections(store, entry, *, prices=None, base=None) -> tuple:
    """一个 paper scope 的全部 section。返回 `(sections, manifest_view)`。

    manifest 视图一并返回，因为信封要它的 `strategy_version` —— 不在这里返回，调用方就
    得自己再构造一份，那就是「同一件事两份定义」。
    """
    rp = _report()
    vocab = PINNED_VOCABULARY
    manifest = ManifestView(json.loads((base / entry['manifest']).read_text(encoding='utf-8'))) \
        if entry.get('manifest') else _arm_manifest_view(store, entry)
    em = rp.entry_metrics(store, manifest, vocab)
    pm = rp.position_metrics(store, manifest, vocab)
    # 配对绩效需要**两个**账户；三臂实验只有一个（只差宇宙，没有 R/L 配对）⇒ 不适用，
    # 而不是算出一个假数（需求 §7：不适用与 0 是两件事）。
    if len(manifest.account_scopes) >= 2:
        paired = rp.paired_performance(store, manifest, None, vocab)
    else:
        paired = {'applicable': False, 'status': C.NOT_APPLICABLE,
                  'why': '本实验只有一个账户（三臂只差宇宙，没有 R/L 配对）'}
    rows = rp.position_rows(store, manifest, vocab, prices)
    accounts = _account_summary(store, manifest)
    return {
        'overview': _overview_section(manifest, accounts, em, pm, entry),
        'positions': _positions_section(rows, manifest.account_scopes),
        'opportunities': _opportunities_section(store, em),
        'llm_impact': _llm_impact_section(store, manifest, em, pm, paired),
        'accounts': accounts,
    }, manifest


def live_sections(base: Path, entry: dict) -> tuple:
    """实盘链路（SIMULATE / DRY-RUN）。**只读 `execution.sqlite3` 的 `books` 表。**

    刻意不走 `PositionRegistry`：它的 `transaction()` 退出时**无条件**
    `INSERT OR REPLACE INTO books` + `commit`（`position_registry.py`）⇒ `registry.all()`
    其实是一次写操作。页面要的只是那两个 JSON blob，直接 `mode=ro` 读即可。

    口径必须标清：`trd_env=SIMULATE` ⇒ 这是**模拟成交**，不是真实账户。
    """
    from .rosqlite import open_ro
    db = base / entry['db']
    if not db.exists():
        raise FileNotFoundError(f'EXECUTION_DB_MISSING:{db}')
    books = {}
    with open_ro(db) as con:
        for ns, payload in con.execute('SELECT namespace, payload FROM books'):
            books[ns] = json.loads(payload)
    accounts, positions = [], []
    for ns in entry['namespaces']:
        book = books.get(ns) or {'positions': {}, 'orders': {}}
        pos = book.get('positions') or {}
        accounts.append({
            'account_id': ns, 'status': C.OK,
            'positions_count': len(pos), 'orders_count': len(book.get('orders') or {}),
            'equity': C.metric(None, 'USD', status=C.NOT_COLLECTED,
                               why='实盘账本不保存净值序列（净值在监控器内存里）'),
        })
        for code, p in sorted(pos.items()):
            positions.append({'account_id': ns, 'security_id': code, **p})
    sections = {
        'overview': {
            'accounts': accounts,
            'period_activity': {
                'opportunities': C.metric(None, status=C.NOT_COLLECTED,
                                          why='实盘层的候选漏斗不在本页数据源里'),
                'reviewed': C.metric(None, status=C.NOT_COLLECTED, why='同上'),
            },
            'llm_scope': {
                'permission_note': '主链 LLM 权限为 shadow ⇒ 校验通过的决策也不改变执行路径；'
                                   '本范围是**模拟成交**（trd_env=SIMULATE / DRY-RUN）',
            },
            'in_progress': [], 'todo': [],
        },
        'positions': {'positions': positions, 'not_collected': [
            {'field': 'protection_state', 'status': C.NOT_COLLECTED,
             'why': '实盘持仓的保护线由 chandelier 监控器持有（内存），账本里没有落盘副本'},
        ]},
        'opportunities': {'funnel': {'total': None, 'by_terminal': {}, 'stages': [
            {'stage': '候选机会', 'count': None, 'status': C.NOT_COLLECTED,
             'why': '实盘提案漏斗不在本页数据源里'},
        ]}},
        'llm_impact': {'roles': [], 'decision_table': [], 'paired': {},
                       'cost_budget': {'status': C.NOT_COLLECTED,
                                       'why': '实盘层没有模型成本账'}},
    }
    return sections, {'kind': 'live_simulated', 'db': entry['db'],
                      'namespaces': entry['namespaces'],
                      'note': '模拟成交（SIMULATE / DRY-RUN），不是真实资金账户'}



def _arm_manifest_view(store, entry):
    """三臂没有落盘的 manifest（`arm_manifest()` 是确定性重建、且带 `MANIFEST_CHANGED_
    SINCE_FREEZE` 守卫）⇒ 这里**重建最小视图**，只填 `report.py` 用得到的字段。

    刻意不调 `forward_arms.arm_manifest()`：那会 import 冻结模块，而导出器不该依赖
    运行版本里的策略代码。
    """
    exp = entry['experiment_id']
    return ManifestView({
        'experiment_id': exp, 'status': 'FROZEN',
        'account_scopes': [a['account_id'] for a in entry['accounts']],
        'initial_cash': 100000.0,
        'parent_strategy_id': 'B3', 'parent_version': '1',
        'llm_policy': {'overlay': 'fixed_pass', 'use_real_model': False},
        'execution_policy': {'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': 60},
        'evaluation_protocol': {},
    })


MAX_LIST = 8
MAX_STR = 600


def compact(obj, *, depth=0):
    """把大产物压成「页面首屏够用」的形状，**长数组只留前几项并标明总数**。

    实测：`comparison.json` 1.9MB、`step1.json` 372KB（都是逐笔数组）⇒ 四份产物把
    `/api/analytics/experiments` 顶到 2.2MB。页面首屏要的是判定与头部数字，不是全量逐笔；
    全量另存 `artifacts/` 由页面按需取。**截断必须可见**（`_truncated`），不静默丢。
    """
    if depth > 4:
        return '…'
    if isinstance(obj, dict):
        return {k: compact(v, depth=depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        head = [compact(v, depth=depth + 1) for v in obj[:MAX_LIST]]
        if len(obj) > MAX_LIST:
            return {'_truncated': True, '_total': len(obj), 'head': head}
        return head
    if isinstance(obj, str) and len(obj) > MAX_STR:
        return obj[:MAX_STR] + f'…（{len(obj)} 字）'
    return obj


# ---- 策略对比（需求 §5.5 第二批）：四份研究结论的同一张表 --------------------
# 每份研究的产物形状**不一样**（诊断研究是 comparison+statistics，策略研究是
# step1/entry_arms/sleeve），所以这里显式按形状取值，取不到的**如实标未采集**，
# 绝不填 0 —— 需求 §7 的那条规则在跨实验的表上同样适用。
def _m(value, unit=None, *, scale=1.0, status=None, why='', label=None):
    if value is None:
        return C.metric(None, unit, status=status or C.NOT_COLLECTED, why=why, label=label)
    return C.metric(value * scale, unit, status=status, label=label)


def _dig(d, *path):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _source_ref(base: Path, entry: dict, art_key: str) -> dict:
    import hashlib
    rel = (entry.get('artifacts') or {}).get(art_key)
    if not rel:
        return {}
    p = base / entry['run_dir'] / rel
    if not p.exists():
        return {'file': rel, 'status': C.READ_FAILED}
    return {'file': rel, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()[:16],
            'path': str(p)}


def normalize_study(base: Path, entry: dict, research: dict) -> dict:
    """把一份研究产物归一成「四个数 + 判定 + 来源」一行。形状不认识就如实说。"""
    art = research or {}
    row = {'scope_id': entry['scope_id'], 'label': entry.get('label'),
           'run_dir': entry.get('run_dir'), 'status': C.OK, 'metrics': {}, 'verdict': {},
           'deltas': {}, 'sample': {}, 'source': {}, 'kind': None, 'notes': []}
    cmp_ = art.get('comparison') or {}
    stats = art.get('statistics') or {}
    result = art.get('result') or {}

    if cmp_.get('full_cost_return') is not None:               # 诊断：冻结基线本身
        row['kind'] = 'baseline'
        row['metrics'] = {
            'return_pct': _m(cmp_['full_cost_return'], '%', scale=100),
            'mdd_pct': _m(cmp_.get('max_drawdown'), '%', scale=100),
            # ← 胜率在 `comparison.exits` 里，**不在** `statistics` 里（第一次就取错了）
            'win_rate_pct': _m(_dig(cmp_, 'exits', 'realized_win_rate'), '%', scale=100),
            'tail': _m(None, 'R', why='基线产物未单列尾部（ES / 最差单笔）；同机会对照见 S1 行'),
        }
        row['verdict'] = {'token': cmp_.get('phase_conclusion'), 'verdict': cmp_.get('verdict'),
                          'why': '只有基线、没有 challenger ⇒ verdict 为 null 是设计（§9.3）'}
        row['sample'] = {'sessions': cmp_.get('sessions'),
                         'trades': _dig(cmp_, 'exits', 'count'),
                         'closed': _dig(cmp_, 'exits', 'closed'),
                         'right_censored': _dig(cmp_, 'exits', 'right_censored')}
        row['concentration'] = {
            'top1_security_share_of_gain': _m(
                _dig(stats, 'concentration', 'top1_security_share_of_gain'), '%', scale=100),
            'top1_trade_share_of_gain': _m(
                _dig(stats, 'concentration', 'top1_trade_share_of_gain'), '%', scale=100)}
        row['source'] = _source_ref(base, entry, 'comparison')
        row['checks'] = cmp_.get('checks')
    elif _dig(result, 'summary', 'sum_net_r_A') is not None:   # S1 利润保护
        row['kind'] = 'exit_protection'
        s = result.get('summary') or {}
        row['arms'] = {
            'A_规则基线': {'sum_net_r': _m(s.get('sum_net_r_A'), 'R'),
                          'worst_trade_r': _m(s.get('worst_a'), 'R'),
                          'tail_es': _m(s.get('tail_es_a'), 'R'),
                          'profitable': _m(s.get('n_profitable_a'), '笔')},
            'B_加利润保护': {'sum_net_r': _m(s.get('sum_net_r_B'), 'R'),
                            'worst_trade_r': _m(s.get('worst_b'), 'R'),
                            'tail_es': _m(s.get('tail_es_b'), 'R'),
                            'profitable': _m(s.get('n_profitable_b'), '笔')}}
        row['metrics'] = {
            'sum_net_r_A': _m(s.get('sum_net_r_A'), 'R'),
            'sum_net_r_B': _m(s.get('sum_net_r_B'), 'R'),
            # 胜率只给计数 ⇒ 只报计数。比值口径（分母是 197 还是 201）要看报告，不在这里除。
            'win_rate_pct': _m(None, '%', why='产物只给计数（n_profitable/n_closed），'
                                              '比值口径需看报告，不在此处相除'),
            'tail': _m(s.get('worst_b'), 'R', label='B 臂最差单笔 R'),
        }
        row['deltas'] = {'sum_net_r': _m((s.get('sum_net_r_B') or 0) - (s.get('sum_net_r_A') or 0)
                                         if s.get('sum_net_r_A') is not None else None, 'R'),
                         'tail_es_improvement': _m(s.get('tail_es_improvement'), 'R'),
                         'giveback_reduction_pct': _m(s.get('giveback_reduction_pct'), '%',
                                                      scale=100)}
        row['verdict'] = {'token': _dig(result, 'verdict', 'token'),
                          'why': _dig(result, 'verdict', 'note') or ''}
        row['sample'] = {'trades': s.get('n_trades'), 'closed': s.get('n_closed'),
                         'activated': s.get('n_activated'), 'untouched': s.get('n_untouched')}
        row['source'] = _source_ref(base, entry, 'result')
    elif _dig(result, 'a', 'cagr') is not None:                # 抄底 sleeve
        row['kind'] = 'entry_sleeve'
        a, b = result.get('a') or {}, result.get('b') or {}
        row['arms'] = {
            'A_规则基线': {'return_pct': _m(a.get('total_return'), '%', scale=100),
                          'cagr_pct': _m(a.get('cagr'), '%', scale=100),
                          'mdd_pct': _m(a.get('mdd'), '%', scale=100),
                          'win_rate_pct': _m(a.get('win_rate'), '%', scale=100),
                          'payoff': _m(a.get('profit_loss_ratio')),
                          'worst_trade_r': _m(a.get('worst_trade_r'), 'R')},
            'B_加抄底 sleeve': {'return_pct': _m(b.get('total_return'), '%', scale=100),
                                'cagr_pct': _m(b.get('cagr'), '%', scale=100),
                                'mdd_pct': _m(b.get('mdd'), '%', scale=100),
                                'win_rate_pct': _m(b.get('win_rate'), '%', scale=100),
                                'payoff': _m(b.get('profit_loss_ratio')),
                                'worst_trade_r': _m(b.get('worst_trade_r'), 'R')}}
        row['metrics'] = row['arms']['B_加抄底 sleeve']
        row['deltas'] = {'terminal_return_pp': _m(result.get('delta_terminal_return'), 'pp',
                                                  scale=100),
                         'mdd_pp': _m(result.get('delta_mdd'), 'pp', scale=100),
                         'worst_trade_r': _m(result.get('delta_worst_trade_r'), 'R')}
        row['verdict'] = {'token': 'RISK_REJECTED',
                          'why': '收益与风险同时变差（登记判据）'}
        row['sample'] = {'n_a': _dig(result, 'entries', 'n_a'),
                         'n_b': _dig(result, 'entries', 'n_b'),
                         'common': _dig(result, 'entries', 'common')}
        row['source'] = _source_ref(base, entry, 'result')
    elif _dig(result, 'comparison', 'delta_terminal_return') is not None:   # 抄底替换
        row['kind'] = 'entry_replacement'
        d = result['comparison']
        row['metrics'] = {'return_pct': _m(None, '%', why='该产物只给差值 ⇒ 与上面的基线行并列看，'
                                                            '不在此处相加合成（合成出来的数没人复核）'),
                          'mdd_pct': _m(None, '%', why='同上'),
                          'win_rate_pct': _m(None, '%', why='同上'),
                          'tail': _m(d.get('delta_worst_trade_r'), 'R', label='最差单笔 R 的**变化**')}
        row['deltas'] = {'terminal_return_pp': _m(d.get('delta_terminal_return'), 'pp', scale=100),
                         'cagr_pp': _m(d.get('delta_cagr'), 'pp', scale=100),
                         'mdd_pp': _m(d.get('delta_mdd'), 'pp', scale=100),
                         'stress_2x_terminal_return_pp': _m(d.get('delta_terminal_return_2x'),
                                                            'pp', scale=100)}
        row['verdict'] = {'token': _dig(result, 'verdict', 'token'),
                          'why': _dig(result, 'verdict', 'note') or ''}
        row['sample'] = {'n_min': _dig(result, 'verdict', 'n_min'),
                         'bottom_signals': _dig(result, 'bottom_signals', 'n_opportunities')}
        row['source'] = _source_ref(base, entry, 'result')
    else:
        row['status'] = C.NOT_COLLECTED
        row['notes'].append('产物形状未被识别 —— 不猜数字，请先看原始产物')
    return row


def research_sections(base: Path, entry: dict) -> dict:
    """研究实验的 section：读产物文件，**不重算**。

    需求 §5.5 要求「文档判定与机读结果冲突时显示结论待核对」。这里把两者都原样带出，
    由页面并列显示，不在导出器里做调和。
    """
    art = entry.get('artifacts') or {}
    loaded, missing, full = {}, [], {}
    for key, rel in art.items():
        p = base / entry['run_dir'] / rel
        if not p.exists():
            missing.append({'field': f'{entry["run_dir"]}/{rel}', 'status': C.NOT_COLLECTED,
                            'why': '产物文件不存在'})
            continue
        if p.suffix == '.md':
            loaded[key] = p.read_text(encoding='utf-8')
        else:
            payload = json.loads(p.read_text(encoding='utf-8'))
            full[key] = payload
            loaded[key] = compact(payload)
    return {'research': loaded, 'missing_artifacts': missing, 'full': full}
