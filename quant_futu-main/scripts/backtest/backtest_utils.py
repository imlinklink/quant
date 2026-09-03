# -*- coding: utf-8 -*-
"""
回测工具模块 - 提供回测脚本通用的工具和常量
"""
from typing import Dict


# ==================== 颜色常量 ====================
COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_RESET = "\033[0m"

# ==================== 图标常量 ====================
ICON_PROFIT = "📈"
ICON_LOSS = "📉"

# ==================== 退出原因映射 ====================
EXIT_REASON_MAP = {
    'atr_stop_loss': 'ATR止损',
    'atr_take_profit': 'ATR止盈',
    'time_exit': '到期平仓',
}


def format_return_icon(total_return_pct: float) -> str:
    """
    根据收益率返回对应的图标和颜色
    
    Args:
        total_return_pct: 收益率百分比
        
    Returns:
        带颜色的图标字符串
    """
    if total_return_pct >= 0:
        return f"{COLOR_GREEN}{ICON_PROFIT}"
    else:
        return f"{COLOR_RED}{ICON_LOSS}"


def get_return_color(total_return_pct: float) -> str:
    """
    根据收益率返回对应的颜色代码
    
    Args:
        total_return_pct: 收益率百分比
        
    Returns:
        ANSI颜色代码
    """
    return COLOR_GREEN if total_return_pct >= 0 else COLOR_RED


def print_backtest_results(
    results: Dict,
    market_name: str = "",
    market_emoji: str = "",
    currency: str = "HKD"
):
    """
    打印回测结果（通用函数，支持港股/美股）
    
    Args:
        results: 回测结果字典
        market_name: 市场名称（如"港股"、"美股"）
        market_emoji: 市场标识emoji（如"🇭🇰"、"🇺🇸"）
        currency: 货币单位（如"HKD"、"USD"）
    """
    prefix = f"【{market_emoji} {market_name} " if market_emoji and market_name else "【"
    
    print("\n" + "=" * 70)
    print(f"{prefix}{results.get('strategy', 'MOMENTUM').upper()}策略回测结果】")
    print("=" * 70)
    print(f"回测期间: {results['start_date']} 至 {results['end_date']}")
    print(f"初始资金: {results['initial_capital']:,.2f} {currency}")
    print(f"最终资产: {results['final_value']:,.2f} {currency}")

    # 收益率颜色
    total_return_pct = results['total_return'] * 100
    annual_return_pct = results['annual_return'] * 100
    return_icon = format_return_icon(total_return_pct)
    return_color = get_return_color(total_return_pct)

    print(f"总收益率: {return_icon} {return_color}{total_return_pct:.2f}%{COLOR_RESET}")
    print(f"年化收益率: {return_color}{annual_return_pct:.2f}%{COLOR_RESET}")
    print(f"最大回撤: {results['max_drawdown']*100:.2f}%")
    print(f"夏普比率: {results['sharpe_ratio']:.2f}")
    print("-" * 70)
    print(f"交易次数: {results['total_trades']}")
    print(f"胜率: {results['win_rate']*100:.2f}%")

    # 持仓统计
    if 'equity_curve' in results and len(results['equity_curve']) > 0:
        max_positions = int(results['equity_curve']['position_count'].max())
        avg_positions = results['equity_curve']['position_count'].mean()
        print(f"最大持仓数: {max_positions} 只")
        if avg_positions > 0:
            print(f"平均持仓数: {avg_positions:.1f} 只")

    # 退出原因统计
    if len(results['trade_history']) > 0:
        reason_counts = results['trade_history']['reason'].value_counts()
        print("-" * 70)
        print("退出原因统计:")
        for reason, count in reason_counts.items():
            reason_cn = EXIT_REASON_MAP.get(reason, reason)
            print(f"  {reason_cn}: {count}次 ({count/results['total_trades']*100:.1f}%)")

    # 交易成本
    if 'total_cost' in results:
        print("-" * 70)
        print("交易成本明细:")
        print(f"  佣金: {results.get('total_commission', 0):,.2f} {currency}")
        print(f"  印花税: {results.get('total_stamp_duty', 0):,.2f} {currency}")
        print(f"  交易征费: {results.get('total_trading_fee', 0):,.2f} {currency}")
        print(f"  结算费: {results.get('total_settlement_fee', 0):,.2f} {currency}")
        print(f"  滑点: {results.get('total_slippage', 0):,.2f} {currency}")
        print(f"  总成本: {results['total_cost']:,.2f} {currency} ({results['total_cost']/results['initial_capital']*100:.2f}%)")

    print("=" * 70)


def print_hk_results(results: Dict):
    """打印港股回测结果"""
    print_backtest_results(
        results,
        market_name="港股",
        market_emoji="🇭🇰",
        currency="HKD"
    )
