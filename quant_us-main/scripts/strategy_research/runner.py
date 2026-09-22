"""卖出侧第一批：机械利润保护的同机会对照（预登记 `EXIT-PROTECT-20260921`）。

**先登记再计算**：运行前必须存在预登记文件，脚本把它的 sha256 写进产物，
使「登记先于结果」可核对。

第 1 步（机会级，`step1`）：固定同一批成交、同一条价格路径、同一股数，两臂只差
**保护线是否启用**。用 `paper_engine.step` 驱动 —— 与账户级用的是同一个引擎，所以
「A 臂复现 012 的全部成交」这条控制同时验证了整套驱动。

第 2 步（账户级，`step2`）：两个完整账户并排跑同一条机会流，验证现金、容量、
再入场与回撤阶梯的路径影响。**只有第 1 步不是 NO_IMPROVEMENT 才允许跑**（登记里
写明；§4.4「机会级结果不得相加冒充账户超额收益」）。

入场股数怎么来的（第 1 步）：登记要求两臂股数**完全相同**（固定单位风险的机会级结果）。
这里不重算仓位，而是构造只含这一笔的孤立账户，并让 `risk_sized_shares_micro` **恰好**
取到账本里的实际股数（`_isolated_account`），随后断言股数相等 —— 对不上就抛，
绝不静默换一个规模去比较。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

import pandas as pd

from scripts.medium_term.entry_risk import fee_micro, risk_sized_shares_micro
from scripts.portfolio_shadow.candidate_adapter import IncrementalCandidateGenerator, audit_stamps
from scripts.portfolio_shadow.cli import _manifest_to_public, manifest_from_dict
from scripts.portfolio_shadow.paper_engine import NO_NEXT_SESSION, new_account_state, step
from scripts.portfolio_shadow.replay import replay
from scripts.portfolio_shadow.schema import Manifest, Opportunity, to_micro
from scripts.portfolio_shadow.store import ShadowStore
from scripts.strategy_diagnostics import manifest as study_manifest
from scripts.strategy_diagnostics.experiments import shadow_actions, step_account_session
from scripts.strategy_diagnostics.exit_attribution import summarize, trades_from_events
from scripts.strategy_diagnostics.inputs import load
from scripts.strategy_diagnostics.statistics import annual_returns, capacity_summary, concentration
from scripts.strategy_research.exit_policy import DEFAULT_PROTECTION, ProfitProtection

ROOT = Path(__file__).resolve().parents[2]
REGISTRATION = ROOT / 'docs/preregistrations/EXIT-PROTECT-20260921.json'

#: 机会级门槛（登记 `decision_rules.step1_numeric_gates`）
NON_INFERIORITY_R = -8.0
RISK_IMPROVEMENT_GATE = 0.15
MIN_ACTIVATED_TRADES = 20
COST_STRESS_FEE_BP = 20   # 2× 成本情景（基准 10bp）

#: 「只跟踪、永不激活」的同一个策略实例 —— **测量装置，不是候选策略**。
#:
#: A 臂的浮盈回吐需要 H（持仓期最高已完成收盘价）。H 必须按拆股/除息调整，而那份调整
#: 逻辑只有引擎里有（`paper_engine` 阶段 1）—— 在 runner 里再写一份就是第二条定义。
#: 取激活门槛高到不可能达到，引擎便只记录 H、从不抬高保护线，**出场与关闭保护逐字段相同**
#: （`control_tracking_matches_unprotected` 断言，对不上即 ENGINEERING_BLOCKED）。
TRACKING_ONLY = ProfitProtection(activation_num=10 ** 9)

#: 比较两臂「结局是否相同」时只看这些字段。跟踪字段（H、激活标记）**不在其中**：
#: A 臂用跟踪实例跑，它们本来就不该相同。
OUTCOME_FIELDS = ('status', 'exit_reason', 'exit_session', 'exit_price_micro',
                  'holding_sessions', 'net_pnl_micro', 'net_r')


def registration() -> dict:
    if not REGISTRATION.exists():
        raise ValueError(f'PREREGISTRATION_MISSING:{REGISTRATION}')
    return json.loads(REGISTRATION.read_text(encoding='utf-8'))


def registration_digest() -> str:
    if not REGISTRATION.exists():
        raise ValueError(f'PREREGISTRATION_MISSING:{REGISTRATION}')
    return hashlib.sha256(REGISTRATION.read_bytes()).hexdigest()


def load_inputs(study_dir: Path):
    """只读 study 的**冻结输入**（不碰账本）。

    账户级的臂运行只需要输入 —— 而池子扩样的 study（`SD-POOL32-*`）**只冻结输入**、
    没有 `variants/baseline/ledger.sqlite3`。原先 `run_arm` 走 `load_study` 会在那里
    直接 `unable to open database file`（实测）。
    """
    study_dir = Path(study_dir)
    data = json.loads((study_dir / 'study_manifest.json').read_text(encoding='utf-8'))
    prices, market, calendar, quality, actions = load(data, study_dir)
    return data, prices, market, calendar, quality, actions


def load_study(study_dir: Path):
    """读冻结 study 的输入与账本成交。用既有加载器，不自己从文件名推证券 id。"""
    study_dir = Path(study_dir)
    data, prices, market, calendar, quality, actions = load_inputs(study_dir)
    entries = entries_from_ledger(study_dir)
    recorded = recorded_exits(study_dir)
    merged = []
    for e in entries.to_dict('records'):
        key = (e['security_id'], e['entry_session'])
        if key not in recorded:
            raise ValueError(f'RECORDED_EXIT_MISSING:{key[0]}:{key[1]}')
        merged.append({**e, **recorded[key]})
    return data, prices, market, calendar, quality, actions, merged


def recorded_exits(study_dir: Path) -> dict:
    """study 自己那份逐笔退出账（`exit_diagnostics.json`），按 (证券, 入场日) 建索引。

    控制项要拿 A 臂与**账本里实际发生的出场**逐字段比，所以读它，不重算。
    """
    rows = json.loads((Path(study_dir) / 'exit_diagnostics.json').read_text(encoding='utf-8'))
    out = {}
    for r in rows:
        key = (str(r['security_id']), str(r['entry_session']))
        if key in out:
            raise ValueError(f'RECORDED_EXIT_DUPLICATE:{key[0]}:{key[1]}')
        out[key] = {
            'exit_reason_recorded': r['exit_reason'],
            'exit_session_recorded': r['exit_session'],
            'exit_price_recorded': (None if r.get('exit_price') is None
                                    else int(round(float(r['exit_price']) * 1e6))),
            'net_pnl_recorded': r.get('net_pnl_micro'),
        }
    return out


def entries_from_ledger(study_dir: Path) -> pd.DataFrame:
    """study **实际成交**的买入（含引擎当时的价与止损），从它自己的账本读。"""
    ledger = Path(study_dir) / 'variants/baseline/ledger.sqlite3'
    con = sqlite3.connect(ledger)
    rows = []
    for (body,) in con.execute("SELECT body FROM decision_events WHERE event_type='shadow:step'"):
        payload = json.loads(body)['payload']
        if payload.get('type') == 'fill' and payload.get('side') == 'BUY':
            rows.append({'security_id': str(payload['security_id']),
                         'entry_session': str(payload['session']),
                         'shares': int(payload['shares']),
                         'entry_price_micro': int(payload['price_micro']),
                         'stop_micro': int(payload['stop_micro']),
                         'entry_fee_micro': int(payload['fee_micro'])})
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ValueError('NO_ENTRIES_IN_LEDGER')
    return frame.sort_values(['entry_session', 'security_id']).reset_index(drop=True)


def _isolated_account(entry: dict, *, horizon: int, fee_bp: int):
    """只含这一笔的孤立账户，且 `risk_sized_shares_micro` **恰好**取到实际股数。

    定仓是 `min(风险预算/distance, 市值上限/价格, 现金约束)`。取
    `risk_bp = 10000`、`max_weight_bp = 1000000`、`cash = N·P·(1+费率)+1`、
    `initial_cash = N·distance + r`（r < distance）即令三项分别 ≥ N、≥ N、= N。
    不是"差不多能对上"，是构造性的相等 —— 调用方还会断言，对不上就抛。
    """
    n = int(entry['shares'])
    price, stop = int(entry['entry_price_micro']), int(entry['stop_micro'])
    distance = price - stop
    if distance <= 0:
        raise ValueError(f'INVALID_STOP:{entry["security_id"]}:{entry["entry_session"]}')
    nav = n * distance
    cash = n * price * (10000 + fee_bp) // 10000 + 1
    manifest = Manifest(
        experiment_id='EXIT-PROTECT-OPP', status='DRAFT', parent_strategy_id='B3',
        parent_version='1', parent_code_hash='abc', universe_id='isolated',
        universe_hash='isolated', account_scopes=('SHADOW:opp:R',), initial_cash=nav,
        risk_policy={'single_position_risk_bp': 10000, 'max_weight_bp': 1000000,
                     'max_positions': 1},
        execution_policy={'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': horizon},
        llm_policy={'overlay': 'fixed_pass'}, calendar_version='v1',
        evaluation_protocol={'main_metric': 'x', 'enrollment_window': 'x',
                             'review_date': '2026-12-31', 'cost_allocation': 'x'}
    ).freeze('2026-01-02')
    state = new_account_state('SHADOW:opp:R', nav)
    state.cash_available = cash
    # 基准改成真正投入的初始现金：net = 期末权益 − initial_equity 才是这一笔的净损益。
    # 不覆盖的话会把「为凑出实际股数而注入的现金」算成收益（实测差 1e10 微美元量级）。
    state.initial_equity = cash
    state.high_water = cash
    return manifest, state


def _intent(entry: dict, policy_id: str) -> Opportunity:
    return Opportunity(
        experiment_id='EXIT-PROTECT-OPP', security_id=entry['security_id'],
        source_candidate_id=f'{entry["security_id"]}@{entry["entry_session"]}',
        parent_version='1', signal_session=entry['entry_session'],
        observed_at=entry['entry_session'], planned_execution_session=entry['entry_session'],
        rank=1, entry_rule='b3',
        stop_reference={'initial_stop_micro': int(entry['stop_micro'])},
        exit_policy_id=policy_id, input_hash='h')


def walk(entry: dict, prices: pd.DataFrame, acts_by_date: dict, calendar: pd.Series, *,
         protection: ProfitProtection | None, horizon: int = 60, fee_bp: int = 10,
         trend_intents: dict | None = None, time_exit: bool = True) -> dict:
    """沿着真实价格路径推进这一笔，直到它退出（或数据到头）。

    返回该臂的结局。**保护关闭时（`protection=None`）必须复现账本里的成交** —— 这是登记
    要求的控制项，由 `step1` 断言。
    """
    sid = str(entry['security_id'])
    stock = prices[prices.security_id.eq(sid)].sort_values('session')
    sessions = [str(s.date()) for s in stock.session]
    bars_by_session = {str(r.session.date()): {'open': to_micro(r.raw_open),
                                               'high': to_micro(r.raw_high),
                                               'low': to_micro(r.raw_low),
                                               'close': to_micro(r.raw_close)}
                       for r in stock.itertuples(index=False)}
    atr_by_session = {str(r.session.date()): (None if pd.isna(r.asof_atr)
                                              else to_micro(r.asof_atr))
                      for r in stock.itertuples(index=False)}
    cal = [str(s.date()) for s in calendar]
    index_of = {s: i for i, s in enumerate(cal)}
    start = entry['entry_session']
    if start not in bars_by_session:
        raise ValueError(f'ENTRY_SESSION_PRICE_MISSING:{sid}:{start}')
    manifest, state = _isolated_account(entry, horizon=horizon, fee_bp=fee_bp)
    policy_id = 'H60' if protection is None else f'H60+{protection.version}'
    events: list = []
    exit_fill = None
    for session in cal[index_of[start]:]:
        i = index_of[session]
        next_session = cal[i + 1] if i + 1 < len(cal) else None
        # 缺行情也要走这一步：持仓的「第几个 session」在真实账户里照常推进
        # （引擎阶段 5 无条件加一），跳过这一步会让 H60 的到期日晚于账本。
        bar = {sid: bars_by_session[session]} if session in bars_by_session else {}
        atr = {sid: atr_by_session[session]} if session in bars_by_session else {}
        intents = [_intent(entry, policy_id)] if session == start else []
        r = step(state, session=session, bars=bar,
                 corporate_actions=acts_by_date.get(session, ()), intents=intents,
                 manifest=manifest, fee_bp=fee_bp, protection=protection,
                 atr=atr, next_session=(next_session or NO_NEXT_SESSION),
                 trend_exits=(trend_intents or {}).get(session),
                 time_exit=time_exit)
        state = r.state
        events.extend(r.events)
        if session == start:
            # 股数从**成交事件**读，不读持仓：入场当天就可能被打掉（LITE 2016-08-10 正是
            # 当天 STOP），那时持仓已经没了，读持仓会把正常成交误判成定仓失败。
            bought = [e for e in r.events if e['type'] == 'fill' and e['side'] == 'BUY']
            got = bought[0]['shares'] if bought else None
            if got != int(entry['shares']):
                raise ValueError(
                    f'ISOLATED_SIZE_MISMATCH:{sid}:{start}:'
                    f'want={entry["shares"]}:got={got}')
        if sid not in state.positions:
            exit_fill = [e for e in reversed(events)
                         if e['type'] == 'fill' and e['side'] == 'SELL'][0]
            break
    highest = max([e['high_close_micro'] for e in events if e['type'] == 'protection_state'],
                  default=None)
    if exit_fill is None:
        # 窗末仍未平仓：**按市价估值**、标 RIGHT_CENSORED（预登记 §holding_period_and_censoring）。
        # 不给估值的话，取消到期退出的那一臂会凭空少一截收益 —— 只统计已平仓交易是不公平的。
        risk = int(entry['shares']) * (int(entry['entry_price_micro']) - int(entry['stop_micro']))
        cash_ = (state.cash_available + state.cash_reserved + state.unsettled_cash
                 + sum(state.dividend_receivable.values()))
        held = state.positions.get(sid)
        last_close = bars_by_session.get(sessions[-1], {}).get('close') if held else None
        mv = held.shares * last_close if (held and last_close) else 0
        net = None if (held and last_close is None) else int(cash_ + mv - state.initial_equity)
        return {'status': 'RIGHT_CENSORED', 'exit_session': None, 'exit_reason': None,
                'exit_price_micro': None, 'holding_sessions': (held.holding_sessions
                                                               if held else None),
                'net_pnl_micro': net,
                'net_r': (net / risk) if (net is not None and risk > 0) else None,
                'giveback_micro': (None if (highest is None or last_close is None or not held)
                                   else max(0, held.shares * (highest - last_close))),
                'highest_close_micro': highest, 'activated': False,
                'shares_held_micro': (held.shares if held else 0)}
    cash = (state.cash_available + state.cash_reserved + state.unsettled_cash
            + sum(state.dividend_receivable.values()))
    net = cash - state.initial_equity
    risk = int(entry['shares']) * (int(entry['entry_price_micro']) - int(entry['stop_micro']))
    protections = [e for e in events if e['type'] == 'protection_state']
    return {
        'status': 'CLOSED',
        'exit_session': exit_fill['session'],
        'exit_reason': exit_fill['reason'],
        'exit_price_micro': exit_fill['price_micro'],
        'holding_sessions': sum(1 for e in events if e['type'] == 'hold'),
        'net_pnl_micro': net,
        'net_r': (net / risk) if risk > 0 else None,
        # 浮盈回吐 = 持仓期最高已完成收盘价到实际成交价的距离（按退出时股数）
        'giveback_micro': (None if highest is None else
                           max(0, exit_fill['shares'] * (highest - exit_fill['price_micro']))),
        'highest_close_micro': highest,
        'activated': any(e['protection_activated'] for e in protections),
        'raised_count': sum(1 for e in protections if e['stop_micro'] is not None),
    }


def step1(study_dir: Path, *, horizon: int = 60) -> dict:
    """机会级两臂对照。A = 关闭保护（= 现状）；B = 启用登记的保护线。"""
    study_dir = Path(study_dir)
    reg = registration()
    if int(reg['policy_under_test']['parameters']['atr_period']) != DEFAULT_PROTECTION.atr_period:
        raise ValueError('REGISTRATION_CODE_MISMATCH:atr_period')
    if baseline_trades is None:
        data, prices, _market, calendar, _quality, actions, entries = load_study(study_dir)
    else:
        # 池子扩样重跑：只读 study 的**冻结输入**，基线成交来自 `baseline_trades`
        data, prices, _market, calendar, _quality, actions = load_inputs(study_dir)
        entries = _entries_from_baseline_trades(baseline_trades)
    acts_by_date: dict = defaultdict(list)
    converted, _dropped = shadow_actions(
        actions, universe=set(prices.security_id),
        session_range=(str(calendar.min().date()), str(calendar.max().date())))
    for a in converted:
        acts_by_date[a['ex_date']].append(a)
    fee_bp = int(data['cost_policy']['fee_bp'])

    rows = []
    for entry in entries:
        arms = {}
        for label, policy, bp in (('A', TRACKING_ONLY, fee_bp),
                                  ('A_off', None, fee_bp),
                                  ('B', DEFAULT_PROTECTION, fee_bp),
                                  ('A2', None, COST_STRESS_FEE_BP),
                                  ('B2', DEFAULT_PROTECTION, COST_STRESS_FEE_BP)):
            arms[label] = walk(entry, prices, acts_by_date, calendar, protection=policy,
                               horizon=horizon, fee_bp=bp)
        rows.append({**entry, **arms,
                     'delta_r': (None if arms['A']['net_r'] is None or arms['B']['net_r'] is None
                                 else arms['B']['net_r'] - arms['A']['net_r']),
                     'delta_usd': (None if arms['A']['net_pnl_micro'] is None
                                   or arms['B']['net_pnl_micro'] is None
                                   else arms['B']['net_pnl_micro'] - arms['A']['net_pnl_micro']),
                     'delta_r_2x': (None if arms['A2']['net_r'] is None
                                    or arms['B2']['net_r'] is None
                                    else arms['B2']['net_r'] - arms['A2']['net_r']),
                     'tracking_matches_unprotected': all(
                         arms['A'][k] == arms['A_off'][k] for k in OUTCOME_FIELDS)})
    summary = summarise(rows)
    return {'registration_sha256': registration_digest(), 'horizon': horizon,
            'fee_bp': fee_bp, 'cost_stress_fee_bp': COST_STRESS_FEE_BP,
            'study_id': data['study_id'], 'n_trades': len(rows),
            'trades': rows, 'summary': summary,
            'verdict': step1_verdict(summary)}


def _reproduction_failures(rows: list[dict], arm_key: str = 'A_off') -> list[dict]:
    """控制项：A 臂必须复现账本里的每一次出场（原因 + 日期 + 价格 + 净损益）。

    这是**整套驱动**的验证：价格路径、公司行动、费用、引擎调用方式。对不上就是
    ENGINEERING_BLOCKED（登记的停止条件），不出收益结论。

    右删失的那几笔只要求「两边的删失集合一致」：账本里的未平仓损益是浮动估值，
    而这里比的是实际成交，不可同日而语。
    """
    bad = []
    for r in rows:
        a, recorded_reason = r[arm_key], r['exit_reason_recorded']
        if recorded_reason is None:
            if a['status'] != 'RIGHT_CENSORED':
                bad.append({'security_id': r['security_id'], 'entry_session': r['entry_session'],
                            'recorded': 'RIGHT_CENSORED', 'replayed': a['status']})
            continue
        # 成交价只在**两边都有**时比：池子路径的基线成交来自 `trades_from_events`，
        # 它把价格记成 `exit_price`（美元）而非 `exit_price_micro`；缺这一列**不削弱控制**
        # —— 净损益是价格与股数的函数，`net_pnl_micro` 一致即蕴含成交价与费用一致。
        # 缺的那部分计数由调用方披露（不静默）。
        price_mismatch = (r['exit_price_recorded'] is not None
                          and a['exit_price_micro'] != r['exit_price_recorded'])
        if (a['status'] != 'CLOSED' or a['exit_reason'] != recorded_reason
                or a['exit_session'] != r['exit_session_recorded']
                or price_mismatch
                or a['net_pnl_micro'] != r['net_pnl_recorded']):
            bad.append({'security_id': r['security_id'], 'entry_session': r['entry_session'],
                        'recorded': [recorded_reason, r['exit_session_recorded'],
                                     r['exit_price_recorded'], r['net_pnl_recorded']],
                        'replayed': [a['exit_reason'], a['exit_session'], a['exit_price_micro'],
                                     a['net_pnl_micro']]})
    return bad


def _es(values: list[float], tail: float = 0.05) -> float | None:
    """下尾 Expected Shortfall（转为**正损失量**：越大越差）。"""
    if not values:
        return None
    ordered = sorted(values)
    k = max(1, int(round(len(ordered) * tail)))
    return -sum(ordered[:k]) / k


def summarise(rows: list[dict]) -> dict:
    closed = [r for r in rows if r['A']['status'] == 'CLOSED' and r['B']['status'] == 'CLOSED']
    deltas = [r['delta_r'] for r in closed if r['delta_r'] is not None]
    deltas_2x = [r['delta_r_2x'] for r in closed if r['delta_r_2x'] is not None]
    usd = [r['delta_usd'] for r in closed if r['delta_usd'] is not None]
    give_a = sum(r['A']['giveback_micro'] or 0 for r in closed)
    give_b = sum(r['B']['giveback_micro'] or 0 for r in closed)
    activated = [r for r in closed if r['B']['activated']]
    untouched = [r for r in closed if not r['B']['activated']]
    es_a = _es([r['A']['net_r'] for r in closed if r['A']['net_r'] is not None])
    es_b = _es([r['B']['net_r'] for r in closed if r['B']['net_r'] is not None])
    by_sec = defaultdict(float)
    for r in closed:
        if r['delta_r'] is not None:
            by_sec[r['security_id']] += r['delta_r']
    ordered = sorted(by_sec.items(), key=lambda kv: kv[1])
    give_delta = give_a - give_b
    give_by_sec = defaultdict(float)
    for r in closed:
        give_by_sec[r['security_id']] += ((r['A']['giveback_micro'] or 0)
                                          - (r['B']['giveback_micro'] or 0))
    give_ordered = sorted(give_by_sec.items(), key=lambda kv: kv[1], reverse=True)
    gain_by_sec = defaultdict(float)
    for r in closed:
        if r['delta_usd'] is not None:
            gain_by_sec[r['security_id']] += r['delta_usd']
    biggest_loss = min(gain_by_sec.items(), key=lambda kv: kv[1]) if gain_by_sec else (None, 0)
    total_gain = sum(gain_by_sec.values())
    # 机制说明用：原策略的大赢家（>3R）被保护线截断到什么程度。这一条最能解释结果，
    # 不是"又一个统计量"—— 它指的是**同一批笔**在两臂下的差别。
    big = [r for r in closed if (r['A']['net_r'] or 0) > 3]
    return {
        'n_trades': len(rows), 'n_closed': len(closed),
        'n_right_censored': len(rows) - len(closed),
        'n_activated': len(activated), 'n_untouched': len(untouched),
        'sum_net_r_A': sum(r['A']['net_r'] for r in closed if r['A']['net_r'] is not None),
        'sum_net_r_B': sum(r['B']['net_r'] for r in closed if r['B']['net_r'] is not None),
        'sum_usd_A': sum(r['A']['net_pnl_micro'] for r in closed
                         if r['A']['net_pnl_micro'] is not None),
        'sum_usd_B': sum(r['B']['net_pnl_micro'] for r in closed
                         if r['B']['net_pnl_micro'] is not None),
        'n_profitable_a': sum(1 for r in closed if (r['A']['net_r'] or 0) > 0),
        'n_profitable_b': sum(1 for r in closed if (r['B']['net_r'] or 0) > 0),
        'mean_holding_a': (sum(r['A']['holding_sessions'] or 0 for r in closed) / len(closed)
                           if closed else None),
        'mean_holding_b': (sum(r['B']['holding_sessions'] or 0 for r in closed) / len(closed)
                           if closed else None),
        'big_winners': {
            'threshold_r': 3.0, 'n': len(big),
            'sum_r_a': sum(r['A']['net_r'] for r in big),
            'sum_r_b': sum(r['B']['net_r'] for r in big),
        },
        'delta_r_sum': sum(deltas), 'delta_r_mean': (sum(deltas) / len(deltas)) if deltas else None,
        'delta_usd_sum': sum(usd),
        'delta_r_sum_2x': sum(deltas_2x),
        'giveback_a_micro': give_a, 'giveback_b_micro': give_b,
        'giveback_reduction_micro': give_delta,
        'giveback_reduction_pct': (give_delta / give_a) if give_a else None,
        'tail_es_a': es_a, 'tail_es_b': es_b,
        'tail_es_improvement': ((es_a - es_b) / es_a) if es_a else None,
        'worst_a': min((r['A']['net_r'] for r in closed if r['A']['net_r'] is not None),
                       default=None),
        'worst_b': min((r['B']['net_r'] for r in closed if r['B']['net_r'] is not None),
                       default=None),
        'exit_reasons_a': dict(Counter(r['A']['exit_reason'] for r in closed)),
        'exit_reasons_b': dict(Counter(r['B']['exit_reason'] for r in closed)),
        'by_security_delta_r': ordered,
        'by_security_giveback_reduction': give_ordered,
        'concentration': {
            'top1_security': give_ordered[0][0] if give_ordered else None,
            'top1_share_of_giveback_reduction': (
                (give_ordered[0][1] / give_delta) if give_ordered and give_delta else None),
            'leave_one_out_delta_r_sum': (sum(deltas) - ordered[0][1]) if ordered else None,
            'largest_contributor_of_usd_delta': biggest_loss[0],
            'largest_contributor_share_of_loss': (
                (biggest_loss[1] / total_gain) if total_gain else None),
        },
        'control_untouched_arms_identical': all(
            all(r['A'][k] == r['B'][k] for k in OUTCOME_FIELDS) for r in untouched),
        'control_tracking_matches_unprotected': all(r['tracking_matches_unprotected'] for r in rows),
        'control_reproduction_failures': _reproduction_failures(rows),
    }


def step1_verdict(summary: dict) -> dict:
    """按**登记文件**里的门槛判第 1 步。阈值取自登记，不在这里放宽。"""
    s = summary
    checks = {
        'reproduces_the_baseline': not s['control_reproduction_failures'],
        'tracking_matches_unprotected': bool(s['control_tracking_matches_unprotected']),
        'untouched_arms_identical': bool(s['control_untouched_arms_identical']),
        'enough_activated': s['n_activated'] >= MIN_ACTIVATED_TRADES,
        'delta_non_inferior': (s['delta_r_sum'] or 0) >= NON_INFERIORITY_R,
        'delta_positive': (s['delta_r_sum'] or 0) >= 0,
        'risk_improved': (s['tail_es_improvement'] or 0) >= RISK_IMPROVEMENT_GATE,
        'held_under_2x_cost': (s['delta_r_sum_2x'] or 0) >= NON_INFERIORITY_R,
        'not_concentrated': ((s['concentration']['top1_share_of_giveback_reduction'] or 1.0) <= 0.5
                             and (s['concentration']['leave_one_out_delta_r_sum'] or 0) > 0),
    }
    blocked = ('reproduces_the_baseline', 'tracking_matches_unprotected',
               'untouched_arms_identical')
    if not all(checks[k] for k in blocked):
        token = 'ENGINEERING_BLOCKED'
    elif not checks['enough_activated']:
        token = 'INSUFFICIENT_SAMPLE'
    elif not checks['risk_improved']:
        token = 'NO_IMPROVEMENT'
    elif not checks['not_concentrated']:
        token = 'CONCENTRATED'
    elif not (checks['delta_non_inferior'] and checks['held_under_2x_cost']):
        token = 'RISK_REJECTED'
    elif checks['delta_positive']:
        token = 'EVIDENCE_SUPPORTED'
    else:
        token = 'RISK_TRADEOFF'
    return {'token': token, 'checks': checks,
            'thresholds': {'non_inferiority_r': NON_INFERIORITY_R,
                           'risk_improvement_gate': RISK_IMPROVEMENT_GATE,
                           'min_activated_trades': MIN_ACTIVATED_TRADES}}


TREND_REGISTRATION = ROOT / 'docs/preregistrations/EXIT-TREND-MA2060-20260922.json'


def _entries_from_baseline_trades(path: Path) -> list[dict]:
    """把一次基线臂运行的逐笔成交变成 `entries`（池子扩样重跑用）。

    **为什么不用 `load_study`**：那条路要 study 自己的账本与 `exit_diagnostics.json`，
    而 32 只池子的 study 只是冻结了**输入**（诊断要的基线成交由我自己跑一次得到）。
    复现控制仍然成立：A 臂（孤立走查）必须复现这次基线运行的每一笔。
    """
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    # 进场信息只在**成交事件**里（`trades_from_events` 的输出没有 entry_price/stop/shares），
    # 出场信息在 trades 里 ⇒ 两边按 (证券, 入场日) 合并。缺一边即报错，不静默配错。
    exits = {(str(t['security_id']), str(t['entry_session'])): t for t in payload['trades']}
    out = []
    for b in payload['buys']:
        key = (str(b['security_id']), str(b['entry_session']))
        t = exits.get(key)
        if t is None:
            raise ValueError(f'BASELINE_EXIT_MISSING:{key[0]}:{key[1]}')
        out.append({'security_id': key[0], 'entry_session': key[1],
                    'shares': int(b['shares']),
                    'entry_price_micro': int(b['entry_price_micro']),
                    'stop_micro': int(b['stop_micro']),
                    'entry_fee_micro': int(b.get('entry_fee_micro') or 0),
                    'exit_reason_recorded': t.get('exit_reason'),
                    'exit_session_recorded': t.get('exit_session'),
                    'exit_price_recorded': t.get('exit_price_micro'),
                    'net_pnl_recorded': t.get('net_pnl_micro')})
    if not out:
        raise ValueError('BASELINE_TRADES_EMPTY')
    return out


def step1_trend(study_dir: Path, *, horizon: int = 60,
                baseline_trades: Path | None = None) -> dict:
    """趋势退出 vs H60 的同机会对照（预登记 `EXIT-TREND-MA2060-20260922`）。

    A = H60 + 硬止损；B = 趋势退出 + 同一硬止损（**无时间退出**）。
    窗末未平仓在 B 臂按市价估值并标右删失（登记 §holding_period_and_censoring）。
    """
    from scripts.strategy_research.trend_exit import exit_signals, trend_intents_by_session
    study_dir = Path(study_dir)
    if not TREND_REGISTRATION.exists():
        raise ValueError(f'PREREGISTRATION_MISSING:{TREND_REGISTRATION}')
    if baseline_trades is None:
        data, prices, _market, calendar, _quality, actions, entries = load_study(study_dir)
    else:
        # 池子扩样重跑：只读 study 的**冻结输入**，基线成交来自 `baseline_trades`
        data, prices, _market, calendar, _quality, actions = load_inputs(study_dir)
        entries = _entries_from_baseline_trades(baseline_trades)
    acts_by_date: dict = defaultdict(list)
    converted, _dropped = shadow_actions(
        actions, universe=set(prices.security_id),
        session_range=(str(calendar.min().date()), str(calendar.max().date())))
    for a in converted:
        acts_by_date[a['ex_date']].append(a)
    fee_bp = int(data['cost_policy']['fee_bp'])
    intents = trend_intents_by_session(prices, actions, calendar)
    # 每只证券的逐日信号，供「A 臂到期时趋势是否仍完整」这一项用（§6.2 要求的指标）
    signals = {str(sid): exit_signals(g, actions, calendar.max()).set_index('session')
               for sid, g in prices.groupby('security_id')}

    rows = []
    for entry in entries:
        a = walk(entry, prices, acts_by_date, calendar, protection=None, horizon=horizon,
                 fee_bp=fee_bp)
        b = walk(entry, prices, acts_by_date, calendar, protection=None, horizon=horizon,
                 fee_bp=fee_bp, trend_intents=intents, time_exit=False)
        a2 = walk(entry, prices, acts_by_date, calendar, protection=None, horizon=horizon,
                  fee_bp=COST_STRESS_FEE_BP)
        b2 = walk(entry, prices, acts_by_date, calendar, protection=None, horizon=horizon,
                  fee_bp=COST_STRESS_FEE_BP, trend_intents=intents, time_exit=False)
        # A 臂到期退出时，趋势是否仍然完整（描述性，§6.2）
        intact = None
        if a['exit_reason'] == 'TIME_EXIT' and a['exit_session']:
            sig = signals[str(entry['security_id'])]
            ts = pd.Timestamp(a['exit_session'])
            if ts in sig.index:
                row = sig.loc[ts]
                intact = bool(row['close'] > row['ma20'] or row['close'] >= row['ma60'])
        rows.append({**entry, 'A': a, 'B': b, 'A2': a2, 'B2': b2, 'trend_intact_at_a_exit': intact})
    return {'registration_sha256': hashlib.sha256(TREND_REGISTRATION.read_bytes()).hexdigest(),
            'study_id': data['study_id'], 'n_trades': len(rows), 'fee_bp': fee_bp,
            'exit_price_compared': sum(1 for r in rows
                                       if r['exit_price_recorded'] is not None),
            'trades': rows, 'summary': summarise_trend(rows),
            'verdict': trend_verdict(summarise_trend(rows))}


def summarise_trend(rows: list[dict]) -> dict:
    both = [r for r in rows if r['A']['status'] == 'CLOSED']
    deltas = [r['B']['net_r'] - r['A']['net_r'] for r in both
              if r['A']['net_r'] is not None and r['B']['net_r'] is not None]
    deltas_2x = [r['B2']['net_r'] - r['A2']['net_r'] for r in both
                 if r['A2']['net_r'] is not None and r['B2']['net_r'] is not None]
    censored = [r for r in rows if r['B']['status'] == 'RIGHT_CENSORED']
    by_sec = defaultdict(float)
    for r in both:
        if r['A']['net_r'] is not None and r['B']['net_r'] is not None:
            by_sec[r['security_id']] += r['B']['net_r'] - r['A']['net_r']
    ordered = sorted(by_sec.items(), key=lambda kv: kv[1], reverse=True)
    total = sum(by_sec.values())
    a_r = [r['A']['net_r'] for r in both if r['A']['net_r'] is not None]
    b_r = [r['B']['net_r'] for r in both if r['B']['net_r'] is not None]
    hold_a = [r['A']['holding_sessions'] for r in both if r['A']['holding_sessions']]
    hold_b = [r['B']['holding_sessions'] for r in both if r['B']['holding_sessions']]
    intact = [r['trend_intact_at_a_exit'] for r in rows if r['trend_intact_at_a_exit'] is not None]
    return {
        'n_trades': len(rows), 'n_comparable': len(both),
        'n_right_censored_b': len(censored),
        'sum_r_a': sum(a_r), 'sum_r_b': sum(b_r), 'delta_r_sum': sum(deltas),
        'delta_r_sum_2x': sum(deltas_2x),
        'worst_a': min(a_r) if a_r else None, 'worst_b': min(b_r) if b_r else None,
        'tail_es_a': _es(a_r), 'tail_es_b': _es(b_r),
        'mean_holding_a': (sum(hold_a) / len(hold_a)) if hold_a else None,
        'mean_holding_b': (sum(hold_b) / len(hold_b)) if hold_b else None,
        'max_holding_a': max(hold_a) if hold_a else None,
        'max_holding_b': max(hold_b) if hold_b else None,
        'exit_reasons_a': dict(Counter(r['A']['exit_reason'] for r in both)),
        'exit_reasons_b': dict(Counter(r['B']['exit_reason'] for r in both)),
        'trend_intact_at_a_exit': (sum(1 for x in intact if x) / len(intact)) if intact else None,
        'by_security_delta_r': ordered,
        'top1_share': (ordered[0][1] / total) if ordered and total else None,
        'leave_one_out': (total - ordered[0][1]) if ordered else None,
        'control_reproduction_failures': _reproduction_failures(rows, 'A'),
        'control_b_has_no_time_exit': not any(r['B']['exit_reason'] == 'TIME_EXIT' for r in both),
        **_stop_controls(both),
    }


def _stop_controls(rows: list[dict]) -> dict:
    """硬止损机制不受本改动影响 —— 但**不是**「两臂的止损集合相同」。

    第一版我写成「止损退出的集合必须逐笔相同」，跑出来 56 笔不符。查下去是**我的控制写错了**：
    趋势退出在**开盘**消费（阶段 2.2 早于日内止损阶段 4），所以它会**提前**替掉当天本会发生的
    日内止损 —— 这与"止损被改动"是两件完全不同的事。实测 56 笔里，B 的离场日**无一例外早于**
    A；两臂都因止损离场的 30 笔则逐字段完全相同。

    真正的不变量是这两条（都能被实现缺陷打破）：
      ① 两臂都因止损离场时，**日期与价格逐字段相同**（止损机制未被动过）；
      ② A 因止损离场时，B 的离场日**不得晚于**它（趋势退出可以抢先，不能推迟止损）。
    另加一条反向控制：A 止损而 B 从未离场（B 忽略了止损）⇒ 直接判失败。
    """
    same, details, delayed = True, [], []
    both = a_stop_n = 0
    for r in rows:
        a, b = r['A'], r['B']
        a_stop = a['exit_reason'] in ('STOP', 'GAP_STOP')
        b_stop = b['exit_reason'] in ('STOP', 'GAP_STOP')
        if a_stop and b_stop:
            both += 1
            if (a['exit_session'] != b['exit_session']
                    or a['exit_price_micro'] != b['exit_price_micro']):
                same = False
                details.append([r['security_id'], r['entry_session'],
                                a['exit_session'], b['exit_session']])
        elif a_stop:
            a_stop_n += 1
            if b['exit_session'] is None or b['exit_session'] > a['exit_session']:
                delayed.append([r['security_id'], r['entry_session'],
                                a['exit_session'], b['exit_session']])
    return {'both_stop_identical': same, 'both_stop_count': both,
        'both_stop_mismatches': details,
        'b_never_delays_a_stop': not delayed, 'b_delayed_a_stop': delayed,
        'a_stop_b_preempted_count': a_stop_n}


def trend_verdict(s: dict) -> dict:
    checks = {
        'reproduces_the_baseline': not s['control_reproduction_failures'],
        'b_has_no_time_exit': bool(s['control_b_has_no_time_exit']),
        'both_stop_identical': bool(s['both_stop_identical']),
        'b_never_delays_a_stop': bool(s['b_never_delays_a_stop']),
        'delta_positive': (s['delta_r_sum'] or 0) > 0,
        'held_under_2x_cost': (s['delta_r_sum_2x'] or 0) > 0,
        'tail_not_worse': ((s['worst_b'] or 0) >= (s['worst_a'] or 0)
                           and (s['tail_es_b'] or 0) <= (s['tail_es_a'] or 0)),
        'not_concentrated': ((s['top1_share'] or 1.0) <= 0.5
                             and (s['leave_one_out'] or 0) > 0),
    }
    blocked = ('reproduces_the_baseline', 'b_has_no_time_exit', 'both_stop_identical',
               'b_never_delays_a_stop')
    if not all(checks[k] for k in blocked):
        token = 'ENGINEERING_BLOCKED'
    elif not checks['delta_positive']:
        token = 'NO_IMPROVEMENT'
    elif not checks['tail_not_worse']:
        token = 'RISK_REJECTED'
    elif not checks['not_concentrated']:
        token = 'CONCENTRATED'
    elif not checks['held_under_2x_cost']:
        token = 'RISK_REJECTED'
    else:
        token = 'EVIDENCE_SUPPORTED'
    return {'token': token, 'checks': checks}


def main(argv=None) -> int:
    """`python -m scripts.strategy_research.runner step1 --study <dir> [--out <dir>]`。

    产物写在 `data/strategy_research/<study_id>/`（规划 §3.2）。默认 study = 当前冻结基线；
    默认输出目录在**主 checkout 的 data 下**（隔离 worktree 里没有 data，且研究产物应当
    与其它研究数据放在一起、不随后续 worktree 删除而消失）。
    """
    import argparse
    import time
    from pathlib import Path

    from scripts.strategy_research import report

    data_root = Path('/Users/wh1817w/quant/quant_us-main/data')
    ap = argparse.ArgumentParser(prog='strategy_research.runner')
    ap.add_argument('step', choices=('step1',))
    ap.add_argument('--study', default=str(data_root / 'strategy_diagnostics' /
                                          'SD-P0P1-20260921-012'))
    ap.add_argument('--out', default=None)
    ap.add_argument('--horizon', type=int, default=60)
    args = ap.parse_args(argv)
    out = Path(args.out) if args.out else (
        data_root / 'strategy_research' / 'SR-EXIT-PROTECT-20260921-001')
    started = time.time()
    result = step1(Path(args.study), horizon=args.horizon)
    result['elapsed_seconds'] = round(time.time() - started, 1)
    written = report.write_artifacts(result, out)
    print(json.dumps({'verdict': result['verdict']['token'], **written,
                      'elapsed_seconds': result['elapsed_seconds']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
