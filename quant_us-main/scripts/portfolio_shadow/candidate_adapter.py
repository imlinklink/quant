"""B3@60@1% 父策略 → Opportunity（共同机会流）。

两种来源：
- `adapt_schedule`：排程 fixture（工程联调用）；
- `build_real_schedule`：真实 B3 信号（月度动量前5 + 周线门 + 日线择时），复用
  `p2_selection_check` 的 point-in-time 管线，产出与冻结历史矩阵一致的候选。
"""
from __future__ import annotations

import pandas as pd

from .schema import Opportunity, to_micro


def adapt_schedule(experiment_id: str, parent_version: str, entry_rule: str,
                   exit_policy_id: str, schedule: list[dict]) -> list[Opportunity]:
    """把冻结排程（每 session 的 READY 机会）转成 Opportunity 列表。

    schedule 条目：security_id / source_candidate_id / signal_session / observed_at /
    planned_execution_session / rank / atr14_micro / input_hash。
    """
    out = []
    for item in schedule:
        out.append(Opportunity(
            experiment_id=experiment_id, security_id=item['security_id'],
            source_candidate_id=item['source_candidate_id'], parent_version=parent_version,
            signal_session=item['signal_session'], observed_at=item['observed_at'],
            planned_execution_session=item['planned_execution_session'], rank=int(item['rank']),
            entry_rule=entry_rule,
            stop_reference={'atr14_micro': int(item['atr14_micro'])},
            exit_policy_id=exit_policy_id, input_hash=item['input_hash'],
            terminal='READY'))
    return out


def build_real_schedule(prices: pd.DataFrame, market: pd.DataFrame, quality: pd.DataFrame,
                        actions: pd.DataFrame, blocked: dict, etf_path, *,
                        experiment_id: str, parent_version: str, exit_policy_id: str = 'H60',
                        top_n: int = 5, max_wait_sessions: int = 20) -> tuple[dict, dict]:
    """真实 B3 候选 → {execution_session: [Opportunity]} + 漏斗。

    复用 p2_selection_check 的 point-in-time 信号管线：generate_monthly_candidates（月度动量前5）
    → build_timed_entries（周线门 + 日线突破/回踩）→ build_entries（算 initial_stop）。
    """
    from scripts.medium_term.p2_selection_check import (build_entries, build_timed_entries,
                                                        generate_monthly_candidates,
                                                        trading_calendar)
    view_bars = prices[['security_id', 'session', 'raw_open', 'raw_high', 'raw_low',
                        'raw_close', 'volume']].rename(columns={
        'raw_open': 'open', 'raw_high': 'high', 'raw_low': 'low', 'raw_close': 'close'})
    candidates = generate_monthly_candidates(view_bars, market, top_n=top_n, actions=actions)
    selected = candidates[candidates.selected.astype(bool)].copy()
    timed = build_timed_entries(
        selected[['security_id', 'decision_session', 'execution_session', 'rank']],
        prices[['security_id', 'session', 'raw_open', 'raw_high', 'raw_low',
                'raw_close', 'volume']],
        trading_calendar(etf_path), actions=actions, max_wait_sessions=max_wait_sessions)
    spec = timed[timed.entry_type.ne('EXPIRED') & timed.execution_session.notna()]
    prepared, funnel = build_entries(spec, prices, quality, blocked,
                                     atr_session_col='signal_session')
    schedule: dict = {}
    for row in prepared.itertuples(index=False):
        opp = Opportunity(
            experiment_id=experiment_id, security_id=str(row.security_id),
            source_candidate_id=str(row.entry_id), parent_version=parent_version,
            signal_session=str(pd.Timestamp(row.decision_session).date()),
            observed_at=str(pd.Timestamp(row.decision_session).date()) + 'T00:00:00+00:00',
            planned_execution_session=str(pd.Timestamp(row.entry_session).date()),
            rank=int(row.rank), entry_rule='b3',
            stop_reference={'initial_stop_micro': to_micro(row.initial_stop)},
            exit_policy_id=exit_policy_id, input_hash='', terminal='READY')
        schedule.setdefault(opp.planned_execution_session, []).append(opp)
    return schedule, funnel


def intents_for_session(opportunities: list[Opportunity], session: str) -> list[Opportunity]:
    """取出某 session 计划执行、尚未终态的候选（READY）。"""
    return [o for o in opportunities
            if o.planned_execution_session == session and o.terminal == 'READY']
