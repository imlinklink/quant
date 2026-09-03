# -*- coding: utf-8 -*-
"""
富途连接检查模块
"""
from datetime import datetime, timedelta
from mutifactor.data import FutuHKDataFetcher
from mutifactor.config import Config
from mutifactor.utils import setup_logger


def check_futu_connection():
    """检查富途连接"""
    logger = setup_logger('test')
    logger.info("检查富途OpenD连接...")

    try:
        with FutuHKDataFetcher(host=Config.futu.HOST, port=Config.futu.PORT) as fetcher:
            blue_chips = fetcher.get_blue_chip_stocks()

            if blue_chips:
                logger.info(f"✓ 成功连接到富途OpenD")
                logger.info(f"✓ 获取到{len(blue_chips)}只恒生指数成分股")

                test_stock = blue_chips[0]
                end_date = datetime.now().strftime('%Y-%m-%d')
                start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')

                df = fetcher.fetch_stock_kline(test_stock, start_date, end_date)

                if df is not None:
                    logger.info(f"✓ 成功获取{test_stock}的K线数据")
                    logger.info(f"  数据范围: {df['date'].min()} - {df['date'].max()}")
                    logger.info(f"  数据量: {len(df)}天")
                    return True
            else:
                logger.warning("未能获取到蓝筹股数据")
                return False

    except Exception as e:
        logger.error(f"连接失败: {e}")
        logger.error("\n请确保:")
        logger.error("1. 已安装富途牛牛客户端")
        logger.error("2. 已下载并运行FutuOpenD")
        logger.error("3. FutuOpenD运行在 127.0.0.1:11111")
        logger.error("\nFutuOpenD下载地址: https://openapi.futunn.com/download")
        return False
