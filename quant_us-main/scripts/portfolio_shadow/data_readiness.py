"""每日运行前的数据就绪门：T 必须是「规则历里收盘已过的最新 session」。

为什么需要它（2026-09-18 事故）
------------------------------
`cmd_run_daily` 原先直接拿「个股面板的最新日期」当目标 session T，**从不检查那一天是不是
交易所最近一个已收盘的会话**。面板落后时它就用几天前的 T 跑今天的活：机会按早已过去的
截止时间冻结、评审必然 `DECISION_WINDOW_MISSED`。而退出码、日志、「作业成功」全都正常。

当天查出的真正根因不在调度，而在 `refresh_data.py` 选表拿到空 codes —— 个股日线**根本没
被请求过**（上游其实有数据）。所以本门的分工是**发现并区分**形态，不是修复某一种：

    READY              两个源都到齐，且等于规则历给出的应处理会话
    WAITING_FOR_DATA   两个源一致地落后，且**最近确实请求过**（上游还没发布）—— 等待
    PARTIAL_DATA       源之间不一致（一个前进、一个没动）—— 必报错
    STALE_REQUEST      一致地落后，但有源很久没有成功请求 —— 没人真的在请求
    AHEAD_OF_CALENDAR  数据比规则历给出的应处理会话还新（历法或数据有出入）—— 必报错
    NO_CALENDAR        规则历给不出应处理会话 —— 必报错

**为什么不能只判「数据够不够新」**：`PARTIAL_DATA`（个股没动、ETF 动了）与
`STALE_REQUEST`（谁都没动、请求也没发出去）在「数据新不新」这一个维度上都表现为「不够
新」，只按它判就会双双归成 `WAITING_FOR_DATA` —— 一个「上游还没出」的结论，而真相是本地
把请求变成了空操作。所以本门同时看两件事：**数据到哪一天**，以及**最后一次成功请求是什么
时候**。后者取自分区文件里的 `downloaded_at` 列（取数时写进数据本身），不取任何自述。

已知不精确：半日市（13:00 ET 收盘）未建模 —— 规则历的 `close_time_et` 恒为 '16:00'，半日市
当天本门会晚约 3 小时才认为收盘，表现为 `WAITING_FOR_DATA`（不会误判成错误）。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

READY = 'READY'
WAITING_FOR_DATA = 'WAITING_FOR_DATA'
PARTIAL_DATA = 'PARTIAL_DATA'
STALE_REQUEST = 'STALE_REQUEST'
AHEAD_OF_CALENDAR = 'AHEAD_OF_CALENDAR'
NO_CALENDAR = 'NO_CALENDAR'

#: 拦住今天全部前向工作的状态（`WAITING_FOR_DATA` 只等，不算故障）
BLOCKING = frozenset({PARTIAL_DATA, STALE_REQUEST, AHEAD_OF_CALENDAR, NO_CALENDAR})

#: 超过这么久没有成功取数，就认为「没人在请求」而不是「上游还没发布」。
#: 定时任务一天跑三次，所以这个阈值只要大于相邻两次的间隔、且明显小于一个交易日即可。
DEFAULT_STALE_AFTER_HOURS = 12.0

#: 判据里用到的源（名字 → 说明），仅供报告用
SOURCE_LABELS = {'tech': '个股面板（TECH raw）', 'etf': 'ETF 快照（交易日历来源）'}


def _utc(value=None) -> pd.Timestamp:
    stamp = pd.Timestamp.now(tz='UTC') if value is None else pd.Timestamp(value)
    return (stamp.tz_localize('UTC') if stamp.tzinfo is None else stamp).tz_convert('UTC')


def expected_session(now=None) -> str | None:
    """规则历里**收盘时刻已过**的最新 session —— 即此刻应该处理的 T。

    取「收盘已过」而不是「日历上的最后一天」：`_forward_calendar` 里规则历覆盖未来（它
    必须如此才能定出 T+1），拿它的末日当 T 会把还没发生的一天当成已完成。
    """
    from scripts.data.trading_calendar import sessions as rule_sessions
    from scripts.evidence.evidence_store import market_close

    now_dt = _utc(now)
    frame = rule_sessions(now_dt - pd.Timedelta(days=30), now_dt)
    # 规则历给的是**带 UTC 时区**的零点，而 `market_close` 要的是能被 `tz_localize(NY)`
    # 的裸日期（tz-aware 会直接报 TypeError）。这里退回纯日期，避免把时区带进收盘时刻的计算。
    dates = [pd.Timestamp(d).date() for d in frame['session_date']]
    passed = [d for d in dates if market_close(d) <= now_dt]
    return str(max(passed)) if passed else None


def source_state(*, paths) -> dict:
    """从一个源的**分区文件本身**读出 (最新 session, 最后一次成功取数时刻)。

    不用检查点的计数冒充「这次取到了没有」：`download_market_history` 打印的 `completed`
    是检查点里**累计的分区数**（`len(state['completed'])`），不管本次实际取了几个 —— 拿它
    判「请求有没有生效」会得出与事实相反的结论。`downloaded_at` 是取数时写进数据行的，
    数据到哪一天、什么时候取的，都在数据里。
    """
    newest_session, newest_fetch = None, None
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue
        frame = pd.read_csv(path, usecols=['time_key', 'downloaded_at'])
        if frame.empty:
            continue
        session = pd.to_datetime(frame['time_key'], errors='coerce').max()
        if pd.notna(session) and (newest_session is None or session > newest_session):
            newest_session = session
        # 逐元素解析：同一文件里的行来自不同批次的取数，格式可能混用（带/不带微秒），
        # 列级 to_datetime 会把少数派整列判成 NaT —— 静默丢数据（见 evidence_store.to_utc_series）。
        # **先取唯一值再解析**：`downloaded_at` 一批取数一个值，13 只 × 12 年的分区里行数上万、
        # 唯一值只有几十个，逐元素解析整列要 ~58 秒（实测），解析唯一值则不到 0.1 秒。
        from scripts.evidence.evidence_store import to_utc_series
        fetched = to_utc_series(pd.Series(frame['downloaded_at'].dropna().unique()))
        if fetched.notna().any():
            latest = fetched.max()
            if newest_fetch is None or latest > newest_fetch:
                newest_fetch = latest
    return {
        'session': str(newest_session.date()) if newest_session is not None else None,
        'fetched_at': newest_fetch.isoformat() if newest_fetch is not None else None,
    }


def production_sources() -> dict:
    """生产环境的两个源：个股面板（TECH raw）与 ETF 快照（交易日历来源）。

    两个 root 由 `refresh_data` 定义（ETF 有自己的 root 与检查点，用错会触发哈希守卫），
    这里直接复用，避免在第二处重复「哪个源在哪个 root」这个知识。
    """
    from .refresh_data import ETF_ROOT, RAW_ROOT, tech_master_codes
    tech = [p for code in tech_master_codes()
            for p in RAW_ROOT.glob(f'day/none/year=*/{code.replace(".", "_")}.csv.gz')]
    etf = sorted((ETF_ROOT / 'market_history').glob('day/none/year=*/US_*.csv.gz'))
    return {'tech': source_state(paths=tech), 'etf': source_state(paths=etf)}


def assess(*, sources: dict, expected: str | None, now=None,
           stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS) -> dict:
    """纯函数：由「应处理会话」与各源的 (session, fetched_at) 判定状态。

    `sources` 形如 `{'tech': {'session': '2026-09-17', 'fetched_at': '...'}, 'etf': {...}}`。
    """
    now_dt = _utc(now)
    verdict = {
        'state': None, 'blocking': False, 'expected_session': expected,
        'sources': sources, 'stale_sources': [], 'reason': '',
    }
    if expected is None:
        verdict.update(state=NO_CALENDAR, blocking=True,
                       reason='规则历给不出应处理会话（历法不可用或范围不足）')
        return verdict

    sessions = {name: (s or {}).get('session') for name, s in sources.items()}
    if any(v is None for v in sessions.values()):
        missing = sorted(n for n, v in sessions.items() if v is None)
        verdict.update(state=PARTIAL_DATA, blocking=True,
                       reason=f'{missing} 读不到任何行情（源缺失或为空）')
        return verdict

    distinct = set(sessions.values())
    if len(distinct) > 1:
        detail = '、'.join(f'{SOURCE_LABELS.get(n, n)}={v}' for n, v in sorted(sessions.items()))
        verdict.update(state=PARTIAL_DATA, blocking=True,
                       reason=f'各源到齐的会话不一致（{detail}）—— 有一个源没有前进')
        return verdict

    session = distinct.pop()
    if session > expected:
        verdict.update(state=AHEAD_OF_CALENDAR, blocking=True,
                       reason=f'数据到 {session} 比规则历给出的应处理会话 {expected} 还新')
        return verdict
    if session == expected:
        verdict.update(state=READY, reason=f'各源一致到齐 {session} == 应处理会话')
        return verdict

    # 一致地落后：区分「上游还没发布」与「没人在请求」
    stale = []
    for name, src in sorted(sources.items()):
        fetched = (src or {}).get('fetched_at')
        if fetched is None:
            stale.append(name)
            continue
        age_h = (now_dt - _utc(fetched)).total_seconds() / 3600.0
        if age_h > stale_after_hours:
            stale.append(name)
    if stale:
        verdict.update(
            state=STALE_REQUEST, blocking=True, stale_sources=stale,
            reason=(f'数据停在 {session}（应到 {expected}），且 '
                    f'{"、".join(SOURCE_LABELS.get(n, n) for n in stale)} 超过 '
                    f'{stale_after_hours:g} 小时没有成功取数 —— 不是「上游没发布」，'
                    f'而是请求没有落到源上'))
        return verdict

    verdict.update(state=WAITING_FOR_DATA, blocking=False, stale_sources=[],
                   reason=(f'数据停在 {session}（应到 {expected}），但两个源最近都成功取过数 '
                           f'—— 上游尚未发布，等待下一次运行'))
    return verdict


def gate(*, now=None, sources=None, stale_after_hours=DEFAULT_STALE_AFTER_HOURS) -> dict:
    """生产入口：读两个源 + 规则历，给出就绪判定。"""
    return assess(sources=sources if sources is not None else production_sources(),
                  expected=expected_session(now), now=now,
                  stale_after_hours=stale_after_hours)
