"""B3@60@1% 父策略 → Opportunity（共同机会流）。

本轮为 fixture 驱动：从冻结的机会排程生成 Opportunity。真实增量信号生成
（月度动量前5 + 周线门 + 日线择时，逐日 as-of）在 PR3 末尾接入，并与历史引擎
同口径逐日对比。排程条目字段与 Opportunity 一一对应。
"""
from __future__ import annotations

from .schema import Opportunity


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


def intents_for_session(opportunities: list[Opportunity], session: str) -> list[Opportunity]:
    """取出某 session 计划执行、尚未终态的候选（READY）。"""
    return [o for o in opportunities
            if o.planned_execution_session == session and o.terminal == 'READY']
