#!/usr/bin/env python3
"""
直接测试选股逻辑 - 不依赖时间窗口
连接富途模拟仓，直接执行选股并输出结果

用法:
    python test_hk_selection_direct.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

import logging
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from mutifactor.data import FutuHKDataFetcher
from mutifactor.strategies.momentum import MomentumStrategy
from mutifactor.utils.config_loader import get_project_config


def test_selection_direct():
    """直接测试选股逻辑"""
    logger.info("\n" + "="*60)
    logger.info("🧪 直接测试选股逻辑")
    logger.info("="*60)

    config = get_project_config()

    fetcher = None
    try:
        # 连接富途
        logger.info("\n📌 连接富途 OpenD...")
        futu_config = config.get('trading', {}).get('futu', {})
        fetcher = FutuHKDataFetcher(
            host=futu_config.get('host', '127.0.0.1'),
            port=futu_config.get('port', 11111)
        )

        if not fetcher.connect():
            logger.error("❌ 连接失败")
            return

        logger.info("✅ 连接成功")

        # 获取股票列表
        logger.info("\n📌 获取蓝筹股列表...")
        stock_codes = fetcher.get_blue_chip_stocks()
        logger.info(f"   共 {len(stock_codes)} 只股票")

        # 获取历史数据
        logger.info("\n📌 获取历史数据...")
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=100)).strftime('%Y-%m-%d')
        logger.info(f"   日期范围: {start_date} ~ {end_date}")
        logger.info("   正在获取数据，请耐心等待...")

        stocks_data = fetcher.fetch_multiple_stocks(stock_codes, start_date, end_date)
        logger.info(f"   成功获取 {len(stocks_data)} 只股票数据")

        if not stocks_data:
            logger.error("❌ 没有获取到数据")
            return

        # 执行选股
        logger.info("\n📌 执行动量选股策略...")
        strategy = MomentumStrategy(config)
        current_date = datetime.now().date().isoformat()
        selected = strategy.select_stocks(stocks_data, current_date)

        # 输出结果
        logger.info("\n" + "="*60)
        logger.info("📊 选股结果")
        logger.info("="*60)

        if selected:
            max_pos = config.get('momentum', {}).get('max_positions', 3)
            final_selection = selected[:max_pos]
            logger.info(f"\n选出 {len(selected)} 只股票，最终选择前 {len(final_selection)} 只:\n")

            for i, code in enumerate(final_selection, 1):
                # 获取股票名称
                from mutifactor.data import get_hk_stock_name
                name = get_hk_stock_name(code)

                # 获取当前价格
                if code in stocks_data:
                    latest_price = stocks_data[code]['close'].iloc[-1]
                    logger.info(f"   {i}. {code} {name}")
                    logger.info(f"      最新价格: {latest_price:.3f}")
                else:
                    logger.info(f"   {i}. {code} {name}")
        else:
            logger.info("   无选股结果")

        logger.info("\n" + "="*60)

    except Exception as e:
        logger.error(f"❌ 测试失败: {e}")
        import traceback
        logger.error(traceback.format_exc())

    finally:
        if fetcher:
            try:
                fetcher.disconnect()
                logger.info("\n已断开连接")
            except Exception:
                pass


if __name__ == '__main__':
    test_selection_direct()
