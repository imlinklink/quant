"""
双吊灯策略单股回测 - 命令行入口

用法:
    python -m scripts.run_chandelier_backtest US.AAPL 2026-03-01
    python -m scripts.run_chandelier_backtest US.AAPL 2026-03-01 --direction short
    python -m scripts.run_chandelier_backtest US.AAPL 2026-03-01 --price 185.0
    python -m scripts.run_chandelier_backtest US.AAPL 2026-03-01 --holding-days 30
    python -m scripts.run_chandelier_backtest US.AAPL 2026-03-01 --no-plot
"""
import argparse
import logging
import sys
import os

# 添加项目根目录到 path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from mutifactor.data.us_fetcher import FutuUSDataFetcher
from mutifactor.strategies.chandelier_backtest import (
    ChandelierBacktester, print_results, plot_result
)
from mutifactor.utils.us_config_loader import config


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
        datefmt='%H:%M:%S'
    )


def main():
    parser = argparse.ArgumentParser(description='双吊灯策略单股回测')
    parser.add_argument('stock_code', help='股票代码，如 US.AAPL')
    parser.add_argument('entry_date', help='买入日期，如 2026-03-01')
    parser.add_argument('--direction', choices=['long', 'short'], default=None,
                        help='方向: long(做多,默认) / short(做空)')
    parser.add_argument('--price', type=float, default=None,
                        help='指定买入价（不指定则用次日开盘价）')
    parser.add_argument('--holding-days', type=int, default=None,
                        help='持仓天数（默认22个交易日）')
    parser.add_argument('--no-plot', action='store_true',
                        help='不生成图表')
    parser.add_argument('--output', type=str, default=None,
                        help='图表输出路径')

    args = parser.parse_args()

    setup_logging()

    # 从 config.yaml 读取回测配置
    bt_config = config.get('backtest_chandelier', {})

    # 命令行参数覆盖
    if args.holding_days:
        bt_config['holding_days'] = args.holding_days

    print(f"\n📋 回测配置:")
    print(f"   股票: {args.stock_code}")
    print(f"   买入日: {args.entry_date}")
    print(f"   方向: {args.direction or bt_config.get('direction', 'long')}")
    print(f"   K线类型: {bt_config.get('kline_type', '5min')}")
    print(f"   持仓天数: {bt_config.get('holding_days', 22)}")
    print(f"   ATR周期: {bt_config.get('chandelier', {}).get('atr_period', 14)}")
    print(f"   止损倍数: ×{bt_config.get('chandelier', {}).get('stop_multiplier', 2.0)}")
    print(f"   止盈激活: {bt_config.get('chandelier', {}).get('profit_activate_pct', 0.02)*100:.0f}%")
    print()

    # 连接数据源
    futu_host = config.get('futu.host', '127.0.0.1')
    futu_port = config.get('futu.port', 11111)

    fetcher = FutuUSDataFetcher(host=futu_host, port=futu_port)
    if not fetcher.connect():
        print("❌ 无法连接富途OpenD，请确认已启动")
        sys.exit(1)

    try:
        # 运行回测
        backtester = ChandelierBacktester(fetcher, bt_config)
        result = backtester.run(
            stock_code=args.stock_code,
            entry_date=args.entry_date,
            direction=args.direction,
            entry_price=args.price
        )

        # 打印结果
        print_results([result])

        # 绘图
        if not args.no_plot:
            output_path = args.output or None
            plot_result(result, output_path)

    finally:
        fetcher.disconnect()


if __name__ == '__main__':
    main()
