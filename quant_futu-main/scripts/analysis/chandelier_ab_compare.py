#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
吊灯止损 A/B 对比（只读实验，不改实盘策略代码）

变体 A：当前实现 —— 止损基准 min(N日最高, 成本)，结果以成本价为上限
         （止损线永远 <= 成本，盈利锁定交给吊顶止盈）
变体 B：候选实现 —— 真吊灯：止损 = max(N日最高 - mult*ATR, 前值, 成本×95%)，
         可上移到成本之上（锁盈），只升不降

对照口径：同一批真实港股日K + 5 组典型行情合成路径，
其余参数（ATR/止盈/冷却/时间上限）两版完全一致。

用法：
    python3 scripts/analysis/chandelier_ab_compare.py            # 真实数据 + 场景
    python3 scripts/analysis/chandelier_ab_compare.py --offline  # 仅合成场景（无需 OpenD）
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import numpy as np
import pandas as pd

from mutifactor.strategies.exit_strategy import ATRDynamicStrategy


UNIVERSE = [
    'HK.00700', 'HK.09988', 'HK.03690', 'HK.01810',
    'HK.09618', 'HK.01024', 'HK.02318', 'HK.01299',
]
START = '2024-09-01'
END = '2026-09-01'
ENTRY_EVERY = 30      # 每 N 根K线尝试建一次仓
REENTRY_GAP = 5       # 平仓后空仓 N 根
MAX_HOLD_BARS = 120   # 硬性持仓上限（两版一致，标记 TIMEOUT）
WARMUP_BARS = 45      # 首个建仓前最少预热K线（保证 ATR/RSRS 有意义）
ATR_MULT = 2.0


class _VariantConfig:
    risk = {
        'exit_strategy': 'atr_dynamic',
        'atr_period': 14,
        'chandelier_period': 22,
        'stop_loss_multiplier': ATR_MULT,
        'take_profit_multiplier': 3.0,
        'time_exit': {'enabled': False},
        'decline_acceleration': {'enabled': False},
        'early_hard_stop_pct': 0.08,
        'rsrs_warn': {'early_exempt_days': 15},
    }


class VariantB(ATRDynamicStrategy):
    """候选 B：真吊灯（可锁盈，只升不降，底线成本-5%）"""

    def calculate_stop_loss(self, position: dict, df: pd.DataFrame) -> float:
        atr = self.calculate_atr(df, self.atr_period)
        cost = float(position['cost_price'])
        if atr <= 0:
            return cost * 0.95
        lookback = min(self.chandelier_period, len(df))
        # 只用已收盘的历史K线（排除当日），避免建仓当天用当日 high
        # 造成止损线在入场价上方、次日被无意义扫出
        history = df.iloc[:-1] if len(df) > 1 else df
        period_high = float(history['high'].tail(lookback).max())
        prev = float(position.get('stop_price') or 0)
        new_stop = period_high - self.stop_loss_multiplier * atr
        return max(new_stop, prev, cost * 0.95)


def make_strategies():
    cfg = dict(_VariantConfig.risk)
    from mutifactor.strategies.exit_strategy import ExitStrategyFactory
    a = ExitStrategyFactory.create('atr_dynamic', cfg)
    b = VariantB(cfg)
    return a, b


def fetch_daily(code: str) -> pd.DataFrame:
    from futu import OpenQuoteContext, RET_OK, KLType
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    try:
        ret, data, _ = ctx.request_history_kline(
            code=code, start=START, end=END, ktype=KLType.K_DAY, max_count=800,
        )
        if ret != RET_OK or data is None or len(data) == 0:
            return pd.DataFrame()
        df = pd.DataFrame({
            'date': pd.to_datetime(data['time_key']).dt.normalize(),
            'open': data['open'].astype(float),
            'high': data['high'].astype(float),
            'low': data['low'].astype(float),
            'close': data['close'].astype(float),
            'volume': data['volume'].astype(float),
        })
        return df.sort_values('date').reset_index(drop=True)
    finally:
        ctx.close()


def _new_position(cost: float, day: str) -> dict:
    return {
        'cost_price': float(cost),
        'quantity': 1,
        'buy_date': str(day)[:10],
        'highest_price': float(cost),
        'stop_price': 0.0,
    }


def walk_stock(strategy, df: pd.DataFrame) -> dict:
    """单只股票按日推进：周期开仓→规则退出→再开仓。返回交易与每日权益。"""
    trades = []
    daily_eq = []
    n = len(df)
    pos = None
    realized = 0.0
    next_entry = 1

    for i in range(1, n):
        row = df.iloc[i]
        price = float(row['open']) if row['open'] > 0 else float(row['close'])
        day = row['date']

        if pos is None:
            if (i >= max(next_entry, WARMUP_BARS)
                    and i % ENTRY_EVERY == 0 and price > 0):
                pos = _new_position(price, day)
                pos_entry_i = i
                pos['_current_date'] = day.strftime('%Y-%m-%d')
        else:
            if i - pos_entry_i >= MAX_HOLD_BARS:
                trades.append(_close_trade(pos, price, day, pos_entry_i, i, 'TIMEOUT'))
                realized += trades[-1]['pnl_pct'] / 100.0
                pos = None
                next_entry = i + REENTRY_GAP
            else:
                df_slice = df.iloc[:i + 1]
                pos['_current_date'] = day.strftime('%Y-%m-%d')
                should_exit, reason, _atr, _tp, _sl = strategy.check_exit(
                    pos, price, df_slice
                )
                if should_exit:
                    trades.append(_close_trade(pos, price, day, pos_entry_i, i, reason))
                    realized += trades[-1]['pnl_pct'] / 100.0
                    pos = None
                    next_entry = i + REENTRY_GAP

        floating = 0.0
        if pos is not None:
            floating = (price - pos['cost_price']) / pos['cost_price']
        daily_eq.append(1.0 + realized + floating)

    if pos is not None:
        # 结束持仓按最后收盘估值（不影响交易统计，只收尾权益）
        last = df.iloc[-1]
        p = float(last['close'])
        trades.append(_close_trade(pos, p, last['date'], pos_entry_i, n - 1, 'HOLD'))

    eq = np.array(daily_eq) if daily_eq else np.array([1.0])
    peak = np.maximum.accumulate(eq)
    mdd = float(((eq - peak) / peak).min()) if len(eq) else 0.0
    return {'trades': trades, 'eq': eq, 'mdd': mdd, 'end': float(eq[-1])}


def _close_trade(pos, price, day, entry_i, exit_i, reason) -> dict:
    cost = pos['cost_price']
    pnl = (price - cost) / cost * 100 if cost > 0 else 0.0
    return {
        'entry_i': entry_i, 'exit_i': exit_i,
        'hold_bars': exit_i - entry_i,
        'pnl_pct': pnl, 'reason': reason, 'exit_price': float(price),
        'exit_day': str(day)[:10],
    }


def summarize(results: dict) -> dict:
    trades = results['trades']
    pnls = [t['pnl_pct'] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    reasons = {}
    for t in trades:
        reasons[t['reason']] = reasons.get(t['reason'], 0) + 1
    # 震出后 5 根内再涨 >5% 的“假止损”比例
    premature = 0
    for t in trades:
        if t['reason'] in ('STOP_LOSS', 'TAKE_PROFIT'):
            premature += 1
    return {
        'trades': len(trades),
        'total_return_pct': (results['end'] - 1) * 100,
        'win_rate': wins / len(trades) if trades else 0,
        'avg_hold': np.mean([t['hold_bars'] for t in trades]) if trades else 0,
        'avg_pnl': np.mean(pnls) if pnls else 0,
        'max_dd': results['mdd'] * 100,
        'reasons': reasons,
        'premature_like': premature,
    }


def run_real(offline: bool):
    a, b = make_strategies()
    rows = []
    tot_a: dict = {}
    tot_b: dict = {}
    codes = [] if offline else UNIVERSE
    if offline:
        return rows
    for code in codes:
        df = fetch_daily(code)
        if len(df) < 80:
            print(f'跳过 {code}: 数据不足 {len(df)}')
            continue
        ra = walk_stock(a, df)
        rb = walk_stock(b, df)
        sa, sb = summarize(ra), summarize(rb)
        for k, v in sa['reasons'].items():
            tot_a[k] = tot_a.get(k, 0) + v
        for k, v in sb['reasons'].items():
            tot_b[k] = tot_b.get(k, 0) + v
        rows.append({
            'code': code,
            'bars': len(df),
            'A_total%': sa['total_return_pct'],
            'B_total%': sb['total_return_pct'],
            'A_dd%': sa['max_dd'], 'B_dd%': sb['max_dd'],
            'A_trades': sa['trades'], 'B_trades': sb['trades'],
            'A_win%': sa['win_rate'] * 100, 'B_win%': sb['win_rate'] * 100,
            'A_hold': round(sa['avg_hold'], 1), 'B_hold': round(sb['avg_hold'], 1),
            'A_stop': sa['reasons'].get('STOP_LOSS', 0),
            'B_stop': sb['reasons'].get('STOP_LOSS', 0),
            'A_tp': sa['reasons'].get('TAKE_PROFIT', 0),
            'B_tp': sb['reasons'].get('TAKE_PROFIT', 0),
        })
        print(f"{code}: A {sa['total_return_pct']:+.1f}% / dd {sa['max_dd']:.1f}% | "
              f"B {sb['total_return_pct']:+.1f}% / dd {sb['max_dd']:.1f}% | "
              f"trades A{sa['trades']}/B{sb['trades']}")
    if rows:
        agg = pd.DataFrame(rows)
        print('\n=== 真实样本汇总（等权平均/合计）===')
        print(agg[['code', 'A_total%', 'B_total%', 'A_dd%', 'B_dd%',
                   'A_win%', 'B_win%', 'A_stop', 'B_stop', 'A_tp', 'B_tp']].to_string(index=False))
        print('\nA/B 平均: '
              f"总收益 {agg['A_total%'].mean():+.2f}% / {agg['B_total%'].mean():+.2f}% | "
              f"回撤 {agg['A_dd%'].mean():.2f}% / {agg['B_dd%'].mean():.2f}% | "
              f"胜率 {agg['A_win%'].mean():.1f}% / {agg['B_win%'].mean():.1f}%")
        print(f"退出原因合计 A: {tot_a}")
        print(f"退出原因合计 B: {tot_b}")
    return rows


# ================= 合成场景（5 组典型行情） =================

def _scenario_df(close_path) -> pd.DataFrame:
    c = np.asarray(close_path, dtype=float)
    # 前 WARMUP_BARS 根平盘预热，让 ATR/RSRS 有意义
    c = np.concatenate([np.full(WARMUP_BARS, c[0]), c])
    prev = np.concatenate([[c[0]], c[:-1]])
    high = np.maximum(c, prev) * 1.002
    low = np.minimum(c, prev) * 0.998
    open_ = prev  # 简化：当日开盘 = 前收（避免合成行情假跌破）
    return pd.DataFrame({
        'date': pd.date_range('2025-01-01', periods=len(c), freq='B'),
        'open': open_, 'high': high, 'low': low,
        'close': c, 'volume': np.full(len(c), 1_000_000.0),
    })


SCENARIOS = {
    '持续单边上涨(+0.8%/日)': [100 * (1.008 ** i) for i in range(120)],
    '冲高回落(+1.5%×20 后 -0.6%×40)':
        [100 * (1.015 ** i) for i in range(20)] +
        [100 * 1.015 ** 20 * (0.994 ** j) for j in range(1, 41)],
    '剧烈震荡(±2.5%)':
        [100 * (1.025 if i % 2 == 0 else 0.975) ** (1 if i % 2 == 0 else 1)
         for i in range(120)],
    '大涨后深回撤(+1.5%×25 后 -1.2%×35)':
        [100 * (1.015 ** i) for i in range(25)] +
        [100 * 1.015 ** 25 * (0.988 ** j) for j in range(1, 36)],
    '盈利25%后阴跌(-1%/日×30)':
        [100 * (1.01 ** i) for i in range(25)] +
        [100 * 1.01 ** 25 * (0.99 ** j) for j in range(1, 31)],
}


def run_scenarios():
    a, b = make_strategies()
    print('\n=== 合成场景（单仓，入场=首日开盘）===')
    header = f"{'场景':<24}{'A: 退出日/原因/收益':>26}{'B: 退出日/原因/收益':>26}"
    print(header)
    for name, path in SCENARIOS.items():
        df = _scenario_df(path)
        entry_row = df.iloc[WARMUP_BARS]
        pa = _new_position(entry_row['open'], entry_row['date'])
        pb = _new_position(entry_row['open'], entry_row['date'])
        ra = _sim_one(a, pa, df)
        rb = _sim_one(b, pb, df)
        fa = f"hold{ra['hold_bars']}d/{ra['reason']}/{ra['pnl_pct']:+.1f}%"
        fb = f"hold{rb['hold_bars']}d/{rb['reason']}/{rb['pnl_pct']:+.1f}%"
        print(f"{name:<24}{fa:>26}{fb:>26}")


def _sim_one(strategy, pos, df):
    n = len(df)
    for i in range(WARMUP_BARS + 1, n):
        row = df.iloc[i]
        price = float(row['open']) if row['open'] > 0 else float(row['close'])
        pos['_current_date'] = row['date'].strftime('%Y-%m-%d')
        should, reason, *_ = strategy.check_exit(pos, price, df.iloc[:i + 1])
        if should:
            return _close_trade(pos, price, row['date'], WARMUP_BARS, i, reason)
    last = df.iloc[-1]
    return _close_trade(pos, float(last['close']), last['date'],
                        WARMUP_BARS, n - 1, 'HOLD')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--offline', action='store_true', help='只跑合成场景')
    args = parser.parse_args()
    run_real(offline=args.offline)
    run_scenarios()


if __name__ == '__main__':
    main()
