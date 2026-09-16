"""B3@60@1% 父策略 → Opportunity（共同机会流）。

两种来源：
- `adapt_schedule`：排程 fixture（工程联调用）；
- `build_real_schedule`：真实 B3 信号（月度动量前5 + 周线门 + 日线择时），复用
  `p2_selection_check` 的 point-in-time 管线，产出与冻结历史矩阵一致的候选。
"""
from __future__ import annotations

import pandas as pd

from scripts.medium_term.monthly_calendar import month_end_sessions, next_session

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


class IncrementalCandidateGenerator:
    """逐日增量信号生成（真实 B3，as-of，不预读未来、不每步重算全 schedule）。

    维护 pending 状态：月末用 point-in-time 动量选前 top_n（QQQ 门开）入 pending；对每个 pending
    在等待窗内逐日 as-of 检查周线门 + 日线突破/回踩，命中产出 Opportunity（次日开盘执行）。
    """

    def __init__(self, prices: pd.DataFrame, market: pd.DataFrame, quality: pd.DataFrame,
                 actions: pd.DataFrame, blocked: dict, calendar, *,
                 experiment_id: str, parent_version: str, exit_policy_id: str = 'H60',
                 top_n: int = 5, max_wait_sessions: int = 20, entry_rule: str = 'b3',
                 horizon: int = 60):
        self.prices = prices
        self.view_bars = prices[['security_id', 'session', 'raw_open', 'raw_high', 'raw_low',
                                 'raw_close', 'volume']].rename(columns={
            'raw_open': 'open', 'raw_high': 'high', 'raw_low': 'low', 'raw_close': 'close'})
        self.market = market.set_index('session') if 'session' in market.columns else market
        self.quality = quality
        self.actions = actions
        self.blocked = blocked
        self.horizon = horizon
        self.calendar = pd.DatetimeIndex(pd.to_datetime(list(calendar))).normalize().sort_values().unique()
        self.experiment_id = experiment_id
        self.parent_version = parent_version
        self.exit_policy_id = exit_policy_id
        self.top_n = top_n
        self.max_wait_sessions = max_wait_sessions
        self.entry_rule = entry_rule
        self.pending: dict = {}
        self._month_ends = set(month_end_sessions(self.calendar))
        self._bars_raw = {str(sid): g for sid, g in prices.groupby('security_id')}
        self._atr_key = prices.set_index(['security_id', 'session'])
        self._intervals = {}
        if not quality.empty and 'quality_status' in quality.columns:
            self._intervals = {
                str(r.security_id): (pd.Timestamp(r.from_session).normalize(),
                                     pd.Timestamp(r.to_session).normalize())
                for r in quality.loc[quality.quality_status.eq('verified')].itertuples()}

    def _market_gate(self, session) -> bool:
        if session not in self.market.index:
            return False
        row = self.market.loc[session]
        return bool(pd.notna(row.get('asof_ma200')) and
                    float(row['asof_close']) > float(row['asof_ma200']))

    def _generate_monthly(self, session) -> None:
        from scripts.medium_term.momentum_features import point_in_time_momentum_snapshot
        from scripts.medium_term.stock_cross_section import rank_cross_section
        snap = rank_cross_section(point_in_time_momentum_snapshot(
            self.view_bars, self.actions, session))
        gate = self._market_gate(session)
        sel = snap[snap.eligible.astype(bool) & snap['rank'].le(self.top_n)]
        if not gate:
            return
        for r in sel.itertuples(index=False):
            sid = str(r.security_id)
            if sid not in self._intervals:
                continue  # 未核实
            start, end = self._intervals[sid]
            if not (start <= session <= end):
                continue  # 决策日超出质量区间
            self.pending[f'{sid}-{session.date()}'] = {
                'security_id': sid, 'decision_session': session, 'rank': int(r.rank)}

    def _signal_for(self, sid: str, session) -> str | None:
        from scripts.medium_term.timed_entries import _adjusted_bars, entry_signals, weekly_regime
        group = self._bars_raw.get(sid)
        if group is None or group[group.session.eq(session)].empty:
            return None
        adj = _adjusted_bars(group, self.actions, session)
        regime = weekly_regime(adj, self.calendar).set_index('session')
        signals = entry_signals(adj).set_index('session')
        up = bool(regime.weekly_uptrend.get(session, False))
        kind = signals.entry_signal.get(session, '')
        return kind if up and kind else None

    def _atr_micro(self, sid: str, signal_session) -> int | None:
        try:
            atr = float(self._atr_key.loc[(sid, signal_session), 'asof_atr']) * float(
                self._atr_key.loc[(sid, signal_session), 'scale_to_next'])
            return to_micro(atr)
        except KeyError:
            return None

    def opportunities_for(self, session) -> list[Opportunity]:
        session = pd.Timestamp(session).normalize()
        ready = []
        if session in self._month_ends:
            self._generate_monthly(session)
        for cid, cand in list(self.pending.items()):
            start = next_session(self.calendar, cand['decision_session'])
            if start is None:
                del self.pending[cid]
                continue
            ref_window = self.calendar[self.calendar >= start][:self.max_wait_sessions]
            if session < start:
                continue
            if session not in ref_window:
                del self.pending[cid]  # 等待窗过期
                continue
            kind = self._signal_for(cand['security_id'], session)
            if not kind:
                continue
            exec_sess = next_session(self.calendar, session)
            if exec_sess is None:
                continue
            atr = self._atr_micro(cand['security_id'], session)
            if atr is None:
                del self.pending[cid]  # 缺 ATR，无法定止损
                continue
            # 行动覆盖门（与 build_entries 一致）：入场→退出窗落在 blocked 日期则丢弃
            exit_pos = int(self.calendar.searchsorted(exec_sess)) + self.horizon - 1
            exit_sess = self.calendar[exit_pos] if exit_pos < len(self.calendar) else exec_sess
            bdays = self.blocked.get(cand['security_id'], ())
            if any(exec_sess <= pd.Timestamp(d).normalize() <= exit_sess for d in bdays):
                del self.pending[cid]
                continue
            ready.append(Opportunity(
                experiment_id=self.experiment_id, security_id=cand['security_id'],
                source_candidate_id=cid, parent_version=self.parent_version,
                signal_session=str(session.date()), observed_at=f'{session.date()}T00:00:00+00:00',
                planned_execution_session=str(exec_sess.date()), rank=cand['rank'],
                entry_rule=self.entry_rule, stop_reference={'atr14_micro': atr},
                exit_policy_id=self.exit_policy_id, input_hash='', terminal='READY'))
            del self.pending[cid]
        return ready
