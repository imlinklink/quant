"""程序计算的技术证据包（规划 §6.2）。

**为什么需要它**：现有持仓评审的充分性判据建立在「有没有新闻证据」上 —— 而本项目的
刻意非目标是**不接公司事件源**，市场日报又是唯一的证据供给。两者相乘的结果是
「没有新闻 ⇒ `LLM_INSUFFICIENT` ⇒ 模型永不被调用 ⇒ L 恒等于 R」。
§6.2 的解法不是伪造公司事件去绕过质量门，而是**给持仓角色一条自己的充分性标准**：
程序把趋势位置、回撤、ATR、量能、持有期、浮盈/R、保护距离、风险状态、现金暴露算好，
每一项带单位、算法版本、as_of、available_at、来源与缺失原因。

三条纪律：

1. **模型不计算**（§6.2）：股数、收益、ATR、距离全部由程序给出，模型只做判断。
2. **每项都可能是缺失的**，缺失本身就是信息：缺失原因写在该项里，并被充分性标准消费；
   缺必需项 ⇒ `LLM_INSUFFICIENT`（不调用模型、零成本），而不是拿 0 冒充「没有风险」。
3. **技术事实可被引用**：每项渲染成一条**可逐字引用**的 `evidence_id`（`technical:<name>`），
   归属就是本持仓证券，`published_at = available_at = 该值可见的时刻`。
   这样模型的 `facts` 能引用技术事实 —— 否则 `validate_claims` 会要求每条 claim 都引用
   一个 evidence_id，而技术事实上不了桌就只剩原因码可说，判断变得不可解释。

序列一律用**复权**收盘价（与既有信号管线同一处实现 `_adjusted_bars`）：用原始价会在
NVDA/AMZN/GOOGL/AAPL 的拆股处把 MA200 打成假的趋势破坏。ATR 复用面板既有的 `asof_atr`
（与初始止损同一波动率口径），不新造第二套。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from scripts.live_trading.decision_ledger.event_store import stable_id
from scripts.medium_term.timed_entries import _adjusted_bars
from scripts.portfolio_shadow.schema import to_micro

TECHNICAL_SCHEMA_VERSION = 'technical-packet-v1'
SOURCE = 'program:technical_packet'

#: 每个量的算法版本，写进每一项。改算法必须改版本号（否则旧包与新包看起来一样）。
ALGORITHM_VERSIONS = {
    'price': 'adjusted-close-v1',
    'ma': 'sma-close-v1',
    'drawdown': 'rolling-high-252-v1',
    'atr': 'panel-asof-atr14-v1',
    'volume': 'volume-ratio-20-v1',
    'holding': 'completed-session-count-v1',
    'unrealized': 'net-of-entry-fees-v1',
    'protection': 'stop-distance-v1',
    'account': 'account-state-v1',
}

#: 做出一次持仓判断**必需**的量。缺任一 ⇒ 数据不足，不调用模型（§6.2）。
REQUIRED = ('close', 'atr14', 'atr14_frac', 'unrealized_r', 'stop_distance_frac',
            'stop_distance_atr', 'holding_sessions', 'risk_state', 'account_drawdown',
            'cash_share')
#: 有则更好、缺了不拦的量。
OPTIONAL = ('ma20', 'ma50', 'ma200', 'ma200_slope_20', 'high_252', 'drawdown_frac',
            'volume_ratio_20', 'unrealized_usd', 'stop_distance_usd', 'risk_budget_bp',
            'gross_exposure')

MA_WINDOWS = (20, 50, 200)
DRAWDOWN_LOOKBACK = 252
VOLUME_LOOKBACK = 20
TREND_SLOPE_LOOKBACK = 20


@dataclass(frozen=True)
class Fact:
    """一项技术事实。`missing_reason` 非空即表示**这项不可用**，value 必须为 None。"""
    name: str
    value: object
    unit: str
    algorithm_version: str
    as_of: str
    available_at: str
    source: str = SOURCE
    missing_reason: str | None = None

    def as_dict(self) -> dict:
        return {'name': self.name, 'value': self.value, 'unit': self.unit,
                'algorithm_version': self.algorithm_version, 'as_of': self.as_of,
                'available_at': self.available_at, 'source': self.source,
                'missing_reason': self.missing_reason}


def _fact(name, value, unit, version, session, *, missing=None) -> Fact:
    return Fact(name=name, value=(None if missing else value), unit=unit,
                algorithm_version=version, as_of=session, available_at=session,
                missing_reason=missing)


def _series(history: pd.DataFrame, actions, session) -> pd.DataFrame:
    """复权收盘序列 + 成交量。`as_of=session`：只用截至该日可见的行动做复权。"""
    group = history.sort_values('session').reset_index(drop=True)
    adj = _adjusted_bars(group, actions, pd.Timestamp(session)).sort_values(
        'session').reset_index(drop=True)
    return pd.DataFrame({'session': pd.to_datetime(adj.session).dt.normalize(),
                         'close': pd.to_numeric(adj.raw_close, errors='coerce'),
                         'volume': pd.to_numeric(group.volume, errors='coerce')})


def build_facts(*, security_id: str, history: pd.DataFrame, atr14_micro: int | None,
                position, account_facts: dict, session: str, mark_price_micro: int | None,
                actions=None) -> dict:
    """算出该持仓在 `session` 收盘的技术事实。返回 {name: Fact}。

    `account_facts` 由账户侧给出（风险状态、回撤、现金占比…）—— 账户级量不该由本模块
    再算一遍，那是第二份定义。缺键即该项缺失，如实标注。
    """
    session = str(pd.Timestamp(session).date())
    facts: dict = {}
    series = _series(history, actions, session)
    rows = series[series.session.le(pd.Timestamp(session))]
    close = float(rows.close.iloc[-1]) if len(rows) else None
    if mark_price_micro is not None:
        # 市价以账户侧给的为准（引擎的 mark），与序列末值不一致时不下场调和，只如实并列
        facts['mark_price'] = _fact('mark_price', mark_price_micro, 'micro_usd',
                                    ALGORITHM_VERSIONS['price'], session)

    facts['close'] = _fact('close', (None if close is None else to_micro(close)), 'micro_usd',
                           ALGORITHM_VERSIONS['price'], session,
                           missing=None if close is not None else 'NO_HISTORY')

    for window in MA_WINDOWS:
        if len(rows) >= window:
            facts[f'ma{window}'] = _fact(f'ma{window}', to_micro(float(rows.close.tail(window).mean())),
                                         'micro_usd', ALGORITHM_VERSIONS['ma'], session)
        else:
            facts[f'ma{window}'] = _fact(f'ma{window}', None, 'micro_usd',
                                         ALGORITHM_VERSIONS['ma'], session,
                                         missing=f'NEED_{window}_SESSIONS')
    if len(rows) > TREND_SLOPE_LOOKBACK + MA_WINDOWS[-1]:
        ma = rows.close.rolling(MA_WINDOWS[-1], min_periods=MA_WINDOWS[-1]).mean()
        facts['ma200_slope_20'] = _fact(
            'ma200_slope_20', to_micro(float(ma.iloc[-1] - ma.iloc[-1 - TREND_SLOPE_LOOKBACK])),
            'micro_usd', ALGORITHM_VERSIONS['ma'], session)
    else:
        facts['ma200_slope_20'] = _fact('ma200_slope_20', None, 'micro_usd',
                                        ALGORITHM_VERSIONS['ma'], session,
                                        missing='NEED_LONGER_HISTORY')

    if len(rows) >= DRAWDOWN_LOOKBACK:
        high = float(rows.close.tail(DRAWDOWN_LOOKBACK).max())
        facts['high_252'] = _fact('high_252', to_micro(high), 'micro_usd',
                                  ALGORITHM_VERSIONS['drawdown'], session)
        facts['drawdown_frac'] = _fact(
            'drawdown_frac', ((high - close) / close if close else None), 'fraction',
            ALGORITHM_VERSIONS['drawdown'], session,
            missing=None if close else 'NO_HISTORY')
    else:
        for name in ('high_252', 'drawdown_frac'):
            facts[name] = _fact(name, None, 'micro_usd', ALGORITHM_VERSIONS['drawdown'],
                                session, missing=f'NEED_{DRAWDOWN_LOOKBACK}_SESSIONS')

    if atr14_micro is not None and atr14_micro > 0 and close:
        facts['atr14'] = _fact('atr14', int(atr14_micro), 'micro_usd',
                               ALGORITHM_VERSIONS['atr'], session)
        facts['atr14_frac'] = _fact('atr14_frac', (atr14_micro / 1e6) / close, 'fraction',
                                    ALGORITHM_VERSIONS['atr'], session)
    else:
        reason = 'ATR_UNAVAILABLE' if atr14_micro is None else 'NO_HISTORY'
        for name in ('atr14', 'atr14_frac'):
            facts[name] = _fact(name, None, 'micro_usd', ALGORITHM_VERSIONS['atr'], session,
                                missing=reason)

    if len(rows) >= VOLUME_LOOKBACK + 1:
        avg = float(rows.volume.tail(VOLUME_LOOKBACK + 1).head(VOLUME_LOOKBACK).mean())
        last = float(rows.volume.iloc[-1])
        facts['volume_ratio_20'] = _fact(
            'volume_ratio_20', (last / avg if avg > 0 else None), 'ratio',
            ALGORITHM_VERSIONS['volume'], session,
            missing=None if avg > 0 else 'ZERO_AVERAGE_VOLUME')
    else:
        facts['volume_ratio_20'] = _fact('volume_ratio_20', None, 'ratio',
                                         ALGORITHM_VERSIONS['volume'], session,
                                         missing=f'NEED_{VOLUME_LOOKBACK + 1}_SESSIONS')

    # ---- 持仓自身 ----
    holding = getattr(position, 'holding_sessions', None)
    facts['holding_sessions'] = _fact(
        'holding_sessions', holding, 'count', ALGORITHM_VERSIONS['holding'], session,
        missing=None if holding is not None else 'POSITION_FIELD_MISSING')
    entry = getattr(position, 'entry_price_micro', None)
    shares = getattr(position, 'shares', None)
    risk = getattr(position, 'initial_risk_micro', 0) or 0
    mark = mark_price_micro
    if None not in (entry, shares, mark):
        pnl = shares * (mark - entry)
        facts['unrealized_usd'] = _fact('unrealized_usd', pnl, 'micro_usd',
                                        ALGORITHM_VERSIONS['unrealized'], session)
        facts['unrealized_r'] = _fact(
            'unrealized_r', (pnl / risk if risk > 0 else None), 'R',
            ALGORITHM_VERSIONS['unrealized'], session,
            missing=None if risk > 0 else 'INITIAL_RISK_MISSING')
    else:
        for name in ('unrealized_usd', 'unrealized_r'):
            facts[name] = _fact(name, None, 'micro_usd', ALGORITHM_VERSIONS['unrealized'],
                                session, missing='POSITION_FIELD_MISSING')

    stop = getattr(position, 'stop_micro', None)
    if None not in (stop, mark) and mark > 0:
        distance = mark - stop
        facts['stop_distance_usd'] = _fact('stop_distance_usd', distance, 'micro_usd',
                                           ALGORITHM_VERSIONS['protection'], session)
        facts['stop_distance_frac'] = _fact('stop_distance_frac', distance / mark, 'fraction',
                                            ALGORITHM_VERSIONS['protection'], session)
        atr = facts['atr14'].value
        facts['stop_distance_atr'] = _fact(
            'stop_distance_atr', (distance / atr if atr else None), 'ATR',
            ALGORITHM_VERSIONS['protection'], session,
            missing=None if atr else 'ATR_UNAVAILABLE')
    else:
        for name in ('stop_distance_usd', 'stop_distance_frac', 'stop_distance_atr'):
            facts[name] = _fact(name, None, 'micro_usd', ALGORITHM_VERSIONS['protection'],
                                session, missing='POSITION_FIELD_MISSING')

    # ---- 账户级（由账户侧给，缺键即缺失）----
    for name, unit in (('risk_state', 'enum'), ('account_drawdown', 'fraction'),
                       ('risk_budget_bp', 'bp'), ('cash_share', 'fraction'),
                       ('gross_exposure', 'fraction')):
        value = account_facts.get(name)
        facts[name] = _fact(name, value, unit, ALGORITHM_VERSIONS['account'], session,
                            missing=None if value is not None else 'ACCOUNT_FACT_MISSING')
    return facts


def sufficiency(facts: dict) -> dict:
    """**独立、显式**的充分性标准（§6.2）：必需项是否齐备，与技术/新闻无关。

    与新闻质量门是**两件事**：这里判的是「程序能不能把这个持仓的状态说清楚」；
    新闻门判的是「有没有可引用的公司事件」。本轮持仓角色以本判据为准。
    """
    required_missing = [n for n in REQUIRED
                        if n not in facts or facts[n].missing_reason]
    optional_missing = [n for n in OPTIONAL if n in facts and facts[n].missing_reason]
    return {'schema_version': TECHNICAL_SCHEMA_VERSION,
            'level': 'INSUFFICIENT' if required_missing else 'OK',
            'required': list(REQUIRED), 'optional': list(OPTIONAL),
            'required_missing': required_missing, 'optional_missing': optional_missing}


def _render(fact: Fact) -> str:
    """渲染成**可逐字引用**的一行。`validate_claims` 对 `fact` 要求全等，所以这里要短、
    要确定、不含随运行变化的东西（时间戳、随机数）。"""
    value = fact.value
    if isinstance(value, float):
        value = f'{value:.4f}'
    return f'{fact.name}={value} {fact.unit}'


def to_evidence_items(facts: dict, *, security_id: str, as_of: str) -> list:
    """把技术事实变成 `new_evidence` 形状的可引用条目。

    归属就是本持仓证券（不是 MARKET）—— 否则一次基于技术状态的退出会因为「只引用了市场级
    证据」而无法解释。缺失项**不生成**条目：引用一个不存在的值没有意义，缺口走
    `missing_information` 如实报出。
    """
    items = []
    for name in sorted(facts):
        fact = facts[name]
        if fact.missing_reason:
            continue
        items.append({
            'evidence_id': stable_id('technical', security_id, name, fact.algorithm_version,
                                     str(fact.value), fact.as_of),
            'subject_code': security_id,
            'kind': 'technical',
            'source': fact.source,
            'source_url': '',
            'title': f'{name} ({fact.algorithm_version})',
            'summary': _render(fact),
            'cluster_id': f'technical:{security_id}:{fact.as_of}',
            'content_hash': None,
            'published_at': fact.available_at, 'observed_at': fact.available_at,
            'effective_at': None, 'expires_at': None,
            'quality': 'ok', 'quality_reasons': [],
        })
    return items


__all__ = ['ALGORITHM_VERSIONS', 'OPTIONAL', 'REQUIRED', 'TECHNICAL_SCHEMA_VERSION',
           'Fact', 'build_facts', 'sufficiency', 'to_evidence_items']
