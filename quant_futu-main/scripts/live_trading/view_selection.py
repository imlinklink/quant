#!/usr/bin/env python3
"""
港股选股结果查看工具
用法: python scripts/live_trading/view_selection.py
"""
import os
import sys

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from mutifactor.data import get_hk_stock_name
from mutifactor.infra.yaml_storage import yaml_storage, TradingEnv

def view_selection():
    """查看最新的选股结果"""
    
    print("=" * 70)
    print("🇭🇰 港股实盘选股结果")
    print("=" * 70)
    
    # 从数据库读取选股结果
    try:
        selected = yaml_storage.get_selection_results(TradingEnv.REAL)

        if selected:
            from datetime import date
            today = date.today()
            
            # 只显示今天的选股结果
            today_selected = [s for s in selected if s['selection_date'] == today]
            
            if today_selected:
                # 只取最新一批选股结果（按日期+时间过滤）
                latest_date = today_selected[0]['selection_date']
                latest_time = today_selected[0]['selection_time']

                # 过滤同一批次的记录
                latest_selected = [
                    s for s in today_selected
                    if s['selection_date'] == latest_date and s['selection_time'] == latest_time
                ]

                print(f"\n📅 选股日期: {latest_date} {latest_time}")

                # 显示选中的股票（去重后的）
                print(f"\n📈 今日选中的股票 ({len(latest_selected)}只):")
                print("-" * 70)
                print(f"{'序号':<4} {'代码':<12} {'名称':<14} {'价格':>10} {'状态':>8}")
                print("-" * 70)
                for i, stock in enumerate(latest_selected, 1):
                    code = stock.get('stock_code', 'N/A')
                    name = stock.get('stock_name', get_hk_stock_name(code))
                    price = stock.get('price')
                    price_str = f"{price:.2f}" if price else "获取中..."
                    in_pos = stock.get('in_position', False)
                    status = "⚠️ 已在仓" if in_pos else "✅ 可买入"
                    print(f"{i:<4} {code:<12} {name:<14} {price_str:>10} {status:>8}")

                print(f"\n💰 港股数据来自数据库 (env=REAL)")
            else:
                print("\n⚠️  今日暂无选股结果")
        else:
            print("\n⚠️  暂无选股结果")
    except Exception as e:
        print(f"\n⚠️  读取选股结果失败: {e}")
    
    # 从数据库读取当前持仓
    print("\n" + "-" * 70)
    try:
        state = yaml_storage.load_trading_state(TradingEnv.REAL)
        
        if state:
            positions = state.get('positions', {})
            if positions:
                print(f"📋 当前持仓 ({len(positions)}只):")
                print("-" * 70)
                print(f"{'代码':<12} {'名称':<12} {'数量':>8} {'成本价':>10} {'最高价':>10}")
                print("-" * 70)
                for code, pos in positions.items():
                    name = get_hk_stock_name(code)
                    qty = pos.get('quantity', 0)
                    cost = pos.get('cost_price', 0)
                    highest = pos.get('highest_price', 0)
                    print(f"{code:<12} {name:<12} {qty:>8} {cost:>10.3f} {highest:>10.3f}")
            else:
                print("📋 当前持仓: 空")
            
            print(f"\n💵 港股已用资金: {state.get('used_capital', 0):,.2f} HKD")
            print(f"💵 港股总资金: {state.get('capital', 100000):,.2f} HKD")
        else:
            print("📋 当前持仓: 无状态记录")
    except Exception as e:
        print(f"⚠️  读取持仓状态失败: {e}")
    
    print("\n" + "=" * 70)
    
    # 查看最近日志中的选股信息
    log_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        'logs', 'live_output.log'
    )
    
    if os.path.exists(log_file):
        print("\n📝 最近选股日志 (最近5条):")
        print("-" * 70)
        import subprocess
        result = subprocess.run(
            ['grep', '-E', '选股|触发买入|买入成功|卖出', log_file],
            capture_output=True, text=True
        )
        lines = result.stdout.strip().split('\n')[-5:]
        for line in lines:
            if line.strip():
                parts = line.split(' - ')
                if len(parts) >= 4:
                    time_str = parts[0].split('T')[1][:8] if 'T' in parts[0] else ''
                    msg = parts[-1]
                    print(f"   [{time_str}] {msg}")


if __name__ == '__main__':
    view_selection()
