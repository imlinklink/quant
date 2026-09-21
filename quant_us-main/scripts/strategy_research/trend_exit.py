"""趋势退出规则 MA20_60_NEXT_OPEN_V1（预登记 `EXIT-TREND-MA2060-20260922`，设计 §6.3）。

    T 收盘：`close <= MA20` **且** `close < MA60` ⇒ 冻结一条次日开盘的退出意图

**唯一一份定义**：MA 与信号都在这里算，引擎只消费「某证券在某执行日有一条已冻结的退出意图」。
这样引擎不需要懂指标，规则也不会在引擎与 runner 里各写一遍。

**为什么意图作为输入、而不是写进持仓状态**：写状态要给 `Position` 加字段 ⇒ 账本 schema 递增 ⇒
**刚启动的 L1 实验（schema 10）账本立刻不可读**。而意图本来就是**当日输入**的一部分
（与 `intents`、`bars`、`atr` 同类），重放也能从成交事件完整还原 ⇒ 不必进状态。
代价是「意图必须带 `frozen_at`」，由引擎断言它**早于**执行日 —— 否则就是用当天的信息
在当天成交（前视），fail-closed。

序列一律用**复权**收盘价（与既有信号管线同一处 `_adjusted_bars`）：用原始价会在
NVDA/AMZN/GOOGL/AAPL 的拆股处把 MA20/MA60 打成假的趋势破坏 —— 而这条规则正是靠 MA 判趋势。
"""
from __future__ import annotations

import pandas as pd

from scripts.medium_term.timed_entries import _adjusted_bars

POLICY_ID = 'MA20_60_NEXT_OPEN_V1'
MA_FAST = 20
MA_SLOW = 60
#: 事件与归因里的退出原因名（`exit_attribution.KNOWN_EXIT_REASONS` 必须含它）
EXIT_REASON = 'TREND_EXIT'


def exit_signals(bars: pd.DataFrame, actions, as_of, *,
                 ma_fast: int = MA_FAST, ma_slow: int = MA_SLOW) -> pd.DataFrame:
    """逐 session 的退出信号与质量状态（**单一实现**）。

    返回列：`session`、`ma20`、`ma60`、`valid`、`exit_signal`。

    `valid=False`（预热不足/非有限值）时 `exit_signal` 恒为 False —— 缺失**不产生退出**，
    这是 fail-safe 的方向：宁可继续持有（硬止损仍在），也不要因为一个 NaN 平掉仓位。
    """
    group = bars.sort_values('session').reset_index(drop=True)
    adj = _adjusted_bars(group, actions, as_of).sort_values('session').reset_index(drop=True)
    close = pd.to_numeric(adj.raw_close, errors='coerce')
    out = pd.DataFrame({'session': pd.to_datetime(adj.session).dt.normalize(), 'close': close})
    out['ma20'] = close.rolling(ma_fast, min_periods=ma_fast).mean()
    out['ma60'] = close.rolling(ma_slow, min_periods=ma_slow).mean()
    out['valid'] = out[['close', 'ma20', 'ma60']].notna().all(axis=1)
    out['exit_signal'] = out.valid & out.close.le(out.ma20) & out.close.lt(out.ma60)
    return out


def trend_intents_by_session(prices: pd.DataFrame, actions, calendar) -> dict:
    """为每只证券算出信号，折成 `{execution_session: {sid: intent}}`。

    一次性算全窗（与 runner 的逐日推进等价：信号只用截至 T 的收盘价，且是**比较型**——
    未来公司行动对整个回看窗施加同一因子而抵消，见 P0-4）。
    """
    out: dict = {}
    sessions = [pd.Timestamp(s).normalize() for s in calendar]
    index = {s: i for i, s in enumerate(sessions)}
    for sid, group in prices.groupby('security_id'):
        sid = str(sid)
        sig = exit_signals(group, actions, sessions[-1])
        sig = sig.assign(session_id=sid)
        for r in sig[sig.exit_signal].itertuples():
            session = pd.Timestamp(r.session).normalize()
            i = index.get(session)
            if i is None or i + 1 >= len(sessions):
                continue
            out.setdefault(str(sessions[i + 1].date()), {})[sid] = {
                'frozen_at': str(session.date()),
                'signal_session': str(session.date()),
                'execution_session': str(sessions[i + 1].date()),
                'close': float(r.close), 'ma20': float(r.ma20), 'ma60': float(r.ma60)}
    return out


__all__ = ['EXIT_REASON', 'MA_FAST', 'MA_SLOW', 'POLICY_ID', 'exit_signals',
           'trend_intents_by_session']
