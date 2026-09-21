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

# 未来行情成熟度用的前向窗口，必须与 `p2_selection_check.build_entries` 的
# `INSUFFICIENT_FORWARD_BARS` / 行动覆盖窗口径一致：批处理用 max(HORIZONS)=120，
# **不是**策略自身的 60 交易日持有期。由 test_incremental 钉死两者相等。
DEFAULT_FORWARD_HORIZON = 120


def audit_stamps(session, now=None) -> dict:
    """§5.3 的两个墙钟审计字段（与 `Opportunity.signal_generated_at` 同一约定）。

    `observed_at` = 该观察所依据的**数据截止**（信号日收盘）；`generated_at` = 生成该行的
    可信时刻。**历史重建下两者都取截止本身** —— 确定性、可重放；真实前向运行才注入实际
    时刻。§5.3 明令「墙钟审计字段不应导致同一次历史确定性重放产生新的业务身份」，而一个
    自己调 `now()` 的写法会让每次重跑都生成不同的行；在历史重建里那更糟：它会把"今天
    生成"写成"当时观察到的"。

    `generated_at` 不用 `defaultdict` 之类兜底，也不在这里取 `now()`：没有可信时钟就取截止。
    """
    from .evidence import entry_market_cutoff
    cutoff = entry_market_cutoff(session)
    return {'observed_at': cutoff, 'generated_at': now() if now else cutoff}


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
            terminal='READY',
            parent_strategy_id=item.get('parent_strategy_id', ''),
            signal_generated_at=item.get('signal_generated_at', item['observed_at']),
            decision_deadline=item.get('decision_deadline', ''),
            rule_reason_codes=tuple(item.get('rule_reason_codes') or ()),
            market_snapshot_id=item.get('market_snapshot_id', '')))
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
            exit_policy_id=exit_policy_id, input_hash='', terminal='READY',
            parent_strategy_id=str(getattr(row, 'parent_strategy_id', '') or ''),
            signal_generated_at=str(pd.Timestamp(row.decision_session).date())
                                + 'T00:00:00+00:00',
            market_snapshot_id=f'asof-{pd.Timestamp(row.decision_session).date()}')
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
                 forward_horizon: int = DEFAULT_FORWARD_HORIZON,
                 require_matured: bool = True, parent_strategy_id: str = '', now=None,
                 observation_sink=None):
        self.observation_sink = observation_sink
        self.prices = prices
        self.view_bars = prices[['security_id', 'session', 'raw_open', 'raw_high', 'raw_low',
                                 'raw_close', 'volume']].rename(columns={
            'raw_open': 'open', 'raw_high': 'high', 'raw_low': 'low', 'raw_close': 'close'})
        self.market = market.set_index('session') if 'session' in market.columns else market
        self.quality = quality
        self.actions = actions
        self.blocked = blocked
        # 未来行情成熟度窗口，与 build_entries 的 max(HORIZONS) 同口径（不是策略持有期）
        self.forward_horizon = forward_horizon
        # True = 研究/对拍模式：要求未来结果已成熟，与冻结矩阵口径一致；
        # False = 真实前向运行：决策日只知道截至当天的数据，不得据未来 bar 丢弃候选。
        self.require_matured = require_matured
        self.calendar = pd.DatetimeIndex(pd.to_datetime(list(calendar))).normalize().sort_values().unique()
        self.experiment_id = experiment_id
        self.parent_version = parent_version
        self.parent_strategy_id = parent_strategy_id
        # 可信时钟（设计 §4 signal_generated_at）。默认取信号日收盘：确定性、可重放；
        # 真实前向运行可注入实际生成时刻。机会是「首次写入即冻结」的，注入墙钟不会
        # 改写已落库的机会。
        self._now = now
        self.exit_policy_id = exit_policy_id
        self.top_n = top_n
        self.max_wait_sessions = max_wait_sessions
        self.entry_rule = entry_rule
        self.pending: dict = {}
        # 候选轮次身份（§5.1「一个证券在一次月度候选轮次中的身份」）。cid 里含轮次日期，
        # 但那是字符串拼接，不能靠切字符串还原 —— 等待窗内逐日观察的 session 早已不是
        # 轮次日期，得从创建时记下来。
        self._rounds: dict = {}
        self._month_ends = set(month_end_sessions(self.calendar))
        self._bars_raw = {str(sid): g for sid, g in prices.groupby('security_id')}
        self._atr_key = prices.set_index(['security_id', 'session'])
        self._signals = None  # 惰性缓存：{sid: (weekly_uptrend Series, entry_signal Series)}
        self._intervals = {}
        if not quality.empty and 'quality_status' in quality.columns:
            self._intervals = {
                str(r.security_id): (pd.Timestamp(r.from_session).normalize(),
                                     pd.Timestamp(r.to_session).normalize())
                for r in quality.loc[quality.quality_status.eq('verified')].itertuples()}

    def round_for(self, cid: str) -> str:
        """候选人所属月度轮次的日期。直接索引 —— 取不到就是调用方给了个不是候选的 id，
        静默回落到"观察当天"会把轮次身份写成另一件事。"""
        return self._rounds[cid]

    def _observe(self, cid, sid, session, stage, result, reason='', *, reasons=(),
                 features=None, thresholds=None, state=None):
        """记一次门观察。`reasons` 是**该候选此刻已可判定的全部失败条件**，按固定优先级
        （阶段顺序）排列；`reason` 只在**本阶段自己判定为失败**时给出，且必须是首项。

        §5.3 的两条要求在这里落地：
        · 「同时满足多个拒绝条件时记录**所有可计算的条件**，以固定优先级选主原因」——
          条件来自 `reasons`，主原因固定取首项；
        · 「未执行的后续条件标 `not_evaluated`，**不能当失败**」—— 未执行就没有主原因
          （`Funnel` 会拒绝 reason 非空却标 not_evaluated 的行）。可计算的条件仍全部留在
          `all_reasons` 里作影子诊断（§5.3 明确允许），它们不得被读成"这个阶段判过它"。
        """
        if self.observation_sink is None:
            return
        all_reasons = list(reasons) if reasons else ([reason] if reason else [])
        self.observation_sink({
            'candidate_id': cid, 'security_id': sid, 'session': str(session.date()),
            'candidate_round': self._rounds[cid],
            'stage': stage, 'result': result, 'primary_reason': reason,
            'all_reasons': all_reasons,
            'feature_values': features or {}, 'thresholds': thresholds or {},
            'candidate_state': state, 'rule_version': self.parent_version,
            **audit_stamps(session, self._now)})

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
        # 轮次身份**在 sink 之前、无条件**登记：观察器不得改变生成器的任何状态
        # （§14「观察器旁路」）。这里覆盖当月截面里的**每一只**证券 —— 未入选的那些
        # 也是候选轮次的一员（它们的落选原因正是漏斗要解释的东西）。
        round_session = str(session.date())
        for sid_ in snap['security_id'].astype(str):
            self._rounds[f'{sid_}-{round_session}'] = round_session
        if self.observation_sink is not None:
            market_missing = session not in self.market.index or pd.isna(self.market.loc[session, 'asof_ma200'])
            for row in snap.to_dict('records'):
                sid = str(row['security_id'])
                cid = f'{sid}-{session.date()}'
                eligible = bool(row['eligible'])
                ranked = eligible and pd.notna(row['rank']) and row['rank'] <= self.top_n
                interval = self._intervals.get(sid)
                verified = bool(interval and interval[0] <= session <= interval[1])
                # §5.3「记录所有可计算的条件」：这些条件**与该候选是否走到那一步无关**，
                # 只要此刻已可判定就记下来（否则"某道门到底排除了多少候选"永远算不出来）。
                # 顺序 = 阶段顺序，也就是主原因的固定优先级。
                conditions = []
                if not eligible:
                    conditions.append(str(row.get('reject_reason') or 'UNIVERSE_REJECTED'))
                if market_missing:
                    conditions.append('MARKET_DATA_MISSING')
                elif not gate:
                    conditions.append('MARKET_GATE_CLOSED')
                if eligible and not ranked:
                    conditions.append('BELOW_TOP_N')
                if eligible and not verified:
                    conditions.append('QUALITY_UNVERIFIED')
                # 终态与它的原因**一起**定，不用嵌套三元式：状态和原因分成两处推导，
                # 正是它们会悄悄对不上的原因（原先 `not_evaluated` 的行还挂着失败原因）。
                if not eligible:
                    state, state_reason = 'DATA_BLOCKED', conditions[0]
                elif market_missing:
                    state, state_reason = 'DATA_BLOCKED', 'MARKET_DATA_MISSING'
                elif not gate:
                    state, state_reason = 'RULE_REJECTED', 'MARKET_GATE_CLOSED'
                elif not ranked:
                    state, state_reason = 'RULE_REJECTED', 'BELOW_TOP_N'
                elif not verified:
                    state, state_reason = 'DATA_BLOCKED', 'QUALITY_UNVERIFIED'
                else:
                    state, state_reason = 'WAITING', ''
                self._observe(cid, sid, session, 'universe', 'pass' if eligible else 'unknown',
                              '' if eligible else conditions[0],
                              reasons=conditions, features=row)
                self._observe(cid, sid, session, 'market',
                              ('unknown' if market_missing else 'pass' if gate else 'reject')
                              if eligible else 'not_evaluated',
                              ('MARKET_DATA_MISSING' if market_missing
                               else 'MARKET_GATE_CLOSED' if not gate else '') if eligible else '',
                              reasons=conditions,
                              features=(self.market.loc[session].to_dict()
                                        if session in self.market.index else {}),
                              thresholds={'market_data_present': not market_missing})
                self._observe(cid, sid, session, 'ranking',
                              ('pass' if ranked else 'reject') if eligible and gate else 'not_evaluated',
                              'BELOW_TOP_N' if (eligible and gate and not ranked) else '',
                              reasons=conditions, features={'rank': row['rank']},
                              thresholds={'top_n': self.top_n})
                self._observe(cid, sid, session, 'quality',
                              ('pass' if verified else 'reject') if ranked and gate else 'not_evaluated',
                              'QUALITY_UNVERIFIED' if (ranked and gate and not verified) else '',
                              reasons=conditions, features={'interval': interval})
                self._observe(cid, sid, session, 'candidate_state',
                              'pass' if state == 'WAITING' else 'unknown' if state == 'DATA_BLOCKED' else 'reject',
                              state_reason, reasons=conditions, state=state)
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

    def precompute_signals(self) -> None:
        """预计算每只证券的逐日信号（周线门 + 突破/回踩）并缓存。

        as_of 用日历最后一天；P0-4 已证明入场信号是比较型、对 as_of 锚定不变，故与逐日等价。
        全量回测/对拍用此步加速（O(证券) 而非 O(候选×session)）；真正的前向逐日运行可跳过。
        """
        from scripts.medium_term.timed_entries import _adjusted_bars, entry_signals, weekly_regime
        self._signals = {}
        last = self.calendar[-1]
        for sid, group in self._bars_raw.items():
            adj = _adjusted_bars(group, self.actions, last)
            regime = weekly_regime(adj, self.calendar).set_index('session')['weekly_uptrend']
            sig = entry_signals(adj).set_index('session')['entry_signal']
            self._signals[sid] = (regime, sig)

    def _signal_for(self, sid: str, session) -> str | None:
        if self._signals is not None:
            regime, sig = self._signals[sid]
            up = bool(regime.get(session, False))
            kind = sig.get(session, '')
            # 缓存分支也要填明细：漏斗的周线/日线观察靠它区分"周线没过"与"日线没信号"。
            # 让观察的丰富程度取决于一个无关的加速开关，等于同一份数据两种口径。
            self._last_signal_detail = {'weekly_uptrend': up, 'entry_signal': kind, 'daily': {}}
            return kind if up and kind else None
        from scripts.medium_term.timed_entries import _adjusted_bars, entry_signals, weekly_regime
        group = self._bars_raw.get(sid)
        if group is None or group[group.session.eq(session)].empty:
            return None
        adj = _adjusted_bars(group, self.actions, session)
        regime = weekly_regime(adj, self.calendar).set_index('session')
        signals = entry_signals(adj).set_index('session')
        up = bool(regime.weekly_uptrend.get(session, False))
        kind = signals.entry_signal.get(session, '')
        self._last_signal_detail = {'weekly_uptrend': up, 'entry_signal': kind,
                                    'daily': signals.loc[session].to_dict() if session in signals.index else {}}
        return kind if up and kind else None

    def _atr_micro(self, sid: str, signal_session) -> int | None:
        try:
            atr = float(self._atr_key.loc[(sid, signal_session), 'asof_atr']) * float(
                self._atr_key.loc[(sid, signal_session), 'scale_to_next'])
            return to_micro(atr) if pd.notna(atr) and 0 < atr < float("inf") else None
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
                self._observe(cid, cand['security_id'], session, 'candidate_state', 'reject',
                              'WAIT_WINDOW_EXPIRED', state='EXPIRED',
                              thresholds={'max_wait_sessions': self.max_wait_sessions})
                del self.pending[cid]  # 等待窗过期
                continue
            self._last_signal_detail = {}
            kind = self._signal_for(cand['security_id'], session)
            detail = self._last_signal_detail
            # `_signal_for` 的返回值把周线门与日线信号**乘在一起**了；要如实区分"周线没过"
            # 与"日线没信号"，必须取明细里的**原始**日线信号（`entry_signal` 未经门控）。
            raw_kind = str(detail.get('entry_signal') or '')
            up = bool(detail.get('weekly_uptrend'))
            conditions = []
            if not up:
                conditions.append('WEEKLY_NOT_CONFIRMED')
            if not raw_kind:
                conditions.append('NO_ENTRY_SIGNAL')
            self._observe(cid, cand['security_id'], session, 'weekly',
                          ('pass' if up else 'reject') if detail else 'unknown',
                          'WEEKLY_NOT_CONFIRMED' if (detail and not up) else '',
                          reasons=conditions if detail else [], features=detail)
            self._observe(cid, cand['security_id'], session, 'daily',
                          ('pass' if raw_kind else 'reject') if up else 'not_evaluated',
                          'NO_ENTRY_SIGNAL' if (up and not raw_kind) else '',
                          reasons=conditions if detail else [], features=detail)
            if not kind:
                # 无信号 ⇒ 该候选今天仍活着（WAITING），只是这一天的日线门没过。
                # **明细为空**说明 `_signal_for` 提前返回（该 session 没有 bar）—— 那是数据
                # 缺口，不是一次策略判断：标 DATA_BLOCKED，不标 NO_ENTRY_SIGNAL。
                # 注意判据是 `conditions` 为空而**不是** `detail` 为空 —— `_signal_for` 是可以
                # 被打桩替换的（测试就这么做），那种情况下明细也是空的，但候选是正常的。
                self._observe(cid, cand['security_id'], session, 'candidate_state',
                              'reject' if conditions else 'unknown',
                              conditions[0] if conditions else 'SESSION_BAR_MISSING',
                              reasons=conditions or ['SESSION_BAR_MISSING'],
                              state='WAITING' if conditions else 'DATA_BLOCKED')
                continue
            exec_sess = next_session(self.calendar, session)
            if exec_sess is None:
                self._observe(cid, cand['security_id'], session, 'candidate_state', 'unknown',
                              'NEXT_SESSION_UNAVAILABLE', state='WAITING')
                continue
            atr = self._atr_micro(cand['security_id'], session)
            if atr is None:
                self._observe(cid, cand['security_id'], session, 'candidate_state', 'unknown',
                              'ATR_MISSING', state='DATA_BLOCKED')
                del self.pending[cid]  # 缺 ATR，无法定止损
                continue
            # 以下是「未来结果是否已成熟」的检查，只在研究/对拍模式成立：真实前向运行
            # 在决策日只知道截至当天的数据，用未来 bar / 未来行动丢弃候选等于作弊。
            # 前向模式下的数据缺口由引擎运行时兜底（缺行情 → VALUATION_INCOMPLETE /
            # PROVISIONAL，公司行动在除息日按公告应用）。
            if self.require_matured:
                # 前向 bar 充足性（与 build_entries 的 INSUFFICIENT_FORWARD_BARS 一致）
                group = self._bars_raw.get(cand['security_id'])
                if group is None or len(group[group.session >= exec_sess]) < self.forward_horizon:
                    del self.pending[cid]
                    continue
                # 行动覆盖门（与 build_entries 一致）：入场→退出窗落在 blocked 日期则丢弃
                exit_pos = int(self.calendar.searchsorted(exec_sess)) + self.forward_horizon - 1
                exit_sess = self.calendar[exit_pos] if exit_pos < len(self.calendar) else exec_sess
                bdays = self.blocked.get(cand['security_id'], ())
                if any(exec_sess <= pd.Timestamp(d).normalize() <= exit_sess for d in bdays):
                    del self.pending[cid]
                    continue
            self._observe(cid, cand['security_id'], session, 'candidate_state', 'pass', '',
                          features={'entry_signal': kind, 'atr14_micro': atr,
                                    'execution_session': exec_sess},
                          state='READY')
            from .evidence import entry_market_cutoff, entry_response_deadline
            cutoff = entry_market_cutoff(session)
            ready.append(Opportunity(
                experiment_id=self.experiment_id, security_id=cand['security_id'],
                source_candidate_id=cid, parent_version=self.parent_version,
                signal_session=str(session.date()), observed_at=cutoff,
                planned_execution_session=str(exec_sess.date()), rank=cand['rank'],
                entry_rule=self.entry_rule, stop_reference={'atr14_micro': atr},
                exit_policy_id=self.exit_policy_id, input_hash='', terminal='READY',
                parent_strategy_id=self.parent_strategy_id,
                signal_generated_at=(self._now() if self._now else cutoff),
                decision_deadline=entry_response_deadline(exec_sess),
                # 规则侧的入场原因：模型据此理解「规则为何产生这个机会」，不自行推算
                rule_reason_codes=tuple(c for c in (
                    kind, 'WEEKLY_UPTREND',
                    'MARKET_GATE_OPEN' if self._market_gate(session) else '') if c),
                market_snapshot_id=f'asof-{session.date()}'))
            del self.pending[cid]
        return ready
