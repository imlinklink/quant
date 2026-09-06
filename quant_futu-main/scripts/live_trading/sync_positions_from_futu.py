"""
从富途同步持仓数据到数据库
用于恢复误删的 positions 表数据
"""
import os
import sys
import logging
import yaml

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

        # 备份当前 env 的 positions 表（清空式重建前先留后路，
        # 中途崩溃也不至于连旧数据都丢）
        old_rows = []
        backup_path = None
        try:
            old_rows = yaml_storage.get_positions(env=trading_env) or []
            if old_rows:
                import time as _time
                base = yaml_storage._get_filepath('positions')
                backup_path = os.path.join(
                    os.path.dirname(base),
                    f"positions_backup_{env.lower()}_{int(_time.time())}.yaml"
                )
                with open(backup_path, 'w', encoding='utf-8') as f:
                    yaml.dump({'positions': old_rows}, f,
                              allow_unicode=True, sort_keys=False)
                logger.warning(f"已备份原持仓 {len(old_rows)} 条到 {backup_path}")
        except Exception as e:
            logger.warning(f"备份原持仓失败（继续，风险自担）: {e}")

        # 从 trades 表推导“系统买入过”的代码及最早买入时间：
        # 没有旧记录时用来区分手动仓（策略仓计入资金、参与止盈止损）
        bought_map = {}
        try:
            trades = yaml_storage.get_trades(env=trading_env) or []
            for t in trades:
                code = t.get('stock_code')
                trade_type = str(t.get('trade_type', '')).upper()
                if not code or not trade_type.startswith('BUY'):
                    continue
                ts = str(t.get('trade_time') or '')
                if code not in bought_map or ts < bought_map[code]:
                    bought_map[code] = ts
        except Exception as e:
            logger.warning(f"读取交易记录失败（手动/策略判定将退回旧表）: {e}")

        old_by_code = {r.get('stock_code'): r for r in old_rows if r.get('stock_code')}

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
            old = old_by_code.get(stock_code, {})

            # 获取股票名称
            from mutifactor.data import get_hk_stock_name
            stock_name = get_hk_stock_name(stock_code)

            # 保留/推导关键状态，避免恢复后持仓“失去买入日期与历史最高价”：
            # - buy_time：旧表 > trades 最早 BUY > 空
            # - manual：旧表标记 > 按是否有系统 BUY 判定（无记录 = 手动买入）
            # - highest_price：旧表历史最高（不低于成本）> 成本价
            buy_time = str(old.get('buy_time') or bought_map.get(stock_code) or '')
            if old:
                manual = bool(old.get('manual'))
            else:
                manual = stock_code not in bought_map
            try:
                db_highest = float(old.get('highest_price') or 0)
            except (TypeError, ValueError):
                db_highest = 0.0
            highest_price = max(cost_price, db_highest)

            # 保存到数据库
            yaml_storage.save_position(
                stock_code=stock_code,
                stock_name=stock_name or stock_code,
                quantity=quantity,
                cost_price=cost_price,
                highest_price=highest_price,
                manual=manual,
                buy_time=buy_time,
                env=trading_env,
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
