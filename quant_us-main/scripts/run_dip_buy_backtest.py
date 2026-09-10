#!/usr/bin/env python3
"""dip_buy（抄底）策略回测：逐 15 分钟 bar 重放现网评分与出场。

为了能和 donchian 做组合对比，本脚本尽量复用**现网同一份代码**：
  - 入场：`mutifactor.utils.intraday_scoring.analyze_score`（与实盘同一函数）；
  - 出场：`mutifactor.strategies.dual_chandelier.PositionExitState`（与实盘同一引擎，
    参数取 config.chandelier.profiles.dip_buy）；ATR 按 **60 分钟** bar 计算
    （与实盘 atr_cache 的 KLType.K_60M 一致，见 atr_for_bars）。

无前视保证：
  - 评分只喂「当前 bar 及其之前」的窗口（与实盘完成的 rolling 窗口一致）；
  - 入场价用**下一根 bar 的开盘**（信号在当根收盘后才成立）。

**重要限制**：富途 15M 单次上限 **1000 根**、单只股票累计上限 **60000 根**，且窗口从请求的
`start` 起算（可平移）。因此覆盖长周期必须**分多段跑再合并**，例如：

    # 第 1 段：覆盖 2021–2024（含坏年份）
    python scripts/run_dip_buy_backtest.py --start 2021-01-01
    # 第 2 段：覆盖 2024–至今，--append 并入上一段
    python scripts/run_dip_buy_backtest.py --start 2024-04-01 --append \
      --combine-donchian backtests/donchian_sensitivity_trades.csv

未复现的门（与实盘差异，需在结论里注明）：
  index_gate / reversal_gate / rr_gate / earnings_gate / daily_trend_gate / LLM 复核。
  已有：时段过滤、冷却、单股单仓、最大持仓数。

用法：
    python scripts/run_dip_buy_backtest.py --probe          # 只探测数据覆盖
    python scripts/run_dip_buy_backtest.py --probe --codes US.MU --start 2021-01-01
    python scripts/run_dip_buy_backtest.py --start 2024-01-01
"""
import argparse
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from mutifactor.utils.intraday_scoring import analyze_score  # noqa: E402
from mutifactor.strategies.dual_chandelier import PositionExitState  # noqa: E402
from scripts.analyze_backtest_portfolio import (  # noqa: E402
    bucket_metrics, max_drawdown, stability_summary,
)
from scripts.run_donchian_backtest import load_config  # noqa: E402

ET = ZoneInfo('America/New_York')
POSITION_USD = 5000.0
COST_ROUND_TRIP_PCT = 0.20
WINDOW_BARS = 60          # 与实盘 rolling 窗口一致（max(min_bars, 60)）
ATR_PERIOD = 14
MIN_BUCKET_TRADES = 5


def atr_for_bars(df: pd.DataFrame, period: int = ATR_PERIOD):
    """按**60 分钟** bar 计算 ATR(period)，再对齐到每根 15M bar。

    实盘 atr_cache 用的是 60M bar（KLType.K_60M，绝对价格单位），不是 15M；
    直接用 15M 算 ATR 会偏离实盘的吊灯行为。这里从 15M 重采样出 60M。

    无前视：某 15M bar 只能用**已收盘**的 60M 桶（桶起点+60min <= 该 bar 时间）。
    """
    d = df.set_index('time_key')
    o = d.resample('60min', label='left', closed='left').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'})
    o = o.dropna(subset=['close'])
    if o.empty:
        return np.full(len(df), np.nan)
    tr = pd.concat([o['high'] - o['low'],
                    (o['high'] - o['close'].shift(1)).abs(),
                    (o['low'] - o['close'].shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    atr.index = atr.index + pd.Timedelta(minutes=60)   # 索引改为「该桶可用的时刻」
    idx = pd.DatetimeIndex(df['time_key'])
    return atr.reindex(atr.index.union(idx)).ffill().reindex(idx).values


# ---------- 数据 ----------

PAGE_LIMIT = 1000          # 富途 request_history_kline 单次返回上限


def _futu_page_fetcher(ctx, code, page_delay=0.0):
    """返回一个 page_fetcher(start, end) -> DataFrame|None 的闭包（复用同一连接）。

    **API 错误必须抛出**，不能返回 None：None 表示「确实没有更多数据」，
    而限流/网络错误若也返回 None，会被上层当成正常结束，导致静默截断。
    """
    from futu import KLType, RET_OK, Session

    def _fetch(start, end):
        if page_delay:
            time.sleep(page_delay)
        ret, data, msg = ctx.request_history_kline(
            code, start=start, end=end, ktype=KLType.K_15M,
            autype='qfq', extended_time=True, session=Session.ALL)
        if ret != RET_OK:
            raise RuntimeError(f'{code} 拉取失败（{start}~{end}）: {msg}')
        if data is None or len(data) == 0:
            return None
        d = data.copy()
        d['time_key'] = pd.to_datetime(d['time_key'])
        return d.sort_values('time_key').drop_duplicates('time_key').reset_index(drop=True)

    return _fetch


def fetch_15m_range(page_fetch, start, end, max_pages=400, retries=4,
                    backoff=2.0, page_delay=0.0):
    """**分页**拉取 15M，返回 (DataFrame|None, info)。

    富途单次最多 1000 根、累计上限约 60000 根，且窗口从 start 起算；覆盖长周期必须
    分页前进。info 含 truncated（是否因错误/上限提前结束）、last_error、pages。

    限流是常见故障：页间加 page_delay，失败按退避重试；重试仍失败则**标记 truncated**
    并停止，绝不再把它当成「数据结束」静默收尾。
    """
    frames = []
    cursor = start
    truncated = False
    last_error = None
    for _ in range(max_pages):
        d, err = None, None
        for attempt in range(retries):
            try:
                d = page_fetch(cursor, end)
                err = None
                break
            except Exception as exc:                       # API 错误 → 重试
                err = str(exc)
                if attempt < retries - 1:
                    time.sleep(backoff * (2 ** attempt))
        if err:
            truncated, last_error = True, err
            break
        if d is None or d.empty:
            break                                          # 正常结束：没有更多数据
        frames.append(d)
        if len(d) < PAGE_LIMIT:
            break
        last = pd.Timestamp(d['time_key'].max())
        nxt = (last + pd.Timedelta(days=1)).strftime('%Y-%m-%d')
        if nxt <= cursor or nxt > end:
            break
        if page_delay:
            time.sleep(page_delay)
        cursor = nxt
    info = {'pages': len(frames), 'truncated': truncated, 'last_error': last_error}
    if not frames:
        return None, info
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values('time_key').drop_duplicates('time_key').reset_index(drop=True)
    lo = pd.Timestamp(start, tz=out['time_key'].dt.tz)
    hi = pd.Timestamp(end, tz=out['time_key'].dt.tz) + pd.Timedelta(days=1)
    return out[(out['time_key'] >= lo) & (out['time_key'] < hi)].reset_index(drop=True), info


def fetch_15m(codes, start, end, page_delay=0.0):
    """拉 15 分钟 K 线（前复权、含盘前盘后夜盘），与实盘设置一致。内部自动分页。

    返回 (data, coverage)：coverage 记录每只票的实际区间、bar 数、是否被截断。
    有截断时会在控制台明确告警——残缺数据会污染后续所有结论。
    """
    from futu import OpenQuoteContext
    cfg = load_config()
    host = cfg.get('futu', {}).get('host', '127.0.0.1')
    port = int(cfg.get('futu', {}).get('port', 11111))
    ctx = OpenQuoteContext(host=host, port=port)
    out, coverage = {}, {}
    try:
        for code in codes:
            df, info = fetch_15m_range(_futu_page_fetcher(ctx, code, page_delay),
                                       start, end, page_delay=page_delay)
            if df is None or df.empty:
                print(f'  ⚠️ {code}: 无数据'
                      + (f'（错误: {info["last_error"]}）' if info.get('last_error') else ''))
                coverage[code] = {'start': None, 'end': None, 'bars': 0,
                                  'truncated': info.get('truncated', False)}
                continue
            cov = {'start': str(df['time_key'].min().date()),
                   'end': str(df['time_key'].max().date()),
                   'bars': len(df), 'truncated': info.get('truncated', False),
                   'pages': info.get('pages')}
            coverage[code] = cov
            flag = '  ⚠️ 被截断（数据不完整）' if cov['truncated'] else ''
            print(f'  {code}: {cov["start"]} ~ {cov["end"]}（{cov["bars"]} 根）{flag}')
            out[code] = df
    finally:
        ctx.close()
    bad = [c for c, v in coverage.items() if v.get('truncated')]
    if bad:
        print(f'\n⚠️  以下股票的 15M 数据被截断，结果不可用于结论: {", ".join(bad)}')
        print('   多为富途限流所致。请加大 --page-delay（如 0.5）后重跑，'
              '或减少 --codes 分批拉取。\n')
    return out, coverage


def probe(data, coverage=None):
    """报告每只股票实际拿到的区间与 bar 数，并标出被截断的。"""
    coverage = coverage or {}
    lines = ['# dip_buy 15分钟数据覆盖探测\n']
    lines.append('| 股票 | 起始 | 结束 | bar数 | 状态 |')
    lines.append('|---|---|---|---|---|')
    codes = sorted(set(data.keys()) | set(coverage.keys()))
    for code in codes:
        df = data.get(code)
        cov = coverage.get(code) or {}
        if df is None or df.empty:
            note = cov.get('last_error') or '无数据'
            lines.append(f'| {code} | — | — | 0 | ⚠️ {note} |')
            continue
        status = '⚠️ 被截断' if cov.get('truncated') else 'OK'
        lines.append(f"| {code} | {df['time_key'].min().date()} | "
                     f"{df['time_key'].max().date()} | {len(df)} | {status} |")
    truncated = [c for c, v in coverage.items() if v.get('truncated')]
    if truncated:
        lines.append(f'\n> ⚠️ **{len(truncated)} 只股票的数据被截断**'
                     f'（{", ".join(truncated)}），结果不可用于结论。'
                     f'多为富途限流，请加大 `--page-delay` 或减少 `--codes` 分批拉取。\n')
    return '\n'.join(lines) + '\n'


# ---------- 时段过滤 ----------

def session_allowed(ts, cfg):
    """按时段过滤（用美东时间）；对应 config.dip_buy.session_filter。"""
    sf = (cfg.get('dip_buy', {}) or {}).get('session_filter', {}) or {}
    if not sf.get('enabled', True):
        return True
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize(ET)
    else:
        ts = ts.tz_convert(ET)
    m = ts.hour * 60 + ts.minute
    regular = 9 * 60 + 30 <= m < 16 * 60
    pre = 4 * 60 <= m < 9 * 60 + 30
    after = 16 * 60 <= m < 20 * 60
    if regular and sf.get('allow_regular', True):
        return True
    if pre and sf.get('allow_pre_market', False):
        return True
    if after and sf.get('allow_after_hours', False):
        return True
    return False


# ---------- 回测 ----------

def _exit_kwargs(cfg):
    """从 config 取 dip_buy 出场参数，交给现网同一个 PositionExitState。"""
    prof = ((cfg.get('chandelier', {}) or {}).get('profiles', {}) or {}).get('dip_buy', {})
    ch = cfg.get('chandelier', {}) or {}
    return {
        'fixed_stop_pct': float(prof.get('fixed_stop_pct', ch.get('fixed_stop_pct', 0.05))),
        'breakeven_pct': float(prof.get('breakeven_pct', ch.get('breakeven_pct', 0.03))),
        'trailing_activate_pct': float(prof.get('trailing_activate_pct',
                                                ch.get('trailing_activate_pct', 0.05))),
        'trailing_pullback_pct': float(prof.get('trailing_pullback_pct',
                                                ch.get('trailing_pullback_pct', 0.03))),
        'atr_threshold_pct': float(prof.get('atr_threshold_pct', ch.get('atr_threshold_pct', 0.0))),
        'atr_trailing_mult': float(prof.get('atr_trailing_mult', ch.get('atr_trailing_mult', 2.0))),
        'trailing_enabled': bool(prof.get('trailing_enabled', ch.get('trailing_enabled', True))),
    }


def replay_stock(code, df, cfg, threshold, time_exit_bars, entry_gate=None):
    """逐 bar 重放一只股票。返回交易列表。

    评分窗口与实盘一致：只喂当前 bar 及其之前最多 WINDOW_BARS 根；
    入场用下一根 bar 开盘（信号当根收盘才成立）。

    entry_gate: 可选回调 (bar_time) -> bool，在信号 bar 处判定是否允许入场
    （用于日线闸门等外层过滤）。默认 None = 不限制。
    """
    n = len(df)
    if n < WINDOW_BARS:
        return []
    exit_kw = _exit_kwargs(cfg)
    atr_vals = atr_for_bars(df)
    trades = []
    i = WINDOW_BARS - 1
    while i < n - 1:
        bar = df.iloc[i]
        if not session_allowed(bar['time_key'], cfg):
            i += 1
            continue
        if entry_gate is not None and not entry_gate(bar['time_key']):
            i += 1
            continue
        window = df.iloc[max(0, i - WINDOW_BARS + 1):i + 1]
        if len(window) < 30:
            i += 1
            continue
        try:
            res = analyze_score(window, float(bar['close']), threshold)
        except Exception:
            i += 1
            continue
        if res.get('signal') != 'buy' or int(res.get('score', 0)) < threshold:
            i += 1
            continue

        # 次日（下一根 bar）开盘入场
        entry_idx = i + 1
        entry_px = float(df.iloc[entry_idx]['open'])
        if not np.isfinite(entry_px) or entry_px <= 0:
            i += 1
            continue
        entry_time = df.iloc[entry_idx]['time_key']

        state = PositionExitState(entry_px, 'long', **exit_kw)
        exit_px, exit_reason, exit_time, j = None, None, None, entry_idx
        mfe_pct, mae_pct = 0.0, 0.0
        reached_breakeven = reached_trailing = reached_atr = False
        while j < n:
            b = df.iloc[j]
            atr = atr_vals[j]
            mfe_pct = max(mfe_pct, (float(b['high']) / entry_px - 1.0) * 100.0)
            mae_pct = min(mae_pct, (float(b['low']) / entry_px - 1.0) * 100.0)
            # 先用上一根已生效的保护线检查当前 bar。不能先用本 bar 收盘更新止损，
            # 再回头拿同一 bar 的历史 low 检查，否则引入 bar 内前视。
            should, reason, stop_px = state.check_exit(float(b['low']))
            if should:
                # 跳空：开盘已在止损之外则按开盘成交
                exit_px = min(float(b['open']), float(stop_px)) if stop_px else float(b['open'])
                exit_reason, exit_time = reason, b['time_key']
                break
            if np.isfinite(atr) and atr > 0:
                state.recompute(float(atr), float(b['close']))
                reached_breakeven = reached_breakeven or state._breakeven_moved
                reached_trailing = reached_trailing or state._trailing_activated
                reached_atr = reached_atr or state._atr_mode_active
            if j - entry_idx + 1 >= time_exit_bars:
                exit_px, exit_reason, exit_time = float(b['close']), 'TIME_EXIT', b['time_key']
                break
            j += 1
        if exit_px is None:
            last = df.iloc[-1]
            exit_px, exit_reason, exit_time = float(last['close']), '期末未平仓', last['time_key']

        gross_pct = (exit_px / entry_px - 1.0) * 100.0
        net_pct = gross_pct - COST_ROUND_TRIP_PCT
        trades.append({
            'stock': code, 'variant': 'dip_buy', 'variant_label': 'dip_buy 抄底',
            'entry_date': str(entry_time), 'exit_date': str(exit_time),
            'entry_price': round(entry_px, 4), 'exit_price': round(exit_px, 4),
            'reason': exit_reason, 'score': int(res.get('score', 0)),
            'mfe_pct': round(mfe_pct, 3), 'mae_pct': round(mae_pct, 3),
            'reached_breakeven': reached_breakeven,
            'reached_trailing': reached_trailing, 'reached_atr': reached_atr,
            'gross_pnl_pct': round(gross_pct, 3), 'net_pnl_pct': round(net_pct, 3),
            'net_pnl_usd': round(net_pct / 100.0 * POSITION_USD, 2),
            'open': exit_reason == '期末未平仓',
            'holding_days': (pd.Timestamp(exit_time) - pd.Timestamp(entry_time)).total_seconds() / 86400,
        })
        i = j + 1 if exit_reason != '期末未平仓' else n
    return trades


def run_backtest(data, cfg, codes=None):
    db = cfg.get('dip_buy', {}) or {}
    threshold = int(db.get('buy_threshold', 7))
    time_exit_bars = int(db.get('time_exit_bars', 8))
    rows = []
    for code, df in sorted(data.items()):
        if codes and code not in codes:
            continue
        print(f'  重放 {code} … {len(df)} 根 15M bar')
        rows.extend(replay_stock(code, df, cfg, threshold, time_exit_bars))
    return pd.DataFrame(rows)


# ---------- 组合 ----------

def combine_with_donchian(dip_trades, donchian_csv, default_cell=(55, 2.0),
                          min_overlap_months=6):
    """把 dip_buy 与 donchian（指定格）在**双方都有交易的月份**上对齐，算相关性与合并净值。

    注意：不能按月 resample 后直接 dropna —— 没有交易的月份会被 sum() 填成 0，
    这样算出来的相关性几乎必然接近 0，是零填充造成的假象，不是真互补。
    这里先各自取出「有交易的月份」再取交集；重叠不足则不给相关性。
    """
    path = Path(donchian_csv)
    if not path.exists():
        return {'error': f'找不到 {path}'}
    dc = pd.read_csv(path)
    if {'channel', 'atr_mult'}.issubset(dc.columns):
        dc = dc[(dc['channel'] == default_cell[0]) & (dc['atr_mult'] == default_cell[1])]
    dc = dc[~dc['open'].astype(bool)].copy()
    dip = dip_trades[~dip_trades['open'].astype(bool)].copy()
    if dc.empty or dip.empty:
        return {'error': '一方没有已平仓交易，无法对比'}
    dc['exit_date'] = pd.to_datetime(dc['exit_date'])
    dip['exit_date'] = pd.to_datetime(dip['exit_date'])

    a = dc.set_index('exit_date')['net_pnl_usd'].resample('MS').sum()
    b = dip.set_index('exit_date')['net_pnl_usd'].resample('MS').sum()
    a_active = a[a != 0].index
    b_active = b[b != 0].index
    overlap = a_active.intersection(b_active)

    base = {
        'months': len(overlap),
        'donchian_active_months': len(a_active),
        'dip_buy_active_months': len(b_active),
        'corr': None,
    }
    if len(overlap) < min_overlap_months:
        return dict(base, error=f'重叠月份不足（{len(overlap)} < {min_overlap_months}），'
                                f'无法给出可信相关性')

    joined = pd.DataFrame({'donchian': a[overlap], 'dip_buy': b[overlap]}).sort_index()
    combined = joined.sum(axis=1)
    return dict(
        base,
        corr=float(joined['donchian'].corr(joined['dip_buy'])),
        donchian_total=float(joined['donchian'].sum()),
        dip_buy_total=float(joined['dip_buy'].sum()),
        combined_total=float(combined.sum()),
        donchian_dd=max_drawdown(joined['donchian'].cumsum()),
        dip_buy_dd=max_drawdown(joined['dip_buy'].cumsum()),
        combined_dd=max_drawdown(combined.cumsum()),
        monthly=joined,
    )


def merge_trades(prev: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """合并两段回测的逐笔（去重）。

    富途 15M 单次上限 60000 根，且窗口从请求的 start 起算 → 覆盖长周期必须分多段跑、
    再合并。重叠区间内的同一笔交易按 (股票, 入场时间, 出场时间, 入场价) 去重。
    """
    frames = [f for f in (prev, new) if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    key = ['stock', 'entry_date', 'exit_date', 'entry_price']
    if all(k in out.columns for k in key):
        out = out.drop_duplicates(subset=key)
    return out.sort_values('entry_date').reset_index(drop=True)


# ---------- 报告 ----------

def build_report(trades, probe_md='', combo=None) -> str:
    lines = ['# dip_buy（抄底）策略回测\n']
    lines.append(f'- 生成时间: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append('- 入场: 现网 analyze_score；出场: 现网 PositionExitState（dip_buy 档）')
    lines.append('- 口径: 单笔 $5,000、往返成本 0.20%、未复现 index/reversal/rr/earnings/日线趋势门\n')
    if trades.empty:
        lines.append('> 没有产生任何交易。\n')
        return '\n'.join(lines) + '\n'

    # 逐笔里的日期是字符串，下游分桶需要 datetime
    trades = trades.copy()
    trades['entry_date'] = pd.to_datetime(trades['entry_date'])
    trades['exit_date'] = pd.to_datetime(trades['exit_date'])

    closed = trades[~trades['open'].astype(bool)].sort_values('exit_date')
    wins = closed[closed['net_pnl_usd'] > 0]
    losses = closed[closed['net_pnl_usd'] <= 0]
    gw, gl = float(wins['net_pnl_usd'].sum()), float(abs(losses['net_pnl_usd'].sum()))
    stab = stability_summary(bucket_metrics(trades, 'YS'), 'dip_buy',
                             min_trades=MIN_BUCKET_TRADES)

    def _n(v, f='{:,.0f}'):
        return '—' if v is None or (isinstance(v, float) and not np.isfinite(v)) else f.format(v)

    lines.append('## 1. 汇总\n')
    lines.append('| 笔数 | 胜率 | 期望/笔$ | 盈亏比 | 总收益$ | 最大回撤$ | 年度通过率 | 平均持仓(天) |')
    lines.append('|---|---|---|---|---|---|---|---|')
    lines.append(f"| {len(closed)} | {len(wins)/len(closed):.1%} | "
                 f"{_n(float(closed['net_pnl_usd'].mean()))} | "
                 f"{_n(gw/gl if gl else np.inf, '{:.2f}')} | "
                 f"{_n(float(closed['net_pnl_usd'].sum()))} | "
                 f"{_n(max_drawdown(closed['net_pnl_usd'].cumsum()))} | "
                 f"{_n(stab.get('pass_rate'), '{:.0%}')} | "
                 f"{closed['holding_days'].mean():.2f} |")
    lines.append('')

    lines.append('## 2. 分年期望/笔（$）\n')
    piv = bucket_metrics(trades, 'YS').pivot(index='period', columns='variant',
                                             values='expectancy')
    lines.append('| 年份 | 期望/笔$ | 笔数 |')
    lines.append('|---|---|---|')
    counts = bucket_metrics(trades, 'YS').set_index('period')['trades']
    for year, row in piv.iterrows():
        lines.append(f"| {year} | {_n(row.get('dip_buy'))} | {int(counts.get(year, 0))} |")
    lines.append('')

    lines.append('## 3. 出场原因分布\n')
    lines.append('| 原因 | 笔数 | 净盈亏$ | 均值$ |')
    lines.append('|---|---|---|---|')
    for reason, g in closed.groupby('reason'):
        lines.append(f"| {reason} | {len(g)} | {_n(float(g['net_pnl_usd'].sum()))} | "
                     f"{_n(float(g['net_pnl_usd'].mean()))} |")
    lines.append('')

    if combo:
        lines.append('## 4. 与 donchian 的组合效果\n')
        if combo.get('error'):
            lines.append(f"- **无法给出组合结论**：{combo['error']}")
            lines.append(f"- donchian 有交易月份 {combo.get('donchian_active_months', '—')}，"
                         f"dip_buy {combo.get('dip_buy_active_months', '—')}，"
                         f"重叠 {combo.get('months', '—')}。")
            lines.append('')
        else:
            lines.append(f"- 可比月份（双方都有交易）: {combo['months']}"
                         f"（donchian 有交易 {combo['donchian_active_months']} 个月，"
                         f"dip_buy {combo['dip_buy_active_months']} 个月）")
            lines.append(f"- 逐月盈亏相关性: **{combo['corr']:.2f}**")
            lines.append(f"- donchian 总收益 {_n(combo['donchian_total'])}，"
                         f"dip_buy {_n(combo['dip_buy_total'])}，"
                         f"合计 {_n(combo['combined_total'])}")
            lines.append(f"- 最大回撤：donchian {_n(combo['donchian_dd'])}，"
                         f"dip_buy {_n(combo['dip_buy_dd'])}，"
                         f"等权合计 {_n(combo['combined_dd'])}")
            lines.append('')
            lines.append('- 相关性越低（越接近 0 或为负），组合分散效果越好；'
                         '若合计回撤明显小于两者之和，说明确实互补。')
            lines.append('- 若可比月份很少，相关性本身噪声很大，不要据此下结论。')
            lines.append('')

    lines.append('## 5. 怎么读\n')
    lines.append('- dip_buy 是**日内**策略，持仓以 bar 计（平均持仓天数应远小于 donchian）。')
    lines.append('- 与 donchian 的组合价值看两个数：**逐月相关性**（越低越好）与'
                 '**合并最大回撤**（相对单跑是否下降）。')
    lines.append('- 因未复现实盘的若干过滤门，本回测的笔数会**多于**实盘，'
                 '应视为「入场信号族」的上界，而非实盘预期。')
    lines.append('- 若 15M 数据只覆盖近 1~2 年，则组合对比只在牛市段成立，'
                 '不能用来说明坏年份的分散效果。')
    if probe_md:
        lines.append('\n---\n')
        lines.append(probe_md)
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser(description='dip_buy（抄底）策略回测')
    ap.add_argument('--probe', action='store_true', help='只探测 15M 数据覆盖区间')
    ap.add_argument('--start', default='2024-01-01')
    ap.add_argument('--end', default=pd.Timestamp.now().strftime('%Y-%m-%d'))
    ap.add_argument('--codes', default=None, help='默认取 config dip_buy.watch_list')
    ap.add_argument('--combine-donchian', default=None,
                    help='donchian 逐笔 CSV，给出则追加组合分析')
    ap.add_argument('--append', action='store_true',
                    help='把本次结果并入已有逐笔 CSV（分段拉取时用，自动去重）')
    ap.add_argument('--page-delay', type=float, default=0.3,
                    help='翻页间隔秒数，避免富途限流（默认 0.3）')
    ap.add_argument('--output', default=str(PROJECT_ROOT / 'backtests' / 'dip_buy_backtest.md'))
    args = ap.parse_args()

    cfg = load_config()
    codes = ([c.strip() for c in args.codes.split(',') if c.strip()] if args.codes
             else list(cfg.get('dip_buy', {}).get('watch_list', [])))
    if not codes:
        print('未找到观察池代码'); return 2

    print(f'拉取 15M：{len(codes)} 只 {args.start}~{args.end}（翻页间隔 {args.page_delay}s）')
    data, coverage = fetch_15m(codes, args.start, args.end, page_delay=args.page_delay)
    if not data:
        print('没有拿到任何 15M 数据'); return 3
    pmd = probe(data, coverage)
    print(pmd)

    if args.probe:
        out = Path(args.output).with_name('dip_buy_probe.md')
        out.write_text(pmd, encoding='utf-8')
        print(f'已写出: {out}')
        return 0

    truncated = [c for c, v in coverage.items() if v.get('truncated')]
    trades = run_backtest(data, cfg)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    trades_out = out.with_name(out.stem + '_trades.csv')
    if args.append and trades_out.exists():
        prev = pd.read_csv(trades_out)
        merged = merge_trades(prev, trades)
        print(f'追加合并：已有 {len(prev)} 笔 + 本次 {len(trades)} 笔 → 去重后 {len(merged)} 笔')
        trades = merged
    # 数据被截断时不给组合结论——残缺样本上的相关性/回撤没有意义
    combo = None
    if args.combine_donchian:
        if truncated:
            combo = {'error': f'本次有 {len(truncated)} 只股票数据被截断'
                              f'（{", ".join(truncated)}），组合结论不可用'}
        else:
            combo = combine_with_donchian(trades, args.combine_donchian)
    report = build_report(trades, pmd, combo)
    out.write_text(report, encoding='utf-8')
    if not trades.empty:
        trades.to_csv(trades_out, index=False, encoding='utf-8')
    print(report)
    print(f'已写出: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
