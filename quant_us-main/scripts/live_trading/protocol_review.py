"""协议复盘排期（设计 §6.5 / §13 P2 的 Review 排期）。

周期：**每周一次**（§13 P2「建立角色级周报」）。到期判定只回答"该不该跑"，
真正跑起来由 `run` 负责，且它**只写事件**：

  `protocol_review_skipped`    没有统计基础，未调用模型（幂等，按周期键）
  `protocol_review_completed`  跑了（含动作、成本、样本量）
  `protocol_change_candidate`  确有建议改动（由 `protocol_changes` 落账，须人工批准）

三条纪律：

1. **没有统计基础就不调用模型**。关联不到任何已结算样本时，调用只会得到"样本不足"，
   却照样花钱。此时记 `protocol_review_skipped` 并**不消耗本周认领** —— 数据晚到还能再跑。
2. **认领在真要调用之前才做**（`claim_daily_job('protocol_review', 周期键)`，键是任意
   字符串，周键照样适用）。认领之后重跑不再调用付费模型。
3. **本模块没有任何写配置或改权限的路径**。Review 的出口只有一条：协议候选。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from scripts.live_trading.decision_engine import DecisionEngine
from scripts.live_trading.decision_ledger.event_store import EventStore, utc
from scripts.live_trading.review_scheduler import DEFAULT_TZ, ReviewScheduler

JOB_TYPE = 'protocol_review'


class ProtocolReviewScheduler:
    """每周一次的协议复盘排期。只做「何时跑 + 幂等 + 组包」，模型调用在 `run` 里。"""

    def __init__(self, registry, config: Optional[dict] = None):
        self.registry = registry
        self.config = config or {}
        self.events = EventStore(registry)
        self.scope = registry.namespace

    # ---------- 到期判定 ----------

    def _cfg(self) -> dict:
        return (self.config.get('llm_decision') or {}).get('protocol_review') or {}

    def period_key(self, now: Optional[datetime] = None,
                   tz: str = DEFAULT_TZ) -> str:
        """周期键 = ISO 年-周。用它做认领，周内重跑天然幂等。"""
        now = now or datetime.now(ZoneInfo(tz))
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo(tz))
        now = now.astimezone(ZoneInfo(tz))
        iso = now.isocalendar()
        return f'{iso[0]}-W{iso[1]:02d}'

    def period_due(self, now: Optional[datetime] = None,
                   tz: str = DEFAULT_TZ) -> Optional[str]:
        """到达配置的星期几与时刻即返回周期键；否则 None。

        默认 `enabled: false` —— 排期会发起**付费**调用，不该在无人点头时自动开始。
        """
        cfg = self._cfg()
        if not cfg.get('enabled', False):
            return None
        now = now or datetime.now(ZoneInfo(tz))
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo(tz))
        now = now.astimezone(ZoneInfo(tz))
        weekday = int(cfg.get('weekday', 5))
        hh, mm = (int(x) for x in str(cfg.get('time', '18:30')).split(':')[:2])
        if now.weekday() != weekday or (now.hour, now.minute) < (hh, mm):
            return None
        return self.period_key(now, tz)

    # ---------- 运行 ----------

    def _record(self, event_type, key, payload):
        self.events.record(event_type, key, payload)

    def run(self, *, advisor=None, call_model=None, now: Optional[datetime] = None,
            tz: str = DEFAULT_TZ, force_period: Optional[str] = None,
            horizon: Optional[str] = None, retry: bool = False) -> Dict[str, Any]:
        """到期则跑一次。返回可审计的结果摘要（**不含任何配置改动**）。

        `retry=True` 跳过周期认领，供**人工**在失败后重跑同一周期。必须显式给出：
        认领在调用之前，所以一次瞬时失败会烧掉整周，没有这个出口就只能等下一周。
        代价是同一周期可能付费调用两次 —— 因此它只出现在人工命令里，且有事件留痕。
        """
        period = force_period or self.period_due(now, tz)
        if period is None:
            return {'skipped': 'NOT_DUE', 'period': self.period_key(now, tz)}

        from scripts.live_trading.decision_ledger.review_stats import (
            build_review_packet, horizon_availability, pick_horizon)

        availability = horizon_availability(self.registry)
        chosen = horizon or pick_horizon(availability)
        if chosen is None:
            # 没有可关联的已结算样本：调用只会得到"样本不足"，却照样花钱。
            # **不认领**本周期 —— 数据晚到还能再跑。
            self._record('protocol_review_skipped', period, {
                'period': period, 'reason': 'NO_SETTLED_SAMPLES',
                'horizon_availability': availability,
                'note': '未调用模型（没有统计基础）；本周期未被消耗，数据到齐后可重跑'})
            return {'skipped': 'NO_SETTLED_SAMPLES', 'period': period,
                    'horizon_availability': availability}

        # 认领必须在真要调用**之前**：认领后重跑不再调用付费模型。
        # `retry=True` 是人工出口：跳过认领，允许同一周期重跑（会再付一次费）。
        if not retry and not ReviewScheduler(self.registry,
                                             self.config).claim_daily_job(JOB_TYPE, period):
            return {'skipped': 'ALREADY_RUN', 'period': period}

        packet = build_review_packet(
            self.registry,
            protocol_version=str(self._cfg().get('protocol_version') or 'unversioned'),
            account_scope=self.scope,
            subject_id=f'protocol_review:{period}', as_of=utc(), horizon=chosen)
        engine = DecisionEngine(self.registry, advisor=advisor, config=self.config,
                                call_model=call_model)
        result = engine.decide_review(packet)

        summary: Dict[str, Any] = {'period': period, 'horizon': chosen,
                                   'decision_id': result.decision_id,
                                   'status': result.status,
                                   'model_action': result.model_action,
                                   'permission_level': result.permission_level,
                                   'validation_errors': list(result.validation_errors or ())}
        if result.status == 'validated':
            from mutifactor.llm.contracts.review_v1 import protocol_candidate_from
            candidate = protocol_candidate_from(result.validated_output or {}, packet)
            if candidate is None:
                summary['candidate'] = None
                summary['note'] = ('模型未提出改动（父策略）；样本不足时这是正确答案，'
                                   '不构成失败')
            else:
                from scripts.live_trading.decision_ledger.protocol_changes import (
                    record_candidate)
                summary['candidate'] = record_candidate(self.registry, candidate)
                summary['candidate_variable'] = candidate['variable']
                summary['note'] = ('候选已落账，**须人工批准**才会成为新版本；'
                                   '本系统不会自动应用')
        else:
            summary['candidate'] = None
            summary['note'] = '决策未通过校验，未产生候选'

        cost = (getattr(advisor, 'last_metadata', None) or {}).get('cost_usd')
        summary['model_cost_usd'] = cost
        self._record('protocol_review_completed', period,
                     {'period': period, 'horizon': chosen, **summary})
        return summary


def _make_advisor(config: dict):
    """真实模型客户端。与 live 侧同源配置（`config['llm']`）。"""
    import yaml
    from mutifactor.llm import LLMAdvisor
    root = Path(__file__).resolve().parents[2]
    llm_cfg = (config.get('llm') if isinstance(config, dict) else None) or (
        yaml.safe_load((root / 'config.yaml').read_text()) or {}).get('llm', {})
    return LLMAdvisor(llm_cfg)


def main(argv=None) -> int:
    import yaml
    from scripts.live_trading.position_registry import registry_for
    parser = argparse.ArgumentParser(
        description='协议复盘排期（默认只报到不到期，不发模型调用）')
    parser.add_argument('--config', default=None)
    parser.add_argument('--registry', default=None)
    parser.add_argument('--scope', default=None, help='账本 namespace；缺省取配置的 account_scope')
    parser.add_argument('--model', choices=('fixture', 'real'), default=None,
                        help='缺省则只判定是否到期；给了才真正执行')
    parser.add_argument('--force-period', default=None,
                        help='手动指定周期键（补跑/演练），跳过到期判定')
    parser.add_argument('--horizon', default=None)
    parser.add_argument('--retry', action='store_true',
                        help='人工重跑同一周期：跳过周期认领（会再付一次模型费）')
    parser.add_argument('--output', default=None)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load(Path(args.config or root / 'config.yaml').read_text()) or {}
    scheduler = ProtocolReviewScheduler(registry_for(config, args.registry, args.scope),
                                       config)
    if args.model is None:
        print(json.dumps({'due_period': scheduler.period_due(),
                          'current_period': scheduler.period_key(),
                          'enabled': scheduler._cfg().get('enabled', False)},
                         ensure_ascii=False))
        return 0
    kwargs: Dict[str, Any] = {'horizon': args.horizon,
                              'force_period': args.force_period,
                              'retry': args.retry}
    if args.model == 'real':
        kwargs['advisor'] = _make_advisor(config)
    else:
        # 夹具：确定性地产出"不改动"（父策略）。--model fixture 不会产生候选。
        def fixture(_contract, _packet):
            return {'schema_version': 'review-v1',
                    'packet_id': _packet.get('packet_id'), 'status': 'complete',
                    'failure_patterns': [], 'proposed_change': None,
                    'expected_improvement': None, 'possible_regression': [],
                    'validation_plan': None, 'reason_codes': [],
                    'missing_information': ['夹具模型：不提出改动']}
        kwargs['call_model'] = fixture
    summary = scheduler.run(**kwargs)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                     encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
