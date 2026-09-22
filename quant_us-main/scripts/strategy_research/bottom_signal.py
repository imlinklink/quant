"""趋势回撤型抄底信号：状态机与逐日信号（规范 `docs/bottom-signal-spec-2026-09-21.md`）。

**第一版刻意窄**：「抄底」= 上升趋势中的回撤企稳，不尝试识别熊市最低点，不给「成功概率」。
它是**独立买入变体**，与 B3 对照 —— 替换的是 B3 的**整个入场包**（月排名 + 市场门 + 周线 +
日线），**不是「移除了日线过滤」**（那是 H-A，结论 CONCENTRATED，不重开）。

状态机（每 session 每证券评估一次，只用截至该日收盘的数据）：

    INELIGIBLE ──趋势成立 且 回撤达标──> WATCH ──收盘收回 MA20──> TRIGGERED（T+1 开盘执行）
                                          ├──趋势破坏 / 风险距离过宽 / ATR 无效──> INVALIDATED
                                          └──等待窗满 20 个 session──> EXPIRED

**同日冲突按固定优先级判**（写死在 `_evaluate` 的顺序里，不随实现漂移）：
`趋势破坏 → 风险距离过宽 → ATR 无效 → MA20 收回 → 等待窗过期`。
即**失效优先于确认**（fail-closed）：趋势破坏的同一天不因为一根阳线就算企稳。

**交棒与再入场**：`TRIGGERED` 之后由账户接管；下一 session 回到 `INELIGIBLE` 重新评估。
再次触发需要**重新跌破并收回 MA20**，所以被容量/现金拒绝的信号**不会次日自动重放**
（规范 §3「`UNFILLED` 不自动重试」）。`EXPIRED` / `INVALIDATED` 之后有 20 个 session 冷却。

**序列口径**：均线与回撤高点取自 `timed_entries._adjusted_bars`（跨拆股连续）—— 用原始价会在
NVDA 4:1/10:1、AMZN 20:1、GOOGL 20:1、AAPL 4:1 处出现**假趋势破坏**。`as_of` 取日历最后一天：
这些条件全是**比较型**，未来公司行动对整个回看窗施加同一乘法因子而抵消（P0-4 已证明）。

ATR 用面板既有的 `asof_atr`（与既有止损同一波动率口径），且**两侧都以「相对当前收盘价」的
比值**比较（左侧是复权比值，与基准无关；右侧是同 session 的原始价比值）—— 不新造第二套 ATR。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from scripts.live_trading.decision_ledger.event_store import stable_id
from scripts.medium_term.entry_risk import medium_initial_stop
from scripts.medium_term.timed_entries import _adjusted_bars
from scripts.portfolio_shadow.schema import Opportunity, to_micro

STATE_INELIGIBLE = 'INELIGIBLE'
STATE_WATCH = 'WATCH'
STATE_TRIGGERED = 'TRIGGERED'
STATE_EXPIRED = 'EXPIRED'
STATE_INVALIDATED = 'INVALIDATED'

ENTRY_RULE = 'bottom_reclaim_v1'
#: 同日多个信号的排序：**非信息性**（账户在容量约束下按证券代码顺序取，确定性但任意）。
#: 规范没有给出横截面优先级，所以不发明一个 —— 实现成 rank=1 并在报告里披露。
SIGNAL_RANK = 1


@dataclass(frozen=True)
class SignalParams:
    """规范里逐条固定的数值。**改动 = 另立登记**（有测试逐项核对登记文件）。"""
    trend_ma: int = 200
    trend_slope_lookback: int = 20
    reclaim_ma: int = 20
    drawdown_lookback: int = 252
    drawdown_atr_multiple: int = 3
    drawdown_floor: float = 0.10
    max_wait_sessions: int = 20
    cooldown_sessions: int = 20
    max_stop_distance_frac: float = 0.25

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _f(value):
    return None if value is None or pd.isna(value) else float(value)


def features_for(group: pd.DataFrame, actions, as_of, params: SignalParams) -> pd.DataFrame:
    """单只证券的逐日特征（列全部来自同一条复权序列，或同 session 的原始价比值）。"""
    group = group.sort_values('session').reset_index(drop=True)
    adj = _adjusted_bars(group, actions, as_of).sort_values('session').reset_index(drop=True)
    close = pd.to_numeric(adj.raw_close, errors='coerce')
    raw_close = pd.to_numeric(group.raw_close, errors='coerce')
    atr = pd.to_numeric(group.asof_atr, errors='coerce')
    out = pd.DataFrame({
        'session': pd.to_datetime(adj.session).dt.normalize(),
        'close': close,
        'ma_reclaim': close.rolling(params.reclaim_ma, min_periods=params.reclaim_ma).mean(),
        'ma_trend': close.rolling(params.trend_ma, min_periods=params.trend_ma).mean(),
        'high_lookback': close.rolling(params.drawdown_lookback,
                                       min_periods=params.drawdown_lookback).max(),
        'atr_abs': atr,
        'atr_frac': atr / raw_close,
    })
    out['ma_trend_prev'] = out.ma_trend.shift(params.trend_slope_lookback)
    out['close_prev'] = out.close.shift(1)
    out['ma_reclaim_prev'] = out.ma_reclaim.shift(1)
    return out


class BottomSignalGenerator:
    """逐日状态机。接口与 `candidate_adapter.IncrementalCandidateGenerator` 对齐
    （`opportunities_for(session)`），故两臂可以共用同一个账户循环。"""

    def __init__(self, *, prices, calendar, actions, quality, experiment_id: str,
                 parent_version: str, exit_policy_id: str = 'H60',
                 universe: set | None = None, params: SignalParams | None = None,
                 observed_at: str | None = None):
        self.params = params or SignalParams()
        self.experiment_id = experiment_id
        self.parent_version = parent_version
        self.exit_policy_id = exit_policy_id
        self.calendar = [pd.Timestamp(s).normalize() for s in calendar]
        self._index = {s: i for i, s in enumerate(self.calendar)}
        self._observed_at = observed_at
        universe = universe if universe is not None else set(prices.security_id.astype(str))
        self._intervals = {}
        if quality is not None and not quality.empty:
            self._intervals = {
                str(r.security_id): (pd.Timestamp(r.from_session).normalize(),
                                     pd.Timestamp(r.to_session).normalize())
                for r in quality.loc[quality.quality_status.eq('verified')].itertuples()}
        as_of = self.calendar[-1] if self.calendar else None
        self.features = {}
        # 执行日基准的 ATR（面板 asof_atr × scale_to_next，与既有 B3 的 `_atr_micro` 同一做法）
        atr_next = {}
        for r in prices.itertuples(index=False):
            sid = str(r.security_id)
            if sid not in universe:
                continue
            value = None
            try:
                scaled = float(r.asof_atr) * float(r.scale_to_next)
                if pd.notna(scaled) and 0 < scaled < float('inf'):
                    value = to_micro(scaled)
            except (TypeError, ValueError):
                value = None
            atr_next[(sid, pd.Timestamp(r.session).normalize())] = value
        for sid, group in prices.groupby('security_id'):
            sid = str(sid)
            if sid in universe:
                self.features[sid] = features_for(group, actions, as_of, self.params).set_index(
                    'session')
        self._atr_next = atr_next
        self.state = {sid: STATE_INELIGIBLE for sid in self.features}
        self.watch_session: dict = {}
        self.cooldown_until: dict = {}
        self.signals: list = []   # 用户可见的逐次状态变化（规范 §4）

    # ---- 判定（顺序即优先级，见模块头）----
    def _trend_ok(self, row) -> bool:
        if pd.isna(row.ma_trend) or pd.isna(row.ma_trend_prev):
            return False
        return bool(row.close > row.ma_trend and row.ma_trend > row.ma_trend_prev)

    def _atr_unavailable(self, row) -> bool:
        """ATR 不可用或缺席。上界 0.4 不是审美：`medium_initial_stop` 在
        `2.5 × ATR > 价格` 时给出非正止损 —— 那是数据问题，不是"风险距离很宽"。"""
        if pd.isna(row.atr_frac):
            return True
        return not (0 < float(row.atr_frac) < 0.4)

    def _drawdown_frac(self, row):
        if pd.isna(row.high_lookback) or pd.isna(row.close) or row.close <= 0:
            return None
        return (row.high_lookback - row.close) / row.close

    def _drawdown_ok(self, row) -> bool:
        drawdown = self._drawdown_frac(row)
        if drawdown is None or self._atr_unavailable(row):
            return False
        threshold = max(self.params.drawdown_atr_multiple * row.atr_frac,
                        self.params.drawdown_floor)
        return bool(drawdown >= threshold)

    def _stop_distance_frac(self, row):
        """初始止损距离占入场价的比例 = `max(8%, 2.5 × ATR/价)`。

        用**单位价**调用同一份 `medium_initial_stop`（唯一一份定义），得到的就是这个比例。
        刻意不用「绝对价 + 绝对 ATR」：`close` 是复权价、面板 `asof_atr` 是原始价基准，
        混用会在复权因子大的地方算出**非正止损**（实测 GOOGL/AMZN 直接抛
        `INITIAL_STOP_NON_POSITIVE`）。比例两边同基，不会。
        """
        return 1.0 - medium_initial_stop(1.0, float(row.atr_frac))

    def _too_wide(self, row) -> bool:
        if self._atr_unavailable(row):
            return False     # 数据不可用走另一条失效原因，不混为一谈
        return bool(self._stop_distance_frac(row) > self.params.max_stop_distance_frac)

    def _reclaimed_ma(self, row) -> bool:
        if pd.isna(row.ma_reclaim) or pd.isna(row.ma_reclaim_prev) or pd.isna(row.close_prev):
            return False
        return bool(row.close_prev <= row.ma_reclaim_prev and row.close > row.ma_reclaim)

    def _record(self, sid, session, state, reason, row, **extra) -> None:
        entry = {'security_id': sid, 'session': str(session.date()), 'state': state,
                 'reason': reason, 'rule_version': ENTRY_RULE,
                 'watch_session': (str(self.watch_session[sid].date())
                                   if sid in self.watch_session else None),
                 'data_through': str(session.date()), 'version': self.params.as_dict()}
        if row is not None:
            drawdown = self._drawdown_frac(row)
            entry.update({
                'close': _f(row.close), 'ma_trend': _f(row.ma_trend),
                'ma_reclaim': _f(row.ma_reclaim), 'high_lookback': _f(row.high_lookback),
                'drawdown_frac': drawdown,
                'drawdown_atr_multiples': (None if drawdown is None or self._atr_unavailable(row)
                                           else drawdown / float(row.atr_frac)),
                'atr_frac': _f(row.atr_frac), 'trend_ok': self._trend_ok(row)})
        entry.update(extra)
        self.signals.append(entry)

    def opportunities_for(self, session) -> list[Opportunity]:
        session = pd.Timestamp(session).normalize()
        i = self._index.get(session)
        if i is None:
            return []
        nxt = self.calendar[i + 1] if i + 1 < len(self.calendar) else None
        out = []
        for sid in sorted(self.features):
            frame = self.features[sid]
            if session not in frame.index:
                continue
            interval = self._intervals.get(sid)
            if not (interval and interval[0] <= session <= interval[1]):
                continue                      # 未核实/超出质量区间：不产生信号、不推进状态机
            cooling = self.cooldown_until.get(sid)
            if cooling is not None:
                if session < cooling:
                    continue
                # 冷却结束：回到 INELIGIBLE 重新竞争（而不是永远卡在 EXPIRED/INVALIDATED）
                self.cooldown_until.pop(sid, None)
                self.watch_session.pop(sid, None)
                self.state[sid] = STATE_INELIGIBLE
            row = frame.loc[session]
            state = self.state[sid]
            if state == STATE_TRIGGERED:
                # 交棒给账户；下一 session 重新评估（再一次触发需要重新跌破并收回 MA20）
                self.state[sid] = STATE_INELIGIBLE
                self.watch_session.pop(sid, None)
                continue
            if state == STATE_WATCH:
                if not self._trend_ok(row):
                    self._invalidate(sid, session, row, 'TREND_BROKEN')
                    continue
                if self._too_wide(row):
                    self._invalidate(sid, session, row, 'RISK_DISTANCE_TOO_WIDE')
                    continue
                if self._atr_unavailable(row):
                    self._invalidate(sid, session, row, 'ATR_UNAVAILABLE')
                    continue
                if self._reclaimed_ma(row) and nxt is not None:
                    self.state[sid] = STATE_TRIGGERED
                    self._record(sid, session, STATE_TRIGGERED, 'MA20_RECLAIMED', row,
                                 earliest_execution_session=str(nxt.date()),
                                 stop_distance_frac=self._stop_distance_frac(row))
                    out.append(self._opportunity(sid, session, nxt, row))
                    continue
                waited = self._index[session] - self._index[self.watch_session[sid]]
                if waited >= self.params.max_wait_sessions:
                    self.state[sid] = STATE_EXPIRED
                    self._set_cooldown(sid, session)
                    self._record(sid, session, STATE_EXPIRED, 'WAIT_WINDOW_EXPIRED', row,
                                 waited_sessions=waited)
                    continue
            if state == STATE_INELIGIBLE:
                if self._atr_unavailable(row):
                    continue                  # 数据不足：不进入 WATCH，也不记失效
                if self._trend_ok(row) and self._drawdown_ok(row):
                    self.state[sid] = STATE_WATCH
                    self.watch_session[sid] = session
                    self._record(sid, session, STATE_WATCH, 'TREND_UP_AND_DRAWDOWN_MET', row)
        return out

    def _invalidate(self, sid, session, row, reason) -> None:
        self.state[sid] = STATE_INVALIDATED
        self._set_cooldown(sid, session)
        self._record(sid, session, STATE_INVALIDATED, reason, row)

    def _set_cooldown(self, sid, session) -> None:
        j = min(self._index[session] + self.params.cooldown_sessions + 1, len(self.calendar))
        self.cooldown_until[sid] = self.calendar[j - 1]

    def _opportunity(self, sid, session, nxt, row) -> Opportunity:
        atr_micro = self._atr_next.get((sid, session)) or to_micro(float(row.atr_abs))
        return Opportunity(
            experiment_id=self.experiment_id, security_id=sid,
            source_candidate_id=f'{sid}-{self.watch_session[sid].date()}',
            parent_version=self.parent_version,
            signal_session=str(session.date()),
            observed_at=self._observed_at or f'{session.date()}T00:00:00+00:00',
            planned_execution_session=str(nxt.date()), rank=SIGNAL_RANK, entry_rule=ENTRY_RULE,
            stop_reference={'atr14_micro': atr_micro}, exit_policy_id=self.exit_policy_id,
            input_hash=stable_id('bottom_signal', sid, str(session.date()),
                                 *(f'{k}={v}' for k, v in sorted(self.params.as_dict().items()))),
            signal_generated_at=self._observed_at or '',
            decision_deadline=str(nxt.date()), market_snapshot_id='frozen-study-inputs',
            rule_reason_codes=('TREND_UP_AND_DRAWDOWN_MET', 'MA20_RECLAIMED'))
