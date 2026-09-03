"""
状态持久化模块 - 负责数据库操作和状态管理
"""
import logging
from datetime import datetime
from typing import Dict, Optional, List

from mutifactor.trading.exceptions import DatabaseError

logger = logging.getLogger(__name__)


class StatePersistence:
    """状态持久化管理器"""

    def __init__(self):
        self._yaml_storage = None
        self._trading_env = None
        self._init_db()

    def _init_db(self):
        """初始化数据库连接"""
        try:
            from mutifactor.infra.yaml_storage import yaml_storage, TradingEnv
            self._yaml_storage = yaml_storage
            self._trading_env = TradingEnv
        except (ImportError, ModuleNotFoundError) as e:
            logger.warning(f"数据库存储模块导入失败: {e}")
        except DatabaseError as e:
            logger.warning(f"数据库初始化失败: {e}")
        except (RuntimeError, OSError) as e:
            logger.error(f"数据库存储模块加载失败 - 系统错误: {type(e).__name__}: {e}", exc_info=True)
            raise

    def load_state(self, env: str = 'REAL') -> Optional[Dict]:
        """加载交易状态"""
        if not self._yaml_storage:
            return None
        try:
            trading_env = self._trading_env.REAL if env == 'REAL' else self._trading_env.SIMULATE
            return self._yaml_storage.load_trading_state(trading_env)
        except DatabaseError as e:
            logger.error(f"加载状态失败 - 数据库错误: {e}")
            return None
        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"加载状态失败 - 数据错误: {e}")
            return None
        except Exception as e:
            logger.error(f"加载状态失败 - 未知错误: {type(e).__name__}: {e}", exc_info=True)
            raise

    def save_state(self, positions: Dict, used_capital: float, capital: float,
                   last_buy_execution: int, env: str = 'REAL'):
        """保存交易状态"""
        if not self._yaml_storage:
            return
        try:
            trading_env = self._trading_env.REAL if env == 'REAL' else self._trading_env.SIMULATE
            self._yaml_storage.save_trading_state(
                positions=positions,
                used_capital=used_capital,
                capital=capital,
                last_buy_execution=last_buy_execution,
                env=trading_env
            )
        except DatabaseError as e:
            logger.error(f"保存状态失败 - 数据库错误: {e}")
        except (TypeError, ValueError) as e:
            logger.error(f"保存状态失败 - 数据错误: {e}")
        except Exception as e:
            logger.error(f"保存状态失败 - 未知错误: {type(e).__name__}: {e}", exc_info=True)
            raise

    def get_today_buy_quantity(self, env: str = 'REAL') -> int:
        """获取今日已买入股数"""
        if not self._yaml_storage:
            return 0
        try:
            trading_env = self._trading_env.REAL if env == 'REAL' else self._trading_env.SIMULATE
            return self._yaml_storage.get_today_buy_quantity(trading_env)
        except DatabaseError as e:
            logger.error(f"获取今日买入数量失败 - 数据库错误: {e}")
            return 0
        except (TypeError, ValueError) as e:
            logger.error(f"获取今日买入数量失败 - 数据错误: {e}")
            return 0
        except Exception as e:
            logger.error(f"获取今日买入数量失败 - 未知错误: {type(e).__name__}: {e}", exc_info=True)
            raise

    def get_today_bought_stocks(self, env: str = 'REAL') -> set:
        """获取今日已买入的股票代码集合"""
        if not self._yaml_storage:
            return set()
        try:
            trading_env = self._trading_env.REAL if env == 'REAL' else self._trading_env.SIMULATE
            return self._yaml_storage.get_today_bought_stocks(trading_env)
        except DatabaseError as e:
            logger.error(f"获取今日已买股票失败 - 数据库错误: {e}")
            return set()
        except (TypeError, ValueError) as e:
            logger.error(f"获取今日已买股票失败 - 数据错误: {e}")
            return set()
        except Exception as e:
            logger.error(f"获取今日已买股票失败: {type(e).__name__}: {e}", exc_info=True)
            return set()

    def save_position(self, stock_code: str, stock_name: str, quantity: int,
                      cost_price: float, highest_price: float, env: str = 'REAL',
                      manual: bool = False):
        """保存持仓"""
        if not self._yaml_storage:
            return
        try:
            trading_env = self._trading_env.REAL if env == 'REAL' else self._trading_env.SIMULATE
            self._yaml_storage.save_position(stock_code, stock_name, quantity,
                                           cost_price, highest_price, trading_env,
                                           manual=manual)
        except DatabaseError as e:
            logger.warning(f"保存持仓失败 - 数据库错误: {e}")
        except (TypeError, ValueError) as e:
            logger.warning(f"保存持仓失败 - 数据错误: {e}")
        except Exception as e:
            logger.warning(f"保存持仓失败: {type(e).__name__}: {e}")

    def get_positions(self, env: str = 'REAL') -> List[Dict]:
        """获取持仓列表"""
        if not self._yaml_storage:
            return []
        try:
            trading_env = self._trading_env.REAL if env == 'REAL' else self._trading_env.SIMULATE
            return self._yaml_storage.get_positions(trading_env)
        except Exception as e:
            logger.warning(f"获取持仓失败: {type(e).__name__}: {e}")
            return []

    def clear_positions(self, env: str = 'REAL'):
        """清空持仓"""
        if not self._yaml_storage:
            return
        try:
            trading_env = self._trading_env.REAL if env == 'REAL' else self._trading_env.SIMULATE
            self._yaml_storage.clear_positions(trading_env)
        except DatabaseError as e:
            logger.warning(f"清空持仓失败 - 数据库错误: {e}")
        except Exception as e:
            logger.warning(f"清空持仓失败: {type(e).__name__}: {e}")

    def save_trade(self, stock_code: str, stock_name: str, quantity: int,
                   price: float, direction: str, order_id: str, env: str = 'REAL'):
        """保存交易记录"""
        if not self._yaml_storage:
            return
        try:
            trading_env = self._trading_env.REAL if env == 'REAL' else self._trading_env.SIMULATE
            self._yaml_storage.save_trade(
                stock_code=stock_code,
                stock_name=stock_name,
                quantity=quantity,
                price=price,
                direction=direction,
                order_id=order_id,
                trade_time=datetime.now(),
                env=trading_env
            )
        except DatabaseError as e:
            logger.error(f"保存交易记录失败 - 数据库错误: {e}", exc_info=True)
        except (TypeError, ValueError) as e:
            logger.error(f"保存交易记录失败 - 数据错误: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"保存交易记录失败: {type(e).__name__}: {e}", exc_info=True)

    def save_capital(self, total_capital: float, used_capital: float, env: str = 'REAL'):
        """保存资金记录"""
        if not self._yaml_storage:
            return
        try:
            trading_env = self._trading_env.REAL if env == 'REAL' else self._trading_env.SIMULATE
            self._yaml_storage.save_capital_record(total_capital, used_capital, trading_env)
        except DatabaseError as e:
            logger.warning(f"保存资金记录失败 - 数据库错误: {e}")
        except (TypeError, ValueError) as e:
            logger.warning(f"保存资金记录失败 - 数据错误: {e}")
        except Exception as e:
            logger.warning(f"保存资金记录失败: {type(e).__name__}: {e}")

    def save_selection_results(self, stock_details: List[Dict], env: str = 'REAL'):
        """保存选股结果"""
        if not self._yaml_storage:
            return
        try:
            trading_env = self._trading_env.REAL if env == 'REAL' else self._trading_env.SIMULATE
            self._yaml_storage.save_selection_results(stock_details, trading_env)
        except DatabaseError as e:
            logger.warning(f"保存选股结果失败 - 数据库错误: {e}")
        except (TypeError, ValueError) as e:
            logger.warning(f"保存选股结果失败 - 数据错误: {e}")
        except Exception as e:
            logger.warning(f"保存选股结果失败: {type(e).__name__}: {e}")

    @property
    def yaml_storage(self):
        return self._yaml_storage
