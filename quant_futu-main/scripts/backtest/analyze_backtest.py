#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
港股回测结果分析工具
分析 trade_history.csv 和 equity_curve.csv，输出详细的中文统计报告
"""

import csv
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def load_trades():
    """加载已完成的交易记录"""
    trades = []
    trade_file = Path('logs/trade_history.csv')
    if not trade_file.exists():
        print("❌ 未找到交易记录文件: logs/trade_history.csv")
        return []
    
    with open(trade_file, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('sell_date'):  # 只统计已平仓的交易
                trades.append(row)
    return trades


def load_equity_curve():
    """加载净值曲线"""
    curve_file = Path('logs/equity_curve.csv')
    if not curve_file.exists():
        return []
    
    with open(curve_file, 'r', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def print_section(title):
    """打印分节标题"""
    print(f"\n{'='*60}")
    print(f"【{title}】")
    print('='*60)


def analyze_basic_stats(trades, curve):
    """基本统计"""
    print_section("基本统计")
    
    if not trades:
        print("❌ 没有交易记录")
        return
    
    if curve:
        print(f"回测区间: {curve[0]['date']} ~ {curve[-1]['date']}")
        initial = float(curve[0]['value'])
        final = float(curve[-1]['value'])
        total_return = (final - initial) / initial * 100
        
        print(f"初始资金: {initial:,.2f} HKD")
        print(f"最终资金: {final:,.2f} HKD")
        print(f"总收益率: {total_return:+.2f}%")
    
    print(f"总交易次数: {len(trades)}笔")


def analyze_profit_distribution(trades):
    """盈亏分布分析"""
    print_section("盈亏分布")
    
    wins = [t for t in trades if float(t['profit_pct']) > 0]
    losses = [t for t in trades if float(t['profit_pct']) <= 0]
    profits = [float(t['profit_pct']) for t in trades]
    
    win_rate = len(wins) / len(trades) * 100
    print(f"盈利次数: {len(wins)}笔 ({win_rate:.1f}%)")
    print(f"亏损次数: {len(losses)}笔 ({100-win_rate:.1f}%)")
    
    if wins:
        win_profits = [float(t['profit_pct']) for t in wins]
        avg_win = statistics.mean(win_profits)
        print(f"平均盈利: +{avg_win:.2f}%")
        print(f"最大盈利: +{max(win_profits):.2f}%")
    
    if losses:
        loss_profits = [float(t['profit_pct']) for t in losses]
        avg_loss = statistics.mean(loss_profits)
        print(f"平均亏损: {avg_loss:.2f}%")
        print(f"最大亏损: {min(loss_profits):.2f}%")
    
    if wins and losses:
        win_mean = statistics.mean([float(t['profit_pct']) for t in wins])
        loss_mean = abs(statistics.mean([float(t['profit_pct']) for t in losses]))
        profit_loss_ratio = win_mean / loss_mean if loss_mean > 0 else 0
        print(f"盈亏比: {profit_loss_ratio:.2f}")
    
    print(f"中位数收益: {statistics.median(profits):.2f}%")


def analyze_exit_reasons(trades):
    """退出原因分析"""
    print_section("退出原因统计")
    
    reasons = Counter(t['reason'] for t in trades)
    
    reason_names = {
        'rsrs_stop': 'RSRS止损',
        'atr_stop': 'ATR止损',
        'atr_take_profit': 'ATR止盈',
        'time_exit': '到期平仓',
        'force_close': '强制平仓',
    }
    
    print(f"{'退出原因':<15} {'次数':>6} {'占比':>8} {'平均收益':>10}")
    print("-" * 45)
    
    for reason, count in reasons.most_common():
        rt = [t for t in trades if t['reason'] == reason]
        avg_pnl = statistics.mean([float(t['profit_pct']) for t in rt])
        pct = count / len(trades) * 100
        name = reason_names.get(reason, reason)
        print(f"{name:<15} {count:>6} {pct:>7.1f}% {avg_pnl:>+9.2f}%")


def analyze_holding_days(trades):
    """持仓天数分析"""
    print_section("持仓天数分析")
    
    holding_days = [float(t['holding_days']) for t in trades]
    wins = [t for t in trades if float(t['profit_pct']) > 0]
    losses = [t for t in trades if float(t['profit_pct']) <= 0]
    
    print(f"平均持仓: {statistics.mean(holding_days):.0f}天")
    print(f"中位持仓: {statistics.median(holding_days):.0f}天")
    print(f"最短持仓: {min(holding_days):.0f}天")
    print(f"最长持仓: {max(holding_days):.0f}天")
    
    if wins:
        win_days = [float(t['holding_days']) for t in wins]
        print(f"盈利交易平均持仓: {statistics.mean(win_days):.0f}天")
    
    if losses:
        loss_days = [float(t['holding_days']) for t in losses]
        print(f"亏损交易平均持仓: {statistics.mean(loss_days):.0f}天")


def analyze_yearly_stats(trades):
    """按年份统计"""
    print_section("年度统计")
    
    by_year = defaultdict(list)
    for t in trades:
        year = t['sell_date'][:4]
        by_year[year].append(t)
    
    print(f"{'年份':<6} {'交易':>5} {'盈利':>5} {'亏损':>5} {'胜率':>7} {'累计收益':>10} {'平均收益':>10}")
    print("-" * 60)
    
    for year in sorted(by_year.keys()):
        yt = by_year[year]
        yp = [float(t['profit_pct']) for t in yt]
        yw = len([t for t in yt if float(t['profit_pct']) > 0])
        yl = len(yt) - yw
        wr = yw / len(yt) * 100
        total_p = sum(yp)
        avg_p = statistics.mean(yp)
        print(f"{year:<6} {len(yt):>5} {yw:>5} {yl:>5} {wr:>6.1f}% {total_p:>+9.1f}% {avg_p:>+9.2f}%")


def analyze_drawdown(curve):
    """最大回撤分析"""
    print_section("风险指标")
    
    if not curve:
        print("❌ 没有净值曲线数据")
        return
    
    max_dd = 0
    peak = float(curve[0]['value'])
    dd_start = dd_end = peak_date = ''
    
    for row in curve:
        eq = float(row['value'])
        if eq > peak:
            peak = eq
            peak_date = row['date']
        dd = (peak - eq) / peak
        if dd > max_dd:
            max_dd = dd
            dd_start = peak_date
            dd_end = row['date']
    
    print(f"最大回撤: {max_dd*100:.2f}%")
    print(f"回撤区间: {dd_start} ~ {dd_end}")


def analyze_current_positions():
    """当前持仓"""
    print_section("当前持仓")
    
    trade_file = Path('logs/trade_history.csv')
    if not trade_file.exists():
        print("❌ 未找到交易记录文件")
        return
    
    open_trades = []
    with open(trade_file, 'r', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            if not row.get('sell_date'):
                open_trades.append(row)
    
    if not open_trades:
        print("✅ 当前无持仓")
        return
    
    print(f"持仓数量: {len(open_trades)}只\n")
    print(f"{'代码':<12} {'股数':>8} {'买入价':>10} {'买入日期':<12} {'原因':<15}")
    print("-" * 60)
    
    for t in open_trades:
        print(f"{t['stock_code']:<12} {t['shares']:>8} {t['buy_price']:>10} {t['buy_date']:<12} {t.get('reason', ''):<15}")


def analyze_big_losses(trades, threshold=-15):
    """大额亏损分析"""
    print_section(f"大额亏损 (亏损>{abs(threshold)}%)")
    
    big_losses = [t for t in trades if float(t['profit_pct']) < threshold]
    
    if not big_losses:
        print(f"✅ 没有亏损超过{abs(threshold)}%的交易")
        return
    
    print(f"共 {len(big_losses)} 笔大额亏损\n")
    print(f"{'代码':<12} {'买入日期':<12} {'卖出日期':<12} {'收益率':>10} {'持仓天数':>8} {'退出原因':<15}")
    print("-" * 75)
    
    for t in sorted(big_losses, key=lambda x: float(x['profit_pct'])):
        print(f"{t['stock_code']:<12} {t['buy_date']:<12} {t['sell_date']:<12} "
              f"{float(t['profit_pct']):>+9.1f}% {int(float(t['holding_days'])):>7}天 {t['reason']:<15}")


def analyze_loss_streaks(trades, top_n=10):
    """连亏分析"""
    print_section(f"连续亏损统计 (Top {top_n})")
    
    # 找出所有连续亏损序列
    streaks = []
    current_streak = []
    
    for t in trades:
        if float(t['profit_pct']) <= 0:
            current_streak.append(t)
        else:
            if current_streak:
                streaks.append(current_streak[:])
            current_streak = []
    
    if current_streak:
        streaks.append(current_streak)
    
    if not streaks:
        print("✅ 没有连续亏损记录")
        return
    
    # 按连亏次数排序
    streaks.sort(key=len, reverse=True)
    
    print(f"{'排名':<4} {'连亏次数':>8} {'开始日期':<12} {'结束日期':<12} {'累计亏损':>10} {'主要原因':<15}")
    print("-" * 70)
    
    for i, s in enumerate(streaks[:top_n]):
        loss_sum = sum(float(t['profit_pct']) for t in s)
        start_date = s[0]['buy_date']
        end_date = s[-1]['sell_date']
        
        # 统计主要原因
        reasons = Counter(t['reason'] for t in s)
        main_reason = reasons.most_common(1)[0][0]
        
        reason_names = {
            'rsrs_stop': 'RSRS止损',
            'atr_stop': 'ATR止损',
            'time_exit': '到期平仓',
        }
        main_reason_cn = reason_names.get(main_reason, main_reason)
        
        print(f"#{i+1:<3} {len(s):>8}次 {start_date:<12} {end_date:<12} {loss_sum:>+9.1f}% {main_reason_cn:<15}")


def analyze_loss_by_holding_days(trades):
    """亏损按持仓天数分布"""
    print_section("亏损交易持仓天数分布")
    
    losses = [t for t in trades if float(t['profit_pct']) <= 0]
    
    if not losses:
        print("✅ 没有亏损交易")
        return
    
    distribution = defaultdict(int)
    for t in losses:
        days = int(float(t['holding_days']))
        if days <= 3:
            distribution['1-3天'] += 1
        elif days <= 7:
            distribution['4-7天'] += 1
        elif days <= 14:
            distribution['8-14天'] += 1
        elif days <= 30:
            distribution['15-30天'] += 1
        elif days <= 60:
            distribution['31-60天'] += 1
        else:
            distribution['60天以上'] += 1
    
    total = len(losses)
    print(f"{'持仓天数':<12} {'亏损次数':>8} {'占比':>8}")
    print("-" * 35)
    
    for period in ['1-3天', '4-7天', '8-14天', '15-30天', '31-60天', '60天以上']:
        count = distribution[period]
        pct = count / total * 100 if total > 0 else 0
        print(f"{period:<12} {count:>8} {pct:>7.1f}%")


def analyze_monthly_stats(trades):
    """月度统计"""
    print_section("月度统计 (最近12个月)")
    
    by_month = defaultdict(list)
    for t in trades:
        month = t['sell_date'][:7]  # YYYY-MM
        by_month[month].append(t)
    
    # 按月份排序，只显示最近12个月
    months = sorted(by_month.keys(), reverse=True)[:12]
    months.reverse()  # 按时间正序显示
    
    print(f"{'月份':<10} {'交易':>5} {'盈利':>5} {'亏损':>5} {'胜率':>7} {'累计收益':>10}")
    print("-" * 50)
    
    for month in months:
        mt = by_month[month]
        mp = [float(t['profit_pct']) for t in mt]
        mw = len([t for t in mt if float(t['profit_pct']) > 0])
        ml = len(mt) - mw
        wr = mw / len(mt) * 100
        total_p = sum(mp)
        print(f"{month:<10} {len(mt):>5} {mw:>5} {ml:>5} {wr:>6.1f}% {total_p:>+9.1f}%")


def main():
    """主函数"""
    print("\n" + "="*60)
    print("港股回测结果分析报告")
    print("="*60)
    
    # 加载数据
    trades = load_trades()
    curve = load_equity_curve()
    
    if not trades:
        print("\n❌ 没有找到交易记录，请先运行回测")
        return
    
    # 各项分析
    analyze_basic_stats(trades, curve)
    analyze_profit_distribution(trades)
    analyze_exit_reasons(trades)
    analyze_holding_days(trades)
    analyze_yearly_stats(trades)
    analyze_monthly_stats(trades)
    analyze_drawdown(curve)
    analyze_loss_streaks(trades)
    analyze_loss_by_holding_days(trades)
    analyze_big_losses(trades)
    analyze_current_positions()
    
    print("\n" + "="*60)
    print("分析完成")
    print("="*60 + "\n")


if __name__ == '__main__':
    main()
