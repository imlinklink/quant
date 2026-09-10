#!/usr/bin/env python3
"""买入链路四组对照实验：分钟择时到底有没有用？

对应设计文档的判断——`dip_buy` 的问题不是「15 分钟指标不够好」，而是
**观察周期与交易逻辑错位**。本脚本把「日线决定该不该买、15 分钟决定今天何时买」
这个假设变成可证伪的对照实验。

四组（§6）：

  A1 `legacy_15m`      原始 15 分钟 dip_buy（任意 15m 信号，日内出场）
  A2 `daily_gate_15m`  日线闸门 + 15 分钟入场，日内出场
  A3 `daily_only`      日线确认 → 次日开盘买入，**日线出场**
  A4 `daily_plus_15m`  日线确认 + 15 分钟择时入场，**日线出场**（与 A3 同出场）

**关键对照是 A3 vs A4**：同一批确认机会、同一套日线出场，只差入场方式。
必须用**匹配子集**比较——只在 A4 也找到 15 分钟信号的同一批机会上，
比较两者的成交价、MAE、止损率。否则样本不同，结论无意义。

判据（§6 末尾）：
  若 A4 相对 A3 的改善在**成交价 / MAE / 止损率**上不显著，则应删除分钟评分，
  直接 A3 次日开盘买入。分钟择时必须**自证**它改善了这三个量，而不是显得更精细。

用法（需 OpenD）：
    python scripts/run_hybrid_entry_study.py --codes US.MU --start 2021-01-01
    python scripts/run_hybrid_entry_study.py --daily-only-check   # 只跑 A3/A4（不需要 15M）
"""
import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from mutifactor.strategies.daily_regime import ACTIONABLE, annotate  # noqa: E402
from scripts.analyze_backtest_portfolio import max_drawdown  # noqa: E402
from scripts.run_dip_buy_backtest import (  # noqa: E402
    COST_ROUND_TRIP_PCT, POSITION_USD, WINDOW_BARS, _exit_kwargs, fetch_15m,
    replay_stock, session_allowed,
)
from scripts.run_donchian_backtest import fetch_daily_data, load_config  # noqa: E402

ET = ZoneInfo('America/New_York')

DAILY_ATR_MULT = 2.0      # 日线 ATR 吊灯倍数
DAILY_MAX_HOLD = 40       # 最长持有交易日（§3.2「10–40 个交易日」）
DAILY_MIN_HOLD = 1
ENTRY_SEARCH_SESSIONS = 2  # 确认后最多等这么多天找 15 分钟信号


# ---------- 日线确认事件 ----------

def confirmation_events(df_daily: pd.DataFrame) -> list:
    """返回日线确认事件的索引列表（state ∈ ACTIONABLE，且为**首次**进入）。

    只用当日及之前数据；入场在**次日**，因此不存在前视。
    """
    events, prev = [], None
    for i, row in df_daily.iterrows():
        st = row.get('state')
        if st in ACTIONABLE and prev not in ACTIONABLE:
            events.append(i)          # 首次进入可买状态
        prev = st
    return events


# ---------- 日线出场 ----------

def _num(v):
    """把可能缺失/非数值的列值安全转为 float；缺失返回 None。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def simulate_daily_exit(df: pd.DataFrame, entry_idx: int, entry_px: float,
                        atr_entry: float, mult: float = DAILY_ATR_MULT,
                        max_hold: int = DAILY_MAX_HOLD) -> dict:
    """日线出场：ATR 吊灯（只升不降）+ 更高低点保护，最长 max_hold 日。

    返回 exit_px / exit_reason / hold_days / mae_pct / mfe_pct / stop_hit。
    """
    n = len(df)
    _a = _num(atr_entry)
    stop = entry_px - mult * _a if (_a is not None and np.isfinite(_a)) else entry_px * 0.95
    highest = entry_px
    mae, mfe = 0.0, 0.0
    for j in range(entry_idx, min(n, entry_idx + max_hold + 1)):
        bar = df.iloc[j]
        low, high, close = float(bar['low']), float(bar['high']), float(bar['close'])
        mae = min(mae, low / entry_px - 1.0)
        mfe = max(mfe, high / entry_px - 1.0)
        # 止损：跳空按开盘成交
        if low <= stop:
            px = min(float(bar['open']), stop)
            return {'exit_px': px, 'exit_reason': 'stop', 'hold_days': j - entry_idx,
                    'mae_pct': mae, 'mfe_pct': mfe, 'stop_hit': True}
        # 吊灯上移 + 更高低点保护（只升不降）
        highest = max(highest, high)
        atr = _num(bar.get('atr'))
        if atr is not None and np.isfinite(atr):
            stop = max(stop, highest - mult * atr)
        ll20 = _num(bar.get('ll20'))
        if bool(bar.get('higher_low', False)) and ll20 is not None and np.isfinite(ll20):
            stop = max(stop, ll20)
        if j - entry_idx >= max_hold:
            return {'exit_px': close, 'exit_reason': 'max_hold', 'hold_days': j - entry_idx,
                    'mae_pct': mae, 'mfe_pct': mfe, 'stop_hit': False}
    last = df.iloc[min(n - 1, entry_idx + max_hold)]
    return {'exit_px': float(last['close']), 'exit_reason': 'end_of_data',
            'hold_days': min(n - 1, entry_idx + max_hold) - entry_idx,
            'mae_pct': mae, 'mfe_pct': mfe, 'stop_hit': False}


def _mk_trade(code, arm, entry_time, entry_px, exit_info, extra=None):
    gross = (exit_info['exit_px'] / entry_px - 1.0) * 100.0
    net = gross - COST_ROUND_TRIP_PCT
    t = {'stock': code, 'arm': arm, 'entry_date': str(entry_time),
         'entry_price': round(entry_px, 4), 'exit_price': round(exit_info['exit_px'], 4),
         'exit_reason': exit_info['exit_reason'], 'gross_pnl_pct': round(gross, 3),
         'net_pnl_pct': round(net, 3), 'net_pnl_usd': round(net / 100.0 * POSITION_USD, 2),
         'hold_days': exit_info['hold_days'], 'mae_pct': round(exit_info['mae_pct'], 4),
         'mfe_pct': round(exit_info['mfe_pct'], 4), 'stop_hit': bool(exit_info['stop_hit']),
         'open': exit_info['exit_reason'] == 'end_of_data'}
    if extra:
        t.update(extra)
    return t


# ---------- A3 / A4 ----------

def arm_daily_only(code: str, df: pd.DataFrame, events: list) -> list:
    """A3：日线确认 → 次日开盘买入 → 日线出场。"""
    trades = []
    for i in events:
        if i + 1 >= len(df):
            continue
        nxt = df.iloc[i + 1]
        entry_px = float(nxt['open'])
        atr = _num(df.iloc[i].get('atr'))      # 确认日 ATR（当时已知）
        if not np.isfinite(entry_px) or entry_px <= 0:
            continue
        info = simulate_daily_exit(df, i + 1, entry_px, atr)
        trades.append(_mk_trade(code, 'A3_daily_only', nxt['date'], entry_px, info,
                                {'confirm_date': str(df.iloc[i]['date']),
                                 'state': df.iloc[i]['state']}))
    return trades


def find_15m_entry(df15: pd.DataFrame, after_date, cfg, threshold: int,
                   sessions: int = ENTRY_SEARCH_SESSIONS):
    """在**确认日的次日**起找第一个 dip_buy 信号。返回 (entry_idx, entry_px) 或 None。

    关键：日线确认依据的是 `after_date` 当日的**收盘**，因此当天盘中（09:30–16:00）
    的 15 分钟 bar 早于信号本身，**绝不能用**——用了就是前视。
    因此搜索区间从 `after_date` 的次日 00:00 开始。

    入场用信号 bar 的**下一根开盘**。
    """
    if df15 is None or df15.empty:
        return None
    d0 = pd.Timestamp(after_date)
    start = d0.normalize() + pd.Timedelta(days=1)      # 次日
    if start.tzinfo is None:
        start = start.tz_localize(ET)
    cutoff = start + pd.Timedelta(days=sessions + 1)
    idxs = df15.index[(df15['time_key'] >= start) & (df15['time_key'] < cutoff)].tolist()
    from scripts.run_dip_buy_backtest import analyze_score  # 延迟导入便于测试打桩
    for i in idxs[:-1]:
        if not session_allowed(df15.iloc[i]['time_key'], cfg):
            continue
        window = df15.iloc[max(0, i - WINDOW_BARS + 1):i + 1]
        if len(window) < 30:
            continue
        try:
            res = analyze_score(window, float(df15.iloc[i]['close']), threshold)
        except Exception:
            continue
        if res.get('signal') == 'buy' and int(res.get('score', 0)) >= threshold:
            j = i + 1
            px = float(df15.iloc[j]['open'])
            if np.isfinite(px) and px > 0:
                return j, px
    return None


def arm_daily_plus_15m(code: str, df: pd.DataFrame, df15: pd.DataFrame, events: list,
                       cfg: dict, threshold: int) -> list:
    """A4：日线确认 + 15 分钟择时入场 → 日线出场。

    找不到 15 分钟信号的机会**不产生交易**（这是 A4 的真实取舍），
    但会被记录进 opportunities 以便与 A3 做**匹配子集**比较。
    """
    trades = []
    for i in events:
        if i + 1 >= len(df):
            continue
        confirm_date = df.iloc[i]['date']
        hit = find_15m_entry(df15, confirm_date, cfg, threshold)
        if hit is None:
            continue
        # 15 分钟入场时间 → 对齐到日线索引（用于日线出场）
        entry_time = df15.iloc[hit[0]]['time_key']
        di = df.index[df['date'] >= pd.Timestamp(entry_time).tz_localize(None).normalize()]
        if len(di) == 0:
            continue
        entry_di = di[0]
        entry_px = hit[1]
        atr = _num(df.iloc[i].get('atr'))
        info = simulate_daily_exit(df, entry_di, entry_px, atr)
        trades.append(_mk_trade(code, 'A4_daily_plus_15m', entry_time, entry_px, info,
                                {'confirm_date': str(confirm_date),
                                 'state': df.iloc[i]['state']}))
    return trades


def matched_pairs(df: pd.DataFrame, events: list, df15: pd.DataFrame, cfg: dict,
                  threshold: int) -> list:
    """A3 vs A4 的**匹配子集**：同一批确认机会，A3 用次日开盘、A4 用 15 分钟入场。

    返回每对 {confirm_date, ref_entry_px, timing_entry_px, fill_improve_pct}。
    """
    out = []
    for i in events:
        if i + 1 >= len(df):
            continue
        ref_px = float(df.iloc[i + 1]['open'])
        if not np.isfinite(ref_px) or ref_px <= 0:
            continue
        hit = find_15m_entry(df15, df.iloc[i]['date'], cfg, threshold)
        if hit is None:
            continue
        timing_px = hit[1]
        out.append({'confirm_date': str(df.iloc[i]['date']),
                    'ref_entry_px': ref_px, 'timing_entry_px': timing_px,
                    # 成交价改善：等到的价格比次日开盘低多少（正=更好）
                    'fill_improve_pct': (ref_px - timing_px) / ref_px * 100.0})
    return out


# ---------- 汇总 ----------

def make_daily_gate(df_daily: pd.DataFrame):
    """构造 entry_gate：仅当该交易日的日线状态 ∈ ACTIONABLE 时放行 15 分钟信号。

    用「最近一根已收盘日线」的状态（当日日线尚未收盘，因此取前一日），无前视。
    """
    dates = pd.to_datetime(df_daily['date']).dt.normalize()
    states = df_daily['state'].values

    def gate(bar_time):
        d = pd.Timestamp(bar_time).tz_localize(None).normalize() \
            if pd.Timestamp(bar_time).tzinfo else pd.Timestamp(bar_time).normalize()
        # 取严格早于当日的最后一个交易日状态
        prior = np.where(dates.values < np.datetime64(d))[0]
        if len(prior) == 0:
            return False
        return states[prior[-1]] in ACTIONABLE

    return gate


def arm_daily_gate_15m(code: str, df_daily: pd.DataFrame, df15: pd.DataFrame,
                       cfg: dict, threshold: int, time_exit_bars: int) -> list:
    """A2：日线闸门 + 15 分钟入场，**保持日内出场**（与 A1 同出场）。"""
    gate = make_daily_gate(df_daily)
    return [{**t, 'arm': 'A2_daily_gate_15m', 'stop_hit': t['reason'] == 'STOP_LOSS',
             'mae_pct': np.nan, 'hold_days': t['holding_days']}
            for t in replay_stock(code, df15, cfg, threshold, time_exit_bars,
                                  entry_gate=gate)]


def summarize(trades: list) -> dict:
    if not trades:
        return {'trades': 0}
    d = pd.DataFrame(trades)
    closed = d[~d['open'].astype(bool)] if 'open' in d.columns else d
    base = closed if len(closed) else d
    wins = base[base['net_pnl_usd'] > 0]
    losses = base[base['net_pnl_usd'] <= 0]
    gw, gl = float(wins['net_pnl_usd'].sum()), float(abs(losses['net_pnl_usd'].sum()))
    return {
        'trades': len(base),
        'win_rate': len(wins) / len(base),
        'expectancy': float(base['net_pnl_usd'].mean()),
        'profit_factor': gw / gl if gl else np.inf,
        'total': float(base['net_pnl_usd'].sum()),
        'max_dd': max_drawdown(base.sort_values('entry_date')['net_pnl_usd'].cumsum()),
        'stop_rate': float(base['stop_hit'].mean()) if 'stop_hit' in base else float('nan'),
        'mae': float(base['mae_pct'].mean()) if 'mae_pct' in base else float('nan'),
        'avg_hold': float(base['hold_days'].mean()) if 'hold_days' in base else float('nan'),
    }


def build_report(arms: dict, pairs: list, skipped: int) -> str:
    lines = ['# 买入链路四组对照：分钟择时到底有没有用\n']
    lines.append(f'- 生成时间: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append('- A1/A2 用日内出场（原 dip_buy 括号 + 时间出场）；A3/A4 用日线出场（ATR 吊灯 + 更高低点）')
    lines.append(f'- A3/A4 同资格（日线 {" / ".join(ACTIONABLE)}）、同出场，**只差入场方式**\n')

    def _n(v, f='{:,.0f}'):
        return '—' if v is None or (isinstance(v, float) and not np.isfinite(v)) else f.format(v)

    lines.append('## 1. 四组汇总\n')
    lines.append('| 组 | 笔数 | 胜率 | 期望/笔$ | 盈亏比 | 总收益$ | 最大回撤$ | 止损率 | 平均MAE | 平均持仓(天) |')
    lines.append('|---|---|---|---|---|---|---|---|---|---|')
    labels = {'A1_legacy_15m': 'A1 原始15分钟', 'A2_daily_gate_15m': 'A2 日线闸门+15分钟',
              'A3_daily_only': 'A3 日线确认→次日开盘', 'A4_daily_plus_15m': 'A4 日线确认+15分钟择时'}
    for key, tr in arms.items():
        m = summarize(tr)
        if not m.get('trades'):
            lines.append(f'| {labels.get(key, key)} | 0 | — | — | — | — | — | — | — | — |')
            continue
        lines.append(
            f"| {labels.get(key, key)} | {m['trades']} | {m['win_rate']:.1%} | "
            f"{_n(m['expectancy'])} | {_n(m['profit_factor'], '{:.2f}')} | "
            f"{_n(m['total'])} | {_n(m['max_dd'])} | {_n(m['stop_rate'], '{:.1%}')} | "
            f"{_n(m['mae'], '{:+.2%}')} | {_n(m['avg_hold'], '{:.1f}')} |")
    lines.append('')

    lines.append('## 2. 关键对照 A3 vs A4（匹配子集）\n')
    if not pairs:
        lines.append('> 没有匹配机会（A4 未在任何确认机会上找到 15 分钟信号），无法判定。\n')
    else:
        p = pd.DataFrame(pairs)
        imp = p['fill_improve_pct']
        better = int((imp > 0).sum())
        lines.append(f'- 匹配机会数: **{len(p)}**（A4 无信号而放弃的机会: {skipped}）')
        lines.append(f'- **成交价改善**：均值 {imp.mean():+.3f}%，中位 {imp.median():+.3f}%；'
                     f'优于开盘价的比例 {better}/{len(p)}（{better/len(p):.0%}）')
        # 用 t 统计量粗判显著（样本小，仅作参考）
        if len(p) > 2 and imp.std(ddof=1) > 0:
            t = imp.mean() / (imp.std(ddof=1) / np.sqrt(len(p)))
            lines.append(f'- t 统计量 ≈ {t:.2f}（|t|<2 视为不显著）')
        lines.append('')
        lines.append('- 判定：**成交价改善若 < 0.2%（一个往返成本）且不显著，'
                     '则 15 分钟择时不足以覆盖其复杂度**。')
        lines.append('')

    lines.append('## 3. 怎么读\n')
    lines.append('- **A1 vs A2**：日线闸门是否提升了期望（出场与入场方式保持不变）。')
    lines.append('- **A3 vs A4**：分钟择时是否改善了成交价 / MAE / 止损率——这是必须自证的一点。')
    lines.append('- **A2 vs A4**：入场周期从「15 分钟独立触发」改为「日线确认后再 15 分钟择时」的整体差异。')
    lines.append('- A4 与 A3 的笔数不同是正常的：A4 会放弃没有 15 分钟信号的机会。'
                 '所以**总量对比无意义**，必须看匹配子集（§2）。')
    lines.append('- 本实验只改**入场链路**，未改选股池、仓位与风控；'
                 '仍受观察池是否事后选择（幸存者偏差）影响。')
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser(description='买入链路四组对照实验')
    ap.add_argument('--start', default='2021-01-01')
    ap.add_argument('--end', default=pd.Timestamp.now().strftime('%Y-%m-%d'))
    ap.add_argument('--codes', default=None)
    ap.add_argument('--benchmark', default='US.SPY')
    ap.add_argument('--page-delay', type=float, default=0.6)
    ap.add_argument('--daily-only-check', action='store_true',
                    help='只跑 A3/A4（仍需 15M，但跳过 A1/A2 的重放）')
    ap.add_argument('--output', default=str(PROJECT_ROOT / 'backtests' / 'hybrid_entry_study.md'))
    args = ap.parse_args()

    cfg = load_config()
    codes = ([c.strip() for c in args.codes.split(',') if c.strip()] if args.codes
             else list(cfg.get('dip_buy', {}).get('watch_list', [])))
    if not codes:
        print('未找到观察池代码'); return 2
    threshold = int(cfg.get('dip_buy', {}).get('buy_threshold', 7))

    print(f'拉取日线：{len(codes)} 只 + 基准 {args.benchmark}')
    daily = fetch_daily_data(codes + [args.benchmark], args.start, args.end)
    bench = daily.pop(args.benchmark, None)
    if not daily:
        print('没有日线数据'); return 3

    print(f'拉取 15M：{len(codes)} 只（翻页间隔 {args.page_delay}s）')
    m15, coverage = fetch_15m(codes, args.start, args.end, page_delay=args.page_delay)
    truncated = [c for c, v in coverage.items() if v.get('truncated')]
    if truncated:
        print(f'⚠️  以下股票数据被截断，结论不可用: {", ".join(truncated)}')

    arms = {k: [] for k in ('A1_legacy_15m', 'A2_daily_gate_15m',
                            'A3_daily_only', 'A4_daily_plus_15m')}
    pairs, skipped = [], 0

    for code, df_raw in daily.items():
        df = annotate(df_raw, bench)
        events = confirmation_events(df)
        if not events:
            print(f'  {code}: 无日线确认事件')
            continue
        df15 = m15.get(code)

        # A3/A4
        arms['A3_daily_only'].extend(arm_daily_only(code, df, events))
        if df15 is not None:
            pr = matched_pairs(df, events, df15, cfg, threshold)
            pairs.extend(pr)
            skipped += len(events) - len(pr)
            arms['A4_daily_plus_15m'].extend(
                arm_daily_plus_15m(code, df, df15, events, cfg, threshold))

        # A1/A2（日内出场；A2 叠加日线闸门）
        if df15 is not None and not args.daily_only_check:
            teb = int(cfg.get('dip_buy', {}).get('time_exit_bars', 8))
            arms['A1_legacy_15m'].extend(
                [{**t, 'arm': 'A1_legacy_15m', 'stop_hit': t['reason'] == 'STOP_LOSS',
                  'mae_pct': np.nan, 'hold_days': t['holding_days']}
                 for t in replay_stock(code, df15, cfg, threshold, teb)])
            arms['A2_daily_gate_15m'].extend(
                arm_daily_gate_15m(code, df, df15, cfg, threshold, teb))

    report = build_report(arms, pairs, skipped)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding='utf-8')
    for k, tr in arms.items():
        if tr:
            pd.DataFrame(tr).to_csv(out.with_name(f'{out.stem}_{k}.csv'),
                                    index=False, encoding='utf-8')
    if pairs:
        pd.DataFrame(pairs).to_csv(out.with_name(f'{out.stem}_matched_pairs.csv'),
                                   index=False, encoding='utf-8')
    print(report)
    print(f'已写出: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
