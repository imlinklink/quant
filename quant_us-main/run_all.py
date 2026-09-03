"""
美股交易系统统一入口
同时启动：Web服务(8899) + 抄底监控 + 止盈止损

用法：
    python run_all.py --dry-run     # 模拟模式
    python run_all.py --real        # 实盘
    python run_all.py --web-only    # 仅Web（调参用）

人工确认模式（config.yaml -> trading.live_trading.human_approval.enabled: true）：
    买入信号只推送到 http://127.0.0.1:8899/approvals，
    页面显示规则理由 + 大模型判定，点「下单」才真正执行。
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

    def _prepare_approval(self):
        """
        人工确认：创建/复用与 Web 共用的提案存储，并注入 Flask。

        启用后 DipBuyMonitor 只推信号不自动下单，由确认页点单执行。
        """
        from web import app as webapp

        ha = self.config.get('trading', {}).get('live_trading', {}).get('human_approval', {})
        enabled = bool(ha.get('enabled', False))
        webapp.approval_enabled = enabled

        if enabled and webapp.approval_store is None:
            try:
                from scripts.live_trading.approval.proposal_store import ProposalStore
                webapp.approval_store = ProposalStore(
                    ttl_seconds=float(ha.get('proposal_ttl_seconds', 180))
                )
            except Exception as e:
                logger.error(f"[人工确认] 存储初始化失败，系统将暂停买入（fail-closed）: {e}")

        self.approval_store = webapp.approval_store
        if self.dry_run:
            webapp.approval_env = 'DRY-RUN'
        else:
            webapp.approval_env = str(
                self.config.get('live_manager', {}).get('trd_env', 'SIMULATE')
            )

        if enabled:
            logger.warning(
                "[人工确认] 已启用：买入信号只推送到确认页，点「下单」才执行。"
                "页面 http://127.0.0.1:8899/approvals"
            )

    def start(self):
        logger.info("=" * 60)
        mode = "仅Web" if self.web_only else ("DRY-RUN" if self.dry_run else "REAL")
        logger.info(f"  🚀 美股交易系统启动  [{mode}]")
        logger.info("=" * 60)

        self._prepare_approval()

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
            self.dip_monitor = DipBuyMonitor(
                watch_list,
                self.config,
                dry_run=self.dry_run,
                approval_store=self.approval_store,
            )
            self.dip_monitor.start()
            logger.info(f"✅ 抄底监控器已启动 ({', '.join(watch_list) or '无'})")

            # 把 LLM 实际可用状态同步到 Web（便于页面显示徽标）
            try:
                from web import app as webapp
                webapp.approval_llm_enabled = bool(getattr(self.dip_monitor, 'llm_enabled', False))
                webapp.dip_monitor_ref = self.dip_monitor
            except Exception:
                pass

        logger.info("\n💡 Ctrl+C 优雅退出")
        logger.info("🌐 http://127.0.0.1:8899")

        # ChandelierExitManager 启动时会注册自己的信号处理器并覆盖主控，
        # 这里重新接管，保证 Ctrl+C / TERM 能整体优雅退出
        def _unified_signal(sig, _frame):
            logger.info("🛑 收到退出信号，正在整体停止...")
            self.shutdown()
            sys.exit(0)

        signal.signal(signal.SIGINT, _unified_signal)
        signal.signal(signal.SIGTERM, _unified_signal)

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
