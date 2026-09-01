"""
美股完整交易系统

功能：
1. 抄底买入监控（DipBuyMonitor）：订阅股票，出现抄底信号自动买入
2. 持仓止盈止损（ChandelierExitManager）：买入后转入4阶段止盈止损管理

用法：
    python scripts/live_trading/run_manager.py --watch US.MU,US.AAPL,US.TSLA --dry-run
    python scripts/live_trading/run_manager.py --watch US.MU,US.AAPL --real
"""
import sys
import os
import time
import logging
import argparse
import yaml
from datetime import datetime
from typing import List, Optional, Dict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE_DIR)

# ===== 日志配置 =====
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def setup_logging():
    """配置日志：控制台输出 + 按日期滚动文件"""
    log_format = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    
    # 清除已有handler
    root_logger.handlers.clear()

    # 控制台
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(log_format, date_fmt))
    root_logger.addHandler(console)

    # 文件
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(LOG_DIR, f"quant_us_{today}.log")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, date_fmt))
    root_logger.addHandler(file_handler)

    return log_file


logger = logging.getLogger("__main__")


class QuantUSTradingSystem:
    """美股完整交易系统"""
    
    def __init__(self, watch_codes: List[str], config: Dict, dry_run: bool = True):
        """
        Args:
            watch_codes: 监控股票列表
            config: 配置字典
            dry_run: 模拟模式
        """
        self.watch_codes = watch_codes
        self.config = config
        self.dry_run = dry_run
        
        # 两个核心模块
        self.dip_monitor = None      # 抄底买入
        self.exit_manager = None     # 止盈止损
        
        # 状态
        self._running = False
        
    def start(self):
        """启动完整系统"""
        if self._running:
            return
        
        logger.info("=" * 70)
        logger.info("  🚀 美股完整交易系统启动")
        logger.info("=" * 70)
        logger.info(f"  监控股票: {', '.join(self.watch_codes)}")
        logger.info(f"  模式:     {'DRY-RUN（模拟）' if self.dry_run else 'REAL（实盘）'}")
        logger.info("=" * 70)
        
        # 1. 启动止盈止损管理器（监控已有持仓）
        from scripts.live_trading.chandelier_exit_manager import ChandelierExitManager
        self.exit_manager = ChandelierExitManager(dry_run=self.dry_run)
        self.exit_manager.start()
        logger.info("✅ 止盈止损管理器已启动")
        
        # 2. 启动抄底买入监控器
        from scripts.live_trading.dip_buy_monitor import DipBuyMonitor
        self.dip_monitor = DipBuyMonitor(
            self.watch_codes, 
            self.config, 
            dry_run=self.dry_run
        )
        self.dip_monitor.start()
        logger.info("✅ 抄底买入监控器已启动")
        
        self._running = True
        logger.info("\n💡 提示：按 Ctrl+C 可优雅退出程序")
        
    def stop(self):
        """停止完整系统"""
        if not self._running:
            return
        
        logger.info("\n" + "=" * 70)
        logger.info("  🛑 停止交易系统...")
        logger.info("=" * 70)
        
        # 先停止买入（避免买入后无法管理）
        if self.dip_monitor:
            logger.info("  停止抄底买入监控器...")
            self.dip_monitor.stop()
        
        # 再停止止盈止损
        if self.exit_manager:
            logger.info("  停止止盈止损管理器...")
            self.exit_manager.stop()
        
        self._running = False
        logger.info("✅ 系统已完全停止")


def main():
    log_file = setup_logging()

    parser = argparse.ArgumentParser(description="美股完整交易系统")
    parser.add_argument("--watch", help="监控股票列表，逗号分隔（可选，默认从配置文件读取）")
    parser.add_argument("--dry-run", action="store_true", help="模拟模式（不实际下单）")
    parser.add_argument("--real", action="store_true", help="实盘模式（谨慎使用）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--interval", type=int, help="抄底检查间隔（秒，可选，默认从配置读取）")
    args = parser.parse_args()

    # 加载配置
    config_path = os.path.join(BASE_DIR, args.config)
    with open(config_path, encoding='utf-8') as f:
        config = yaml.safe_load(f)

    # 确定模式
    dry_run = not args.real
    if args.real:
        logger.warning("⚠️  实盘模式已启用，将真实下单！")

    # 股票列表：命令行优先，否则从配置读取
    if args.watch:
        codes = [c.strip().upper() for c in args.watch.split(',') if c.strip()]
    else:
        watch_list = config.get('dip_buy', {}).get('watch_list', [])
        if not watch_list:
            logger.error("❌ 未配置监控股票列表，请在 config.yaml 的 dip_buy.watch_list 中添加")
            sys.exit(1)
        codes = [c.strip().upper() for c in watch_list if c.strip()]

    if not codes:
        logger.error("❌ 监控股票列表为空")
        sys.exit(1)

    # 确保配置中有 dip_buy 节点
    if 'dip_buy' not in config:
        config['dip_buy'] = {}
    
    # 命令行 interval 覆盖配置
    if args.interval:
        config['dip_buy']['check_interval'] = args.interval

    # 启动系统
    system = QuantUSTradingSystem(codes, config, dry_run=dry_run)

    try:
        system.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("\n👋 用户触发退出")
    except Exception as e:
        logger.error(f"❌ 程序异常: {e}", exc_info=True)
    finally:
        system.stop()
        logger.info("🔚 程序已完全退出")


if __name__ == "__main__":
    main()
