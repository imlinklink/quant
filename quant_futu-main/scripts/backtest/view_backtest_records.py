#!/usr/bin/env python3
"""
查看回测历史记录
用法: python scripts/backtest/view_backtest_records.py [limit]
"""
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from mutifactor.infra.yaml_storage import yaml_storage


def view_backtest_records(limit: int = 20):
    """查看回测历史记录"""
    records = yaml_storage.get_backtest_records(limit=limit)
    
    if not records:
        print("暂无回测记录")
        return
    
    print("=" * 120)
    print(f"📊 回测历史记录 (最近 {len(records)} 条)")
    print("=" * 120)
    print(f"{'ID':<4} {'日期':<16} {'策略':<10} {'回测区间':<22} {'初始资金':>10} {'最终价值':>10} {'总收益':>8} {'年化':>8} {'最大回撤':>8} {'胜率':>6} {'交易数':>6}")
    print("-" * 120)
    
    for r in records:
        record_id = r.get('id', 0)
        run_time = r.get('run_time', '').strftime('%m-%d %H:%M') if r.get('run_time') else 'N/A'
        strategy = r.get('strategy', 'N/A')[:8]
        date_range = f"{r.get('start_date', '')}~{r.get('end_date', '')}"
        initial = r.get('initial_capital', 0)
        final = r.get('final_value', 0)
        total_return = r.get('total_return', 0) * 100
        annual = r.get('annual_return', 0) * 100
        drawdown = r.get('max_drawdown', 0) * 100
        win_rate = r.get('win_rate', 0) * 100 if r.get('win_rate') else 0
        trades = r.get('total_trades', 0)
        
        print(f"{record_id:<4} {run_time:<16} {strategy:<10} {date_range:<22} {initial:>10,.0f} {final:>10,.0f} {total_return:>7.1f}% {annual:>7.1f}% {drawdown:>7.1f}% {win_rate:>5.1f}% {trades:>6}")
    
    print("=" * 120)
    print(f"\n💡 提示: 运行 'python scripts/backtest/view_backtest_records.py 50' 可查看50条记录")


if __name__ == '__main__':
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    view_backtest_records(limit)
