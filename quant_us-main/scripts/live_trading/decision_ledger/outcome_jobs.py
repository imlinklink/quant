"""多期限结果结算（技术设计 §16）：selection / entry / position 的 outcome 投影。

从冻结计划 + 后续价格序列计算标签，写 decision_outcomes_v2，并 append outcome_observed 事件。
所有反事实使用决策时已冻结的计划、下一可成交时刻与统一滑点模型；跳空用第一可成交价格。
"""
import json
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from .event_store import EventStore, insert_event, make_event, stable_id, utc

logger = logging.getLogger(__name__)

HORIZONS = (1, 3, 5, 10, 20)

# 落库指标的量化位数。用来**压掉浮点末位噪声**，不是"相差小于 1e-6 就算同一个值" ——
# 落在档边界两侧的两个数仍然不同，那时冲突保护照旧生效（这正是我们要的：宁可让真正
# 不同的值暴露出来，也不要静默当成一样）。真正的防重算由 `write_outcome` 的跳过规则承担。
QUANTUM_DIGITS = 6
# 单条期限允许的最大修订次数（防止修订键无限增长）
MAX_REVISIONS = 8


def quantize(value):
    """浮点指标量化；非数值原样返回。"""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return round(float(value), QUANTUM_DIGITS)


def independence_group(code: str, strategy: str, primary_event_cluster: Optional[str],
                       trading_week: str) -> str:
    """独立样本分组（§16.4）：同股票/同事件簇/同交易周 聚合为一个样本。"""
    return stable_id('sample_group', code, strategy or '', primary_event_cluster or '', trading_week)


# ---------- 纯计算 ----------

def simulate_trade(entry_price: float, stop: float, target: Optional[float],
                   closes: List[float]) -> Dict[str, Any]:
    """沿收盘价序列模拟一笔交易：先触止损/目标则按对应价退出，否则持有到序列末。

    跳空规则：若某日开盘价（用前收盘近似）穿越止损/目标，按第一可成交价（该日收盘近似）成交。
    返回 exit_price / exit_reason / mfe_pct / mae_pct / return_pct / realized_r。
    """
    if not closes:
        return {'exit_price': entry_price, 'exit_reason': 'no_data',
                'mfe_pct': 0.0, 'mae_pct': 0.0, 'return_pct': 0.0, 'realized_r': 0.0}
    mfe = 0.0
    mae = 0.0
    prev = entry_price
    exit_price = closes[-1]
    exit_reason = 'time_exit'
    for c in closes:
        pct = (c - entry_price) / entry_price
        mfe = max(mfe, pct)
        mae = min(mae, pct)
        if stop is not None and c <= stop:
            exit_price = c
            exit_reason = 'stop'
            break
        if target is not None and c >= target:
            exit_price = c
            exit_reason = 'target'
            break
        prev = c
    return_pct = (exit_price - entry_price) / entry_price
    realized_r = return_pct / abs((entry_price - stop) / entry_price) if stop else 0.0
    return {'exit_price': exit_price, 'exit_reason': exit_reason,
            'mfe_pct': mfe, 'mae_pct': mae, 'return_pct': return_pct,
            'realized_r': realized_r}


def selection_outcomes_for_code(code: str, closes: List[float],
                                benchmark_closes: Optional[List[float]] = None,
                                stop: Optional[float] = None,
                                target: Optional[float] = None,
                                entry_price: Optional[float] = None,
                                data_quality: str = 'good',
                                decision_id: str = '') -> List[Dict[str, Any]]:
    """结算单只股票 1/3/5/10/20 交易日 outcome（§16.1）。"""
    if not closes:
        return []
    base = entry_price if entry_price is not None else closes[0]
    out = []
    # closes 约定为 [决策基准收盘, 后续第1日收盘, ...]。
    for h in HORIZONS:
        if len(closes) <= h:
            continue
        c = closes[h]
        ret = (c - base) / base
        bench = None
        excess = None
        if benchmark_closes and len(benchmark_closes) > h:
            bench = (benchmark_closes[h] - benchmark_closes[0]) / benchmark_closes[0]
            excess = ret - bench
        sim = simulate_trade(base, stop, target, closes[1:h + 1])
        out.append({
            'horizon': f'{h}d', 'label_as_of': utc(),
            'return_pct': ret, 'benchmark_return_pct': bench,
            'excess_return_pct': excess, 'mfe_pct': sim['mfe_pct'],
            'mae_pct': sim['mae_pct'], 'realized_r': sim['realized_r'],
            'data_quality': data_quality,
            'body': {'code': code, 'decision_id': decision_id,
                     'exit_reason': sim['exit_reason'], 'entry_price': base},
        })
    return out


def entry_path_outcome(template: Dict[str, Any], entry_price: float,
                       closes: List[float]) -> Dict[str, Any]:
    """给定入场模板 + 价格序列，计算该路径的净 R / 回撤（§16.2 反事实）。"""
    qty = float(template.get('quantity', 0))
    stop = template.get('initial_stop')
    if qty <= 0:
        return {'path': template.get('kind', 'no_entry'), 'quantity': 0,
                'net_r': 0.0, 'max_drawdown_r': 0.0, 'return_pct': 0.0}
    sim = simulate_trade(entry_price, stop, None, closes)
    return {'path': template.get('kind', 'unknown'), 'quantity': int(qty),
            'net_r': sim['realized_r'], 'max_drawdown_r': sim['mae_pct'],
            'return_pct': sim['return_pct'], 'exit_reason': sim['exit_reason']}


def position_outcome(trade: Dict[str, Any], actual_exit_price: Optional[float],
                     mechanical_exit_price: Optional[float],
                     first_llm_exit_price: Optional[float],
                     closes: List[float]) -> Dict[str, Any]:
    """持仓退出对照（§16.3）：saved_r / premature_exit_cost_r / invalidation_lead_time。"""
    entry = float(trade.get('entry_price', 0))
    stop = (trade.get('protection') or {}).get('active_stop') or trade.get('initial_stop')
    sim = simulate_trade(entry, stop, None, closes)
    result = {
        'trade_id': trade.get('trade_id'),
        'mechanical_exit_price': mechanical_exit_price,
        'actual_exit_price': actual_exit_price,
        'first_llm_exit_price': first_llm_exit_price,
        'exit_mfe_pct': sim['mfe_pct'], 'exit_mae_pct': sim['mae_pct'],
        'saved_r': None, 'premature_exit_cost_r': None, 'invalidation_lead_time': None,
    }
    if entry > 0 and mechanical_exit_price is not None and actual_exit_price is not None:
        result['saved_r'] = (actual_exit_price - mechanical_exit_price) / entry
    if entry > 0 and first_llm_exit_price is not None and mechanical_exit_price is not None:
        # 过早卖出：LLM 建议价 vs 机械退出价之间的错失收益（负=过早卖出损失）
        result['premature_exit_cost_r'] = (mechanical_exit_price - first_llm_exit_price) / entry
    return result


# ---------- 写库 ----------

class OutcomeSettlement:
    """把结算结果写 decision_outcomes_v2 + outcome_observed 事件。"""

    def __init__(self, registry):
        self.events = EventStore(registry)
        self.scope = registry.namespace

    # ---------- 已结算的判定与量化 ----------

    def _find_settled(self, con, decision_id: str, horizon: str, subject_key: str):
        """该期限**是否已结算**；返回 (修订号, 载荷)。未结算返回 (None, None)。

        判据是**事件是否存在**，不是"投影说 good"：`write_outcome` 先在事务 A 更新投影、
        再在事务 B 写事件；两次之间崩掉会留下"投影 good 但事件缺失"的行 —— 那种情况下
        跳过就等于让账本永远缺这条记录。事件才是账本的真相。
        """
        from .event_store import stable_id as _sid
        base = [decision_id, horizon, subject_key]
        for rev in range(MAX_REVISIONS, 0, -1):
            eid = _sid('event', self.scope, 'outcome_observed', [*base, f'rev{rev}'])
            row = con.execute('SELECT body FROM decision_events WHERE event_id=?',
                              (eid,)).fetchone()
            if row:
                return rev, json.loads(row[0])['payload']
        eid = _sid('event', self.scope, 'outcome_observed', base)
        row = con.execute('SELECT body FROM decision_events WHERE event_id=?',
                          (eid,)).fetchone()
        return (0, json.loads(row[0])['payload']) if row else (None, None)

    def settled_outcome(self, decision_id: str, horizon: str, subject_key: str = ''):
        """已结算的载荷（含修订）；未结算返回 None。只读。"""
        with self.events.transaction() as con:
            _, payload = self._find_settled(con, decision_id, horizon, subject_key)
        return payload

    def write_outcome(self, decision_id: str, outcome: Dict[str, Any],
                      subject_key: str = '') -> bool:
        """写投影 + outcome_observed 事件。**已结算的期限直接跳过**，返回 False。

        重跑不得改写已记录的结果：既因为账本是追加式的（改历史等于用今天的数据改写
        当时的结论），也因为重新推导只会引入浮点末位噪声 —— 而事件键固定，
        那种噪声会让 `insert_event` 抛「同一事件ID出现冲突内容」，把**整个日任务钉死**
        （`retryable: false`，永不重试）。2026-09-19 的生产故障正是这个形态：
        一条 1.28e-13 的 `excess_return_pct` 差异让结算链路停了。

        确需修正历史时走 `revise_outcome`（显式、留因、不复用原事件键）。
        """
        horizon = outcome['horizon']
        with self.events.transaction() as con:
            settled_rev, _ = self._find_settled(con, decision_id, horizon, subject_key)
        if settled_rev is not None:
            return False
        cols = ['account_scope', 'decision_id', 'horizon', 'subject_key', 'label_as_of',
                'return_pct', 'benchmark_return_pct', 'excess_return_pct', 'mfe_pct',
                'mae_pct', 'realized_r', 'data_quality', 'body']
        import json as _json
        row = {
            'account_scope': self.scope,
            'decision_id': decision_id,
            'horizon': horizon,
            'subject_key': subject_key,
            'label_as_of': outcome.get('label_as_of', utc()),
            # 量化：压掉浮点末位噪声，让"经济上相同"的重算产出逐字节相同的载荷。
            'return_pct': quantize(outcome.get('return_pct')),
            'benchmark_return_pct': quantize(outcome.get('benchmark_return_pct')),
            'excess_return_pct': quantize(outcome.get('excess_return_pct')),
            'mfe_pct': quantize(outcome.get('mfe_pct')),
            'mae_pct': quantize(outcome.get('mae_pct')),
            'realized_r': quantize(outcome.get('realized_r')),
            'data_quality': outcome.get('data_quality', 'good'),
            'body': _json.dumps(outcome.get('body', {}), ensure_ascii=False, default=str),
        }
        with self.events.transaction() as con:
            con.execute(
                'INSERT OR REPLACE INTO decision_outcomes_v2 (' + ','.join(cols) +
                ') VALUES (' + ','.join('?' for _ in cols) + ')',
                [row[c] for c in cols])
        if outcome.get('data_quality', 'good') == 'pending_future_bars':
            return True  # 占位：只更新投影，不写 outcome_observed（未成熟，下轮还会来）
        self.events.record('outcome_observed', [decision_id, horizon, subject_key],
                           {'decision_id': decision_id, 'horizon': horizon,
                            'subject_key': subject_key,
                            'return_pct': row['return_pct'],
                            'excess_return_pct': row['excess_return_pct']})
        return True

    def revise_outcome(self, decision_id: str, outcome: Dict[str, Any],
                       subject_key: str = '', *, reason: str, operator: str) -> str:
        """**显式修订**一条已结算的 outcome。返回新的事件键。

        与 `write_outcome` 的区别是刻意的：
        - **不复用原事件键**（加 `rev{n}` 后缀）⇒ 原记录原样留在账本里，可对照；
        - 载荷携带 `previous`（被取代的值）与 `reason`/`operator` ⇒ "改了什么、为什么、谁批的"可查；
        - 未结算的期限不走这里（那没有"历史"可修）。

        这是唯一能改写已记录结果的入口，且必须由人显式调用 —— 普通重跑永远不会走到。
        """
        if not str(reason or '').strip() or not str(operator or '').strip():
            raise ValueError('REVISION_REQUIRES_REASON_AND_OPERATOR')
        horizon = outcome['horizon']
        fields = ('label_as_of', 'return_pct', 'benchmark_return_pct',
                  'excess_return_pct', 'mfe_pct', 'mae_pct', 'realized_r',
                  'data_quality', 'body')
        metrics = fields[1:7]
        # 修订事件与投影在同一事务提交，失败时两者一起回滚。
        with self.events.transaction() as con:
            settled_rev, recorded = self._find_settled(con, decision_id, horizon, subject_key)
            if settled_rev is None:
                raise ValueError(f'NOT_SETTLED:{decision_id}:{horizon}:{subject_key}')
            new_rev = settled_rev + 1
            if new_rev > MAX_REVISIONS:
                raise ValueError(f'TOO_MANY_REVISIONS:{new_rev}>{MAX_REVISIONS}')
            row = con.execute(
                'SELECT ' + ','.join(fields) + ' FROM decision_outcomes_v2 '
                'WHERE account_scope=? AND decision_id=? AND horizon=? AND subject_key=?',
                (self.scope, decision_id, horizon, subject_key)).fetchone()
            if row is None:
                raise ValueError('REVISION_PROJECTION_MISSING')
            projection_before = dict(zip(fields, row))
            projection_before['body'] = json.loads(projection_before['body'])
            previous = dict(projection_before)
            # 老事件仅存两个指标；其余字段只能从投影保留。新版修订保存完整结果。
            previous.update({k: recorded[k] for k in fields if k in recorded})
            revised = {k: outcome[k] if k in outcome else previous[k] for k in fields}
            for k in metrics:
                if k in outcome:
                    revised[k] = quantize(outcome[k])
            payload = dict(revised, decision_id=decision_id, horizon=horizon,
                           subject_key=subject_key, revision=new_rev, reason=reason,
                           operator=operator, previous=previous,
                           projection_before=projection_before)
            insert_event(con, make_event(self.scope, 'outcome_observed',
                         [decision_id, horizon, subject_key, f'rev{new_rev}'], payload))
            con.execute(
                'UPDATE decision_outcomes_v2 SET ' + ','.join(k + '=?' for k in fields)
                + ' WHERE account_scope=? AND decision_id=? AND horizon=? AND subject_key=?',
                [json.dumps(revised[k], ensure_ascii=False) if k == 'body' else revised[k]
                 for k in fields] + [self.scope, decision_id, horizon, subject_key])
        return f'rev{new_rev}'

    def settle_selection(self, decision_id: str, code: str, closes: List[float],
                         benchmark_closes: Optional[List[float]] = None,
                         stop: Optional[float] = None, target: Optional[float] = None,
                         entry_price: Optional[float] = None,
                         data_quality: str = 'good') -> int:
        outcomes = selection_outcomes_for_code(code, closes, benchmark_closes, stop,
                                               target, entry_price, data_quality, decision_id)
        written = 0
        for o in outcomes:
            written += 1 if self.write_outcome(decision_id, o, subject_key=code) else 0
        return written

    def settle_entry(self, decision_id: str, entry_price: float,
                     templates: List[Dict[str, Any]],
                     closes: List[float]) -> int:
        n = 0
        for t in templates:
            o = entry_path_outcome(t, entry_price, closes)
            self.write_outcome(decision_id, {
                'horizon': '1d', 'label_as_of': utc(),
                'return_pct': o['return_pct'], 'benchmark_return_pct': None,
                'excess_return_pct': None, 'mfe_pct': None,
                'mae_pct': o['max_drawdown_r'], 'realized_r': o['net_r'],
                'data_quality': 'good', 'body': {'path': o['path'],
                                                 'quantity': o['quantity']},
            }, subject_key=o['path'])
            n += 1
        return n

    def settle_position(self, decision_id: str, trade: Dict[str, Any],
                        actual_exit_price: Optional[float],
                        mechanical_exit_price: Optional[float],
                        first_llm_exit_price: Optional[float],
                        closes: List[float]) -> int:
        o = position_outcome(trade, actual_exit_price, mechanical_exit_price,
                             first_llm_exit_price, closes)
        self.write_outcome(decision_id, {
            'horizon': 'exit', 'label_as_of': utc(),
            'return_pct': None, 'benchmark_return_pct': None,
            'excess_return_pct': None, 'mfe_pct': o['exit_mfe_pct'],
            'mae_pct': o['exit_mae_pct'], 'realized_r': o['saved_r'],
            'data_quality': 'good', 'body': o,
        }, subject_key=trade.get('trade_id', ''))
        return 1

    def settle_position_counterfactual(self, frozen: Dict[str, Any], bars) -> int:
        """结算 LLM 持仓反事实；每个成熟期限写一条 L/R 对照 outcome。"""
        from .position_counterfactual import simulate
        decision_id = frozen.get('decision_id') or frozen['counterfactual_id']
        subject = f"{frozen.get('trade_id', '')}:position_cf"
        outcomes = simulate(frozen, bars)
        for row in outcomes:
            self.write_outcome(decision_id, {
                'horizon': row['horizon'], 'label_as_of': utc(),
                'return_pct': row['l_return_pct'],
                'benchmark_return_pct': row['r_return_pct'],
                'excess_return_pct': row['delta_return_pct'],
                'mfe_pct': None, 'mae_pct': row['l_max_drawdown_pct'],
                'realized_r': None, 'data_quality': row['data_quality'],
                'body': dict(row, counterfactual_id=frozen['counterfactual_id'],
                             experiment_version=frozen['experiment_version'],
                             l_action=(frozen.get('l_path') or {}).get('action')),
            }, subject_key=subject)
        if outcomes:
            self.events.record('position_counterfactual_settled',
                               [frozen['counterfactual_id'], outcomes[-1]['horizon']],
                               {'counterfactual_id': frozen['counterfactual_id'],
                                'decision_id': decision_id, 'outcomes': outcomes},
                               trade_id=frozen.get('trade_id'), decision_id=decision_id)
        return len(outcomes)

    def settle_entry_counterfactual(self, frozen: Dict[str, Any], bars,
                                    fee_rate: float = 0.0) -> int:
        from .entry_counterfactual import simulate
        outcomes = simulate(frozen, bars, fee_rate=fee_rate)
        decision_id = frozen['decision_id']
        subject = f"{frozen.get('signal_id', '')}:entry_cf"
        for row in outcomes:
            self.write_outcome(decision_id, {
                'horizon': row['horizon'], 'label_as_of': utc(),
                'return_pct': row['l_return_pct'],
                'benchmark_return_pct': row['r_return_pct'],
                'excess_return_pct': row['delta_return_pct'],
                'mfe_pct': None, 'mae_pct': row['l_max_drawdown_pct'],
                'realized_r': None, 'data_quality': row['data_quality'],
                'body': dict(row, counterfactual_id=frozen['counterfactual_id'],
                             experiment_version=frozen['experiment_version'],
                             l_action=(frozen.get('llm_path') or {}).get('action')),
            }, subject_key=subject)
        if outcomes:
            self.events.record('entry_counterfactual_settled',
                               [frozen['counterfactual_id'], outcomes[-1]['horizon']],
                               {'counterfactual_id': frozen['counterfactual_id'],
                                'decision_id': decision_id, 'outcomes': outcomes},
                               signal_id=frozen.get('signal_id'), decision_id=decision_id)
        return len(outcomes)

    def settle_selection_counterfactual(self, frozen: Dict[str, Any], outcomes) -> int:
        """写入 Selection 容量 R/L 组合结果。"""
        subject = f"{frozen.get('batch_id', '')}:selection_cf"
        for row in outcomes:
            self.write_outcome(frozen['decision_id'], {
                'horizon': row['horizon'], 'label_as_of': utc(),
                'return_pct': row['l_return_pct'],
                'benchmark_return_pct': row['r_return_pct'],
                'excess_return_pct': row['delta_return_pct'],
                'mfe_pct': None, 'mae_pct': None, 'realized_r': None,
                'data_quality': 'good',
                'body': dict(row, counterfactual_id=frozen['counterfactual_id'],
                             experiment_version=frozen['experiment_version']),
            }, subject_key=subject)
        if outcomes:
            self.events.record('selection_counterfactual_settled',
                               [frozen['counterfactual_id'], outcomes[-1]['horizon']],
                               {'counterfactual_id': frozen['counterfactual_id'],
                                'outcomes': outcomes}, decision_id=frozen['decision_id'])
        return len(outcomes)


# ---------- 投影与事件不一致的核查与修复 ----------

def _projection_delta(*pairs):
    deltas = []
    for actual, expected in pairs:
        if actual is None or expected is None:
            if actual != expected:
                return None
            continue
        if not math.isfinite(actual) or not math.isfinite(expected):
            return None
        deltas.append(abs(actual - expected))
    return max(deltas, default=0.0)


def divergences(registry) -> List[Dict[str, Any]]:
    """找出**投影与账本事件不一致**的已结算行。只读。

    `write_outcome` 先在事务 A 更新投影、再在事务 B 写事件；两次之间失败（例如事件键
    冲突）会留下"投影是新值、事件是旧值"的行。事件是账本的真相，投影只是它的索引 ——
    两者不一致必须看得见。
    """
    settlement = OutcomeSettlement(registry)
    out: List[Dict[str, Any]] = []
    with settlement.events.transaction() as con:
        rows = con.execute(
            "SELECT decision_id, horizon, subject_key, return_pct, excess_return_pct "
            "FROM decision_outcomes_v2 WHERE data_quality='good' AND account_scope=?",
            (settlement.scope,)).fetchall()
        for decision_id, horizon, subject_key, ret, excess in rows:
            rev, payload = settlement._find_settled(con, decision_id, horizon,
                                                    subject_key or '')
            if payload is None:
                out.append({'decision_id': decision_id, 'horizon': horizon,
                            'subject_key': subject_key, 'kind': 'EVENT_MISSING',
                            'projection': {'return_pct': ret,
                                           'excess_return_pct': excess}})
                continue
            if (payload.get('return_pct') != ret
                    or payload.get('excess_return_pct') != excess):
                out.append({'decision_id': decision_id, 'horizon': horizon,
                            'subject_key': subject_key, 'kind': 'VALUE_DIVERGED',
                            'revision': rev,
                            'event': {'return_pct': payload.get('return_pct'),
                                      'excess_return_pct': payload.get('excess_return_pct')},
                            'projection': {'return_pct': ret,
                                           'excess_return_pct': excess},
                            'delta': _projection_delta(
                                (ret, payload.get('return_pct')),
                                (excess, payload.get('excess_return_pct')))})
    return out


def repair_projection(registry, *, operator: str, reason: str,
                      max_delta: float = 1e-6) -> Dict[str, Any]:
    """把投影**恢复成账本事件的值**（事件是真相，投影是索引）。**必须显式调用。**

    只修复**差异在 `max_delta` 以内**的行 —— 那才是"写事件时崩掉"留下的噪声残迹。
    差异更大的行意味着另有故事（换了数据源、改了模型），必须走 `revise_outcome` 显式修订，
    不能借"修复"之名把账本的值悄悄改掉。

    修复会留下 `outcome_projection_repaired` 事件（含原始投影值、原因、操作人），
    **不改动任何 outcome_observed 事件** —— 失败记录因此保留。
    """
    if not str(operator or '').strip() or not str(reason or '').strip():
        raise ValueError('REPAIR_REQUIRES_REASON_AND_OPERATOR')
    settlement = OutcomeSettlement(registry)
    repaired, refused = [], []
    for item in divergences(registry):
        if item['kind'] == 'EVENT_MISSING':
            # 事件缺失不是"投影写脏了"，而是"结算没写完"：留给正常结算补齐，不在这里动手
            refused.append({**item, 'why': '事件缺失：留给正常结算补齐'})
            continue
        if item['delta'] is None:
            refused.append({**item, 'why': '缺失值或非有限值不属于浮点噪声；须走 revise_outcome'})
            continue
        if item['delta'] > max_delta:
            refused.append({**item, 'why': f"差异 {item['delta']:.3e} > {max_delta:.0e}："
                                           f'须走 revise_outcome 显式修订'})
            continue
        key = [item['decision_id'], item['horizon'], item['subject_key'],
               f"repair{len(repaired) + 1}"]
        # 投影与修复事件**同一事务**：分两次提交时，若崩在中间，投影已修好而修复记录没写；
        # 而投影修好之后分歧就消失了，下次调用没有东西可修 —— **那条记录永久丢失**。
        # 这恰好发生在一个"存在意义就是留下审计轨迹"的函数里。
        with settlement.events.transaction() as con:
            con.execute(
                'UPDATE decision_outcomes_v2 SET return_pct=?, excess_return_pct=? '
                "WHERE account_scope=? AND decision_id=? AND horizon=? AND subject_key=? "
                "AND data_quality='good'",
                (item['event']['return_pct'], item['event']['excess_return_pct'],
                 settlement.scope, item['decision_id'], item['horizon'],
                 item['subject_key']))
            insert_event(con, make_event(settlement.scope, 'outcome_projection_repaired',
                                         key, {
                'decision_id': item['decision_id'], 'horizon': item['horizon'],
                'subject_key': item['subject_key'], 'kind': item['kind'],
                'projection_before': item['projection'], 'event_value': item['event'],
                'delta': item['delta'], 'reason': reason, 'operator': operator,
                'note': '投影恢复为账本事件的值；outcome_observed 未改动，失败记录保留'}))
        repaired.append(item)
    return {'repaired': len(repaired), 'refused': refused, 'items': repaired}
