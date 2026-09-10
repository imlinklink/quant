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
        return {'horizon_stats': horizon_stats, 'outcome_count': len(rows)}

    # ---------- Entry ----------

    def entry_metrics(self) -> Dict[str, Any]:
        """defer/reject 避免的亏损与延后造成的错失收益（相对规则立即买入反事实）。"""
        with self.events.transaction() as con:
            rows = con.execute(
                'SELECT horizon,realized_r,return_pct,body FROM decision_outcomes_v2 '
                'WHERE account_scope=? AND decision_id IN '
                '(SELECT decision_id FROM llm_decision_runs WHERE account_scope=? AND role=?)',
                (self.scope, self.scope, 'entry')).fetchall()
        import json as _json
        paths: Dict[str, List[float]] = {}
        for h, rr, ret, body in rows:
            p = _json.loads(body).get('path', 'unknown') if body else 'unknown'
            if rr is not None:
                paths.setdefault(p, []).append(float(rr))
        return {'path_net_r': {k: {'count': len(v), 'mean_net_r': sum(v) / len(v)}
                               for k, v in paths.items()},
                'outcome_count': len(rows)}

    # ---------- Position ----------

    def position_metrics(self) -> Dict[str, Any]:
        """saved_r / premature_exit_cost_r / invalidation_lead_time。"""
        with self.events.transaction() as con:
            rows = con.execute(
                'SELECT realized_r,mae_pct,body FROM decision_outcomes_v2 '
                'WHERE account_scope=? AND horizon=? AND decision_id IN '
                '(SELECT decision_id FROM llm_decision_runs WHERE account_scope=? AND role=?)',
                (self.scope, 'exit', self.scope, 'position')).fetchall()
        import json as _json
        saved = [r[0] for r in rows if r[0] is not None]
        premature = []
        for _, _, body in rows:
            b = _json.loads(body) if body else {}
            if b.get('premature_exit_cost_r') is not None:
                premature.append(float(b['premature_exit_cost_r']))
        return {
            'saved_r': {'count': len(saved), 'mean': sum(saved) / len(saved)} if saved else None,
            'premature_exit_cost_r': {'count': len(premature), 'mean': sum(premature) / len(premature)} if premature else None,
            'outcome_count': len(rows),
        }

    # ---------- 健康 ----------

    def health(self) -> Dict[str, Any]:
        """运行健康（§21）：连续相同动作、数据质量、outcome 覆盖。"""
        with self.events.transaction() as con:
            actions = con.execute(
                "SELECT body FROM decision_events WHERE account_scope=? "
                "AND event_type='decision_effective_action' ORDER BY observed_at DESC LIMIT 10",
                (self.scope,)).fetchall()
            validated = con.execute(
                'SELECT COUNT(*) FROM llm_decision_runs WHERE account_scope=? AND status=?',
                (self.scope, 'validated')).fetchone()[0]
            outcomes = con.execute(
                'SELECT COUNT(DISTINCT decision_id) FROM decision_outcomes_v2 WHERE account_scope=?',
                (self.scope,)).fetchone()[0]
        import json as _json
        last_effective = []
        for (body,) in actions:
            p = _json.loads(body).get('payload', {})
            last_effective.append(p.get('effective_action'))
        all_same = len(set(last_effective)) <= 1 if last_effective else False
        return {
            'all_same_action': all_same,
            'outcome_coverage': {'validated': int(validated), 'settled': int(outcomes)},
        }
