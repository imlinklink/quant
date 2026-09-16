"""多期限结果结算（技术设计 §16）：selection / entry / position 的 outcome 投影。

从冻结计划 + 后续价格序列计算标签，写 decision_outcomes_v2，并 append outcome_observed 事件。
所有反事实使用决策时已冻结的计划、下一可成交时刻与统一滑点模型；跳空用第一可成交价格。
"""
import logging
from typing import Any, Dict, List, Optional

from .event_store import EventStore, stable_id, utc

logger = logging.getLogger(__name__)

HORIZONS = (1, 3, 5, 10, 20)


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

    def write_outcome(self, decision_id: str, outcome: Dict[str, Any],
                      subject_key: str = '') -> None:
        cols = ['account_scope', 'decision_id', 'horizon', 'subject_key', 'label_as_of',
                'return_pct', 'benchmark_return_pct', 'excess_return_pct', 'mfe_pct',
                'mae_pct', 'realized_r', 'data_quality', 'body']
        import json as _json
        row = {
            'account_scope': self.scope,
            'decision_id': decision_id,
            'horizon': outcome['horizon'],
            'subject_key': subject_key,
            'label_as_of': outcome.get('label_as_of', utc()),
            'return_pct': outcome.get('return_pct'),
            'benchmark_return_pct': outcome.get('benchmark_return_pct'),
            'excess_return_pct': outcome.get('excess_return_pct'),
            'mfe_pct': outcome.get('mfe_pct'),
            'mae_pct': outcome.get('mae_pct'),
            'realized_r': outcome.get('realized_r'),
            'data_quality': outcome.get('data_quality', 'good'),
            'body': _json.dumps(outcome.get('body', {}), ensure_ascii=False, default=str),
        }
        with self.events.transaction() as con:
            con.execute(
                'INSERT OR REPLACE INTO decision_outcomes_v2 (' + ','.join(cols) +
                ') VALUES (' + ','.join('?' for _ in cols) + ')',
                [row[c] for c in cols])
        if outcome.get('data_quality', 'good') == 'pending_future_bars':
            return  # 占位：只更新投影，不写 outcome_observed，避免 pending→completed 事件冲突
        self.events.record('outcome_observed', [decision_id, outcome['horizon'], subject_key],
                           {'decision_id': decision_id, 'horizon': outcome['horizon'],
                            'subject_key': subject_key,
                            'return_pct': outcome.get('return_pct'),
                            'excess_return_pct': outcome.get('excess_return_pct')})

    def settle_selection(self, decision_id: str, code: str, closes: List[float],
                         benchmark_closes: Optional[List[float]] = None,
                         stop: Optional[float] = None, target: Optional[float] = None,
                         entry_price: Optional[float] = None,
                         data_quality: str = 'good') -> int:
        outcomes = selection_outcomes_for_code(code, closes, benchmark_closes, stop,
                                               target, entry_price, data_quality, decision_id)
        for o in outcomes:
            self.write_outcome(decision_id, o, subject_key=code)
        return len(outcomes)

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
