"""
美股交易系统统一入口
同时启动：Web服务(8899) + 抄底监控 + 止盈止损

用法：
    python run_all.py --dry-run     # 模拟模式
    python run_all.py --real        # 实盘
    python run_all.py --web-only    # 仅Web（调参用）
"""
import sys
import os
import time
import logging
import signal
import argparse
import yaml
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# ===== 日志配置 =====
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

def setup_logging():
    log_format = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter(log_format, date_fmt))
    root.addHandler(ch)
    today = datetime.now().strftime("%Y-%m-%d")
    fh = logging.FileHandler(os.path.join(LOG_DIR, f"quant_us_{today}.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter(log_format, date_fmt))
    root.addHandler(fh)

logger = logging.getLogger("run_all")


class UnifiedSystem:
    def __init__(self, dry_run=True, web_only=False):
        self.dry_run = dry_run
        self.web_only = web_only
        self.flask_thread = None
        self.flask_app = None
        self.running = True

        # 加载配置
        config_path = os.path.join(BASE_DIR, "config.yaml")
        with open(config_path, encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

    def start_flask(self):
        """后台线程启动 Flask"""
        from web.app import app
        self.flask_app = app
        logger.info("🌐 Web服务启动 http://127.0.0.1:8899")
        app.run(host="0.0.0.0", port=8899, debug=False, use_reloader=False)

    def start(self):
        logger.info("=" * 60)
        mode = "仅Web" if self.web_only else ("DRY-RUN" if self.dry_run else "REAL")
        logger.info(f"  🚀 美股交易系统启动  [{mode}]")
        logger.info("=" * 60)

        if self.web_only:
            # 仅 Web
            self.flask_thread = threading.Thread(target=self.start_flask, daemon=True)
            self.flask_thread.start()
            logger.info("✅ Web服务已启动 (仅Web模式，可调参)")
        else:
            # Web + 监控
            self.flask_thread = threading.Thread(target=self.start_flask, daemon=True)
            self.flask_thread.start()
            time.sleep(1)  # 等 Flask 先起来

            # 止盈止损
            from scripts.live_trading.chandelier_exit_manager import ChandelierExitManager
            self.exit_mgr = ChandelierExitManager(dry_run=self.dry_run)
            self.exit_mgr.start()
            logger.info("✅ 止盈止损管理器已启动")

            # 抄底监控
            from scripts.live_trading.dip_buy_monitor import DipBuyMonitor
            watch_list = self.config.get("dip_buy", {}).get("watch_list", [])
            self.dip_monitor = DipBuyMonitor(watch_list, self.config, dry_run=self.dry_run)
            self.dip_monitor.start()
            logger.info(f"✅ 抄底监控器已启动 ({', '.join(watch_list) or '无'})")

        logger.info("\n💡 Ctrl+C 优雅退出")
        logger.info("🌐 http://127.0.0.1:8899")

        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.shutdown()

    def shutdown(self):
        logger.info("\n🛑 收到退出信号...")
        self.running = False
        if not self.web_only:
            if hasattr(self, "dip_monitor"):
                self.dip_monitor.stop()
            if hasattr(self, "exit_mgr"):
                self.exit_mgr.stop()
        logger.info("✅ 系统已停止")


def main():
    setup_logging()

    parser = argparse.ArgumentParser(description="美股交易系统统一入口")
    parser.add_argument("--dry-run", action="store_true", help="模拟模式（默认）")
    parser.add_argument("--real", action="store_true", help="实盘模式")
    parser.add_argument("--web-only", action="store_true", help="仅启动Web服务（调参用）")
    args = parser.parse_args()

    if args.web_only:
        mode = "web-only"
        dry_run = True
    elif args.real:
        mode = "real"
        dry_run = False
    else:
        mode = "dry-run"
        dry_run = True

    system = UnifiedSystem(dry_run=dry_run, web_only=args.web_only)

    def signal_handler(sig, frame):
        system.shutdown()
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    system.start()


if __name__ == "__main__":
    main()
