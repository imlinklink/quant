"""从事件投影生成查询指标（技术设计 §16/§17/§21）。

供评估看板 API 使用：selection / entry / position 的表现统计、模型 vs 规则增量、
数据质量、延迟、model action vs effective action 分布。
"""
import logging
import math
from typing import Any, Dict, List, Optional

from .decision_ledger.event_store import EventStore

logger = logging.getLogger(__name__)


def _quantiles(values: List[float], qs=(0.5, 0.95)) -> Dict[str, Optional[float]]:
    if not values:
        return {f'p{int(q * 100)}': None for q in qs}
    s = sorted(values)
    out = {}
    for q in qs:
        idx = min(len(s) - 1, int(math.ceil(q * len(s)) - 1))
        out[f'p{int(q * 100)}'] = s[idx]
    return out


class ProjectDecisionMetrics:
    """查询 llm_decision_runs / llm_model_attempts / decision_outcomes_v2 生成指标。"""

    def __init__(self, registry):
        self.events = EventStore(registry)
        self.scope = registry.namespace

    # ---------- 通用 ----------

    def overview(self, role: Optional[str] = None) -> Dict[str, Any]:
        with self.events.transaction() as con:
            where = 'WHERE r.account_scope=?'
            args: list = [self.scope]
            if role:
                where += ' AND r.role=?'
                args.append(role)
            rows = con.execute(
                'SELECT r.role, r.status, COUNT(*) FROM llm_decision_runs r ' + where +
                ' GROUP BY r.role, r.status', args).fetchall()
            attempts = con.execute(
                'SELECT r.role, a.status, a.latency_ms FROM llm_model_attempts a '
                'JOIN llm_decision_runs r '
                'ON a.decision_id = r.decision_id AND a.account_scope = r.account_scope ' + where,
                args).fetchall()
        by_role: Dict[str, Dict[str, int]] = {}
        for r, s, c in rows:
            by_role.setdefault(r, {})[s] = int(c)
        latency = {'all': _quantiles([a[2] for a in attempts if a[2] is not None])}
        return {
            'by_role': by_role,
            'latency': latency,
            'attempts_total': len(attempts),
        }

    def action_distribution(self) -> List[Dict[str, Any]]:
        with self.events.transaction() as con:
            rows = con.execute(
                "SELECT body FROM decision_events WHERE account_scope=? "
                "AND event_type='decision_effective_action'", (self.scope,)).fetchall()
        import json as _json
        dist: Dict[str, int] = {}
        for (body,) in rows:
            p = _json.loads(body).get('payload', {})
            key = f"{p.get('model_action')}->{p.get('effective_action')}"
            dist[key] = dist.get(key, 0) + 1
        return [{'transition': k, 'count': v} for k, v in sorted(dist.items())]

    # ---------- Selection ----------

    def selection_metrics(self) -> Dict[str, Any]:
        """Top-N 相对规则基线的超额收益 / 排除避损 / 按置信度命中率。"""
        with self.events.transaction() as con:
            rows = con.execute(
                'SELECT horizon,return_pct,excess_return_pct,mfe_pct,mae_pct,body '
                'FROM decision_outcomes_v2 WHERE account_scope=? AND decision_id IN '
                '(SELECT decision_id FROM llm_decision_runs WHERE account_scope=? AND role=?)',
                (self.scope, self.scope, 'selection')).fetchall()
            counterfactual_rows = con.execute(
                "SELECT horizon,excess_return_pct FROM decision_outcomes_v2 "
                "WHERE account_scope=? AND subject_key LIKE ?",
                (self.scope, '%:selection_cf')).fetchall()
        import json as _json
        by_horizon: Dict[str, List[float]] = {}
        for h, ret, excess, mfe, mae, body in rows:
            if ret is None:
                continue
            by_horizon.setdefault(h, []).append(float(ret))
        horizon_stats = {
            h: {'count': len(v), 'mean_return_pct': sum(v) / len(v)}
            for h, v in by_horizon.items()
        }
        counterfactual = {}
        for horizon in sorted({r[0] for r in counterfactual_rows}):
            values = [float(delta) for h, delta in counterfactual_rows
                      if h == horizon and delta is not None]
            if values:
                counterfactual[horizon] = {
                    'count': len(values), 'mean_delta_return_pct': sum(values) / len(values),
                    'llm_win_rate': sum(value > 0 for value in values) / len(values)}
        return {'horizon_stats': horizon_stats, 'outcome_count': len(rows),
                'counterfactual': counterfactual}

    # ---------- Entry ----------

    def entry_metrics(self) -> Dict[str, Any]:
        """defer/reject 避免的亏损与延后造成的错失收益（相对规则立即买入反事实）。"""
        with self.events.transaction() as con:
            rows = con.execute(
                'SELECT horizon,realized_r,return_pct,body FROM decision_outcomes_v2 '
                'WHERE account_scope=? AND decision_id IN '
                '(SELECT decision_id FROM llm_decision_runs WHERE account_scope=? AND role=?)',
                (self.scope, self.scope, 'entry')).fetchall()
            counterfactual_rows = con.execute(
                "SELECT horizon,excess_return_pct,body FROM decision_outcomes_v2 "
                "WHERE account_scope=? AND subject_key LIKE ?",
                (self.scope, '%:entry_cf')).fetchall()
        import json as _json
        paths: Dict[str, List[float]] = {}
        for h, rr, ret, body in rows:
            p = _json.loads(body).get('path', 'unknown') if body else 'unknown'
            if rr is not None:
                paths.setdefault(p, []).append(float(rr))
        by_horizon: Dict[str, List[Dict[str, float]]] = {}
        for horizon, delta, body in counterfactual_rows:
            if delta is None:
                continue
            parsed = _json.loads(body) if body else {}
            by_horizon.setdefault(horizon, []).append({
                'delta': float(delta),
                'saved_loss': float(parsed.get('saved_loss_pct') or 0.0),
                'missed_upside': float(parsed.get('missed_upside_pct') or 0.0),
            })
        counterfactual = {
            horizon: {
                'count': len(values),
                'mean_delta_return_pct': sum(v['delta'] for v in values) / len(values),
                'llm_win_rate': sum(v['delta'] > 0 for v in values) / len(values),
                'mean_saved_loss_pct': sum(v['saved_loss'] for v in values) / len(values),
                'mean_missed_upside_pct': sum(v['missed_upside'] for v in values) / len(values),
            } for horizon, values in sorted(by_horizon.items())
        }
        return {'path_net_r': {k: {'count': len(v), 'mean_net_r': sum(v) / len(v)}
                               for k, v in paths.items()},
                'outcome_count': len(rows), 'counterfactual': counterfactual,
                'counterfactual_outcome_count': len(counterfactual_rows)}

    # ---------- Position ----------

    def position_metrics(self) -> Dict[str, Any]:
        """saved_r / premature_exit_cost_r / invalidation_lead_time。"""
        with self.events.transaction() as con:
            rows = con.execute(
                'SELECT realized_r,mae_pct,body FROM decision_outcomes_v2 '
                'WHERE account_scope=? AND horizon=? AND decision_id IN '
                '(SELECT decision_id FROM llm_decision_runs WHERE account_scope=? AND role=?)',
                (self.scope, 'exit', self.scope, 'position')).fetchall()
            counterfactual_rows = con.execute(
                "SELECT horizon,return_pct,benchmark_return_pct,excess_return_pct,mae_pct,body "
                "FROM decision_outcomes_v2 WHERE account_scope=? AND subject_key LIKE ?",
                (self.scope, '%:position_cf')).fetchall()
        import json as _json
        saved = [r[0] for r in rows if r[0] is not None]
        premature = []
        for _, _, body in rows:
            b = _json.loads(body) if body else {}
            if b.get('premature_exit_cost_r') is not None:
                premature.append(float(b['premature_exit_cost_r']))
        by_horizon: Dict[str, List[Dict[str, float]]] = {}
        for horizon, l_ret, r_ret, delta, l_mae, body in counterfactual_rows:
            if delta is None:
                continue
            parsed = _json.loads(body) if body else {}
            by_horizon.setdefault(horizon, []).append({
                'delta': float(delta),
                'drawdown_delta': float(l_mae or 0.0) - float(
                    parsed.get('r_max_drawdown_pct') or 0.0),
                'saved_loss': float(parsed.get('saved_loss_pct') or 0.0),
                'missed_upside': float(parsed.get('missed_upside_pct') or 0.0),
            })
        counterfactual = {}
        for horizon, values in sorted(by_horizon.items()):
            counterfactual[horizon] = {
                'count': len(values),
                'mean_delta_return_pct': sum(v['delta'] for v in values) / len(values),
                'llm_win_rate': sum(v['delta'] > 0 for v in values) / len(values),
                'mean_drawdown_delta_pct': sum(v['drawdown_delta'] for v in values) / len(values),
                'mean_saved_loss_pct': sum(v['saved_loss'] for v in values) / len(values),
                'mean_missed_upside_pct': sum(v['missed_upside'] for v in values) / len(values),
            }
        return {
            'saved_r': {'count': len(saved), 'mean': sum(saved) / len(saved)} if saved else None,
            'premature_exit_cost_r': {'count': len(premature), 'mean': sum(premature) / len(premature)} if premature else None,
            'outcome_count': len(rows),
            'counterfactual': counterfactual,
            'counterfactual_outcome_count': len(counterfactual_rows),
        }

    # ---------- 健康 ----------

    def health(self, min_action_sample: int = 10) -> Dict[str, Any]:
        """运行健康（§21）：连续相同动作、数据质量、outcome 覆盖。"""
        with self.events.transaction() as con:
            actions = con.execute(
                "SELECT e.body,r.subject_id FROM decision_events e "
                "LEFT JOIN llm_decision_runs r ON r.account_scope=e.account_scope "
                "AND r.decision_id=json_extract(e.body,'$.decision_id') "
                "WHERE e.account_scope=? AND e.event_type='decision_effective_action' "
                "ORDER BY e.observed_at DESC LIMIT ?",
                (self.scope, int(min_action_sample))).fetchall()
            validated = con.execute(
                'SELECT COUNT(*) FROM llm_decision_runs WHERE account_scope=? AND status=?',
                (self.scope, 'validated')).fetchone()[0]
            outcomes = con.execute(
                'SELECT COUNT(DISTINCT decision_id) FROM decision_outcomes_v2 WHERE account_scope=?',
                (self.scope,)).fetchone()[0]
        import json as _json
        last_effective = []
        groups = set()
        for body, subject_id in actions:
            p = _json.loads(body).get('payload', {})
            last_effective.append(p.get('effective_action'))
            groups.add(subject_id or p.get('decision_id'))
        sample_size = len(last_effective)
        enough = sample_size >= min_action_sample and len(groups) >= min_action_sample
        all_same = len(set(last_effective)) == 1 if enough else None
        return {
            'all_same_action': all_same,
            'all_same_action_alert': bool(all_same) if enough else False,
            'status': ('abnormal' if all_same else 'ok') if enough else 'insufficient_sample',
            'sample_size': sample_size,
            'independence_group_count': len(groups),
            'minimum_sample_size': int(min_action_sample),
            'outcome_coverage': {'validated': int(validated), 'settled': int(outcomes)},
        }
