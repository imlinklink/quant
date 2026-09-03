"""
从富途同步持仓数据到数据库
用于恢复误删的 positions 表数据
"""
import os
import sys
import logging

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from mutifactor.trading import FutuTrader
from mutifactor.infra.yaml_storage import yaml_storage, TradingEnv
from mutifactor.utils.config_loader import get_project_config
from futu import TrdEnv

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def sync_positions(env: str = 'SIMULATE'):
    """
    从富途同步持仓到数据库

    Args:
        env: 'SIMULATE' 或 'REAL'
    """
    config = get_project_config()
    futu_config = config.get('trading', {}).get('futu', {})

    # 确定交易环境
    trd_env = TrdEnv.SIMULATE if env == 'SIMULATE' else TrdEnv.REAL
    trading_env = TradingEnv.SIMULATE if env == 'SIMULATE' else TradingEnv.REAL

    logger.info(f"开始从富途 {env} 环境同步持仓...")

    try:
        # 连接富途
        trader = FutuTrader(
            host=futu_config.get('host', '127.0.0.1'),
            port=futu_config.get('port', 11111),
            env=trd_env
        )

        if not trader.connect():
            logger.error("连接富途失败")
            return False

        # 获取持仓
        logger.info("正在查询富途持仓...")
        positions = trader.get_positions()
        logger.info(f"从富途获取到 {len(positions)} 条持仓记录")
        if positions:
            for pos in positions:
                logger.info(f"  - {pos}")

        if not positions:
            logger.info("富途持仓为空，清空数据库持仓")
            yaml_storage.clear_positions(trading_env)
            trader.disconnect()
            return True

        # 清空现有持仓并重新导入
        yaml_storage.clear_positions(trading_env)
        logger.info(f"已清空数据库 {env} 环境的现有持仓")

        # 导入持仓到数据库
        for pos in positions:
            stock_code = pos['stock_code']
            quantity = pos['quantity']
            cost_price = pos['cost_price']

            # 获取股票名称
            from mutifactor.data import get_hk_stock_name
            stock_name = get_hk_stock_name(stock_code)

            # 保存到数据库
            yaml_storage.save_position(
                stock_code=stock_code,
                stock_name=stock_name or stock_code,
                quantity=quantity,
                cost_price=cost_price,
                highest_price=cost_price,  # 初始化为成本价
                env=trading_env
            )
            logger.info(f"已同步: {stock_code} ({stock_name}) - {quantity}股 @ {cost_price}")

        trader.disconnect()
        logger.info(f"✅ 成功同步 {len(positions)} 条持仓到数据库")
        return True

    except Exception as e:
        logger.error(f"同步持仓失败: {e}", exc_info=True)
        return False


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description='从富途同步持仓到数据库')
    parser.add_argument('--env', choices=['SIMULATE', 'REAL'], default='SIMULATE',
                        help='交易环境: SIMULATE (模拟仓) 或 REAL (实盘)')
    args = parser.parse_args()

    success = sync_positions(args.env)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
