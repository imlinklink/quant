"""
进程管理工具 - 用于启动、停止、查询交易进程
"""
import os
import json
import signal
import logging
from pathlib import Path
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class ProcessManager:
    """进程管理器"""

    PID_FILE = Path(".comate/live_trading.pid")
    INFO_FILE = Path(".comate/live_trading.info")

    def __init__(self):
        self.pid_file = self.PID_FILE
        self.info_file = self.INFO_FILE
        self.pid_file.parent.mkdir(exist_ok=True)

    def save_process_info(self, pid: int, info: Dict):
        """保存进程信息"""
        try:
            data = {
                'pid': pid,
                'info': info,
                'start_time': os.path.getctime(__file__)  # 近似启动时间
            }

            with open(self.pid_file, 'w') as f:
                f.write(str(pid))

            with open(self.info_file, 'w') as f:
                json.dump(data, f, indent=2)

            logger.info(f"进程信息已保存: PID={pid}")
            return True

        except Exception as e:
            logger.error(f"保存进程信息失败: {e}")
            return False

    def get_process_info(self) -> Optional[Dict]:
        """获取进程信息"""
        try:
            if not self.info_file.exists():
                return None

            with open(self.info_file, 'r') as f:
                return json.load(f)

        except Exception as e:
            logger.error(f"读取进程信息失败: {e}")
            return None

    def is_process_running(self, pid: int) -> bool:
        """检查进程是否在运行"""
        try:
            os.kill(pid, 0)  # 发送0信号检查进程是否存在
            return True
        except OSError:
            return False
        except Exception as e:
            logger.error(f"检查进程状态失败: {e}")
            return False

    def stop_process(self) -> bool:
        """停止运行中的进程"""
        info = self.get_process_info()
        if not info:
            logger.warning("没有找到运行中的进程信息")
            return False

        pid = info['pid']
        if not self.is_process_running(pid):
            logger.warning(f"进程 {pid} 不在运行中")
            self.cleanup()
            return False

        try:
            # 发送终止信号
            os.kill(pid, signal.SIGTERM)
            logger.info(f"已发送停止信号到进程 {pid}")

            # 清理文件
            self.cleanup()
            return True

        except Exception as e:
            logger.error(f"停止进程失败: {e}")
            return False

    def cleanup(self):
        """清理进程文件"""
        try:
            if self.pid_file.exists():
                self.pid_file.unlink()
            if self.info_file.exists():
                self.info_file.unlink()
            logger.info("进程文件已清理")
        except Exception as e:
            logger.error(f"清理进程文件失败: {e}")


# 全局实例
process_manager = ProcessManager()