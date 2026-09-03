"""
富途交易器实现
"""
import logging
import time
from typing import Dict, List, Tuple

from futu import (
    OpenSecTradeContext, TrdEnv, TrdSide, OrderType as FutuOrderType,
    RET_OK, SecurityFirm, TrdMarket
)
from futu import OpenQuoteContext  # 导入行情上下文

from mutifactor.trading.exceptions import (
    TradingError, ConnectionError, TimeoutError, OrderError
)
from mutifactor.trading.enums import OrderType
from mutifactor.trading.futu_constants import OrderStatus, get_order_status_name

logger = logging.getLogger(__name__)


class FutuTrader:
    """富途交易器"""

    def __init__(self, host: str = '127.0.0.1', port: int = 11111,
                 env: TrdEnv = TrdEnv.SIMULATE, quote_ctx=None,
                 market: TrdMarket = TrdMarket.HK):
        """
        初始化富途交易器

        Args:
            host: OpenD主机地址
            port: OpenD端口
            env: 交易环境 (SIMULATE 模拟, REAL 实盘)
            quote_ctx: 行情上下文(用于获取市场规则)
            market: 交易市场 (默认 HK)
        """
        self.host = host
        self.port = port
        self.env = env
        self.market = market
        self.trade_ctx = None
        self.quote_ctx = quote_ctx
        self._connected = False

    def connect(self) -> bool:
        """连接到富途OpenD"""
        try:
            self.trade_ctx = OpenSecTradeContext(
                filter_trdmarket=self.market,
                host=self.host,
                port=self.port,
                security_firm=SecurityFirm.FUTUSECURITIES
            )

            # REAL 模式依赖 GUI OpenD 手动解锁
            # 如果已通过 OpenD 界面右上角解锁，accinfo_query 直接成功
            # GUI OpenD（10.x）屏蔽了 unlock_trade() 接口，只能手动解锁
            if self.env == TrdEnv.REAL:
                ret, data = self.trade_ctx.accinfo_query(trd_env=self.env)
                if ret == RET_OK:
                    self._connected = True
                    logger.info("✅ REAL账户交易已解锁")
                    return True
                else:
                    logger.error(f"❌ REAL账户查询失败，账户可能未解锁")
                    return False
            else:
                # SIMULATE 模式直接连接，无需解锁
                self._connected = True
                logger.info("✅ SIMULATE账户交易已连接")
                return True

        except (OSError, IOError) as e:
            # 网络连接错误
            logger.error(f"连接富途服务器网络错误: {e}")
            raise ConnectionError(f"连接到富途交易服务器失败: {e}")
        except (RuntimeError, ValueError, TypeError) as e:
            # 其他运行时错误
            logger.error(f"连接异常: {type(e).__name__}: {e}", exc_info=True)
            raise ConnectionError(f"连接到富途交易服务器失败: {e}")
        except Exception as e:
            # 未知错误，记录并重新抛出
            logger.critical(f"连接未知错误: {type(e).__name__}: {e}", exc_info=True)
            raise

    def disconnect(self):
        """断开连接"""
        if self.trade_ctx:
            self.trade_ctx.close()
            self.trade_ctx = None
        if self.quote_ctx:
            self.quote_ctx.close()
            self.quote_ctx = None
        self._connected = False
        logger.info("已断开富途交易连接")

    def get_lot_size(self, stock_code: str) -> int:
        """
        获取股票的每手股数

        Args:
            stock_code: 股票代码 (格式: HK.00700)

        Returns:
            每手股数
        """
        # 优先从数据库获取
        try:
            from mutifactor.infra.yaml_storage import yaml_storage
            lot_size = yaml_storage.get_lot_size(stock_code)
            if lot_size:
                return lot_size
        except Exception:
            pass

        # 数据库没有，从富途API获取
        try:
            # 如果没有行情上下文,创建一个
            if self.quote_ctx is None:
                self.quote_ctx = OpenQuoteContext(host=self.host, port=self.port)

            # 获取市场快照,包含lot_size信息
            ret, data = self.quote_ctx.get_market_snapshot([stock_code])

            if ret == RET_OK and len(data) > 0:
                lot_size = int(data['lot_size'][0])
                logger.info(f"获取 {stock_code} 每手股数: {lot_size}")
                return lot_size
            else:
                logger.warning(f"获取 {stock_code} 市场快照失败,使用默认100股")
                return 100

        except (OSError, IOError) as e:
            # 网络错误
            logger.warning(f"获取 {stock_code} 每手股数网络错误: {e},使用默认100股")
            return 100
        except (KeyError, IndexError, ValueError) as e:
            # 数据解析错误
            logger.warning(f"获取 {stock_code} 每手股数数据解析错误: {e},使用默认100股")
            return 100
        except Exception as e:
            # 未知错误，不应静默处理
            logger.critical(f"获取 {stock_code} 每手股数未知错误: {type(e).__name__}: {e}", exc_info=True)
            raise

    def place_order(self, stock_code: str, quantity: int,
                   order_type: OrderType, side: str = 'buy',
                   price: float = None, timeout: int = 30) -> Tuple[str, float, int]:
        """
        下单

        Args:
            stock_code: 股票代码
            quantity: 数量
            order_type: 订单类型
            side: 买卖方向 ('buy'/'sell')
            price: 价格 (限价单需要)
            timeout: 超时时间(秒)

        Returns:
            (order_id, avg_price, dealt_qty) 订单ID、成交均价、成交数量
        """
        if not self._connected:
            raise ConnectionError("未连接到交易服务器")

        try:
            # 转换订单类型和方向
            futu_order_type = self._convert_order_type(order_type)
            trd_side = TrdSide.BUY if side.lower() == 'buy' else TrdSide.SELL

            # 下单（股票代码统一使用 HK. 前缀格式）
            ret, data = self.trade_ctx.place_order(
                price=price if price else 0,
                qty=quantity,
                code=stock_code,
                trd_side=trd_side,
                order_type=futu_order_type,
                trd_env=self.env
            )

            if ret != RET_OK:
                raise OrderError(f"下单失败: {data}", code=ret)

            order_id = data['order_id'][0]
            logger.info(f"下单成功: {order_id}, 股票: {stock_code}, 数量: {quantity}")

            # 等待订单确认
            avg_price, dealt_qty = self.wait_for_confirmation(order_id, timeout)
            return order_id, avg_price, dealt_qty

        except (OSError, IOError) as e:
            # 网络连接错误
            raise ConnectionError(f"下单网络错误: {e}")
        except (KeyError, TypeError, ValueError) as e:
            # 数据/参数错误
            raise OrderError(f"下单参数错误: {e}", stock_code=stock_code)
        except TradingError:
            # 已经是我们定义的异常，直接抛出
            raise
        except Exception as e:
            # 其他未预期错误
            logger.error(f"下单异常: {type(e).__name__}: {e}", exc_info=True)
            raise OrderError(f"下单异常: {e}", stock_code=stock_code)

    def wait_for_confirmation(self, order_id: str, timeout: int = 60) -> Tuple[float, int]:
        """
        等待订单确认

        Args:
            order_id: 订单ID
            timeout: 超时时间(秒)，默认60秒

        Returns:
            (avg_price, dealt_qty) 成交均价和成交数量
        """
        start_time = time.time()
        partial_timeout = 30  # 部分成交后等待超时时间
        partial_start_time = None
        last_status = None

        while time.time() - start_time < timeout:
            try:
                status, avg_price, dealt_qty = self.get_order_status(order_id)
            except OrderError as e:
                # 查询状态失败，可能是订单还在处理中，继续等待
                logger.debug(f"查询订单 {order_id} 状态失败: {e}, 继续等待...")
                time.sleep(0.5)
                continue

            # 记录状态变化
            status_name = get_order_status_name(status)
            if status != last_status:
                last_status_name = get_order_status_name(last_status) if last_status else 'None'
                logger.info(f"订单 {order_id} 状态变化: {last_status_name} -> {status_name}, 均价: {avg_price}, 成交: {dealt_qty}")
                last_status = status

            # 使用富途 OrderStatus 直接比较
            if status == OrderStatus.FILLED_ALL:
                logger.info(f"订单 {order_id} 完全成交, 均价: {avg_price}, 数量: {dealt_qty}")
                return avg_price, dealt_qty
            elif status == OrderStatus.FILLED_PART:
                # 记录部分成交开始时间
                if partial_start_time is None:
                    partial_start_time = time.time()
                    logger.info(f"订单 {order_id} 部分成交, 当前均价: {avg_price}, 数量: {dealt_qty}")

                # 部分成交超时，取消剩余订单
                if time.time() - partial_start_time > partial_timeout:
                    logger.warning(f"订单 {order_id} 部分成交超时({partial_timeout}秒), 尝试取消剩余订单")
                    try:
                        self.cancel_order(order_id)
                        logger.info(f"订单 {order_id} 剩余订单已取消, 最终成交均价: {avg_price}, 数量: {dealt_qty}")
                        return avg_price, dealt_qty
                    except (OSError, IOError) as e:
                        logger.error(f"取消订单网络错误: {e}, 继续等待成交")
                        partial_start_time = time.time()
                    except OrderError:
                        raise
                    except Exception as e:
                        logger.error(f"取消订单异常: {type(e).__name__}: {e}", exc_info=True)
                        partial_start_time = time.time()  # 重置计时器
            elif status in [OrderStatus.CANCELLED_ALL, OrderStatus.CANCELLED_PART, OrderStatus.FAILED, OrderStatus.DISABLED]:
                # 订单失败，检查是否有部分成交
                if avg_price > 0 and dealt_qty > 0:
                    logger.warning(f"订单 {order_id} 已失败但有部分成交, 均价: {avg_price}, 数量: {dealt_qty}")
                    return avg_price, dealt_qty
                raise OrderError(f"订单 {order_id} 失败", order_id=order_id)

            time.sleep(1)  # 每秒检查一次

        # 超时后检查订单状态
        logger.warning(f"订单 {order_id} 确认超时({timeout}秒), 检查最终状态")
        try:
            status, avg_price, dealt_qty = self.get_order_status(order_id)
            logger.info(f"订单 {order_id} 最终状态: {get_order_status_name(status)}, 均价: {avg_price}, 数量: {dealt_qty}")

            if status == OrderStatus.FILLED_ALL:
                logger.info(f"订单 {order_id} 最终成交, 均价: {avg_price}, 数量: {dealt_qty}")
                return avg_price, dealt_qty
            elif status == OrderStatus.FILLED_PART and avg_price > 0:
                # 超时时有部分成交，返回部分成交价格
                logger.warning(f"订单 {order_id} 超时但有部分成交, 均价: {avg_price}, 数量: {dealt_qty}")
                return avg_price, dealt_qty
            else:
                # 尝试取消超时订单
                logger.warning(f"订单 {order_id} 超时未成交，尝试取消")
                try:
                    self.cancel_order(order_id)
                    logger.info(f"订单 {order_id} 已取消")
                except (OSError, IOError) as e:
                    logger.error(f"取消超时订单网络错误: {e}")
                except OrderError as e:
                    logger.error(f"取消超时订单失败: {e}")
                except Exception as e:
                    logger.error(f"取消超时订单异常: {type(e).__name__}: {e}", exc_info=True)

                raise TimeoutError(f"订单 {order_id} 确认超时", order_id=order_id)
        except TimeoutError:
            raise
        except OrderError:
            raise
        except (OSError, IOError) as e:
            raise TimeoutError(f"订单 {order_id} 确认超时(网络错误): {e}", order_id=order_id)
        except Exception as e:
            logger.error(f"订单确认异常: {type(e).__name__}: {e}", exc_info=True)
            raise TimeoutError(f"订单 {order_id} 确认超时: {e}", order_id=order_id)

    def get_order_status(self, order_id: str) -> Tuple[OrderStatus, float, int]:
        """
        获取订单状态

        Args:
            order_id: 订单ID

        Returns:
            (status, avg_price, dealt_qty) 状态、成交均价、成交数量
        """
        if not self._connected:
            raise ConnectionError("未连接到交易服务器")

        try:
            ret, data = self.trade_ctx.order_list_query(
                order_id=order_id,
                trd_env=self.env
            )

            if ret != RET_OK:
                raise OrderError(f"查询订单状态失败: {data}", order_id=order_id)

            if len(data) == 0:
                raise OrderError(f"订单不存在: {order_id}", order_id=order_id)

            order_data = data.iloc[0]
            # 直接使用富途返回的 OrderStatus 枚举
            status = order_data['order_status']
            # 安全获取成交均价，富途API字段名为 dealt_avg_price
            avg_price_raw = order_data.get('dealt_avg_price', None)
            if avg_price_raw is None or avg_price_raw == 0:
                # 尝试其他可能的字段名
                avg_price_raw = order_data.get('avg_price', None)
            avg_price = float(avg_price_raw) if avg_price_raw is not None else 0.0
            # 获取成交数量
            dealt_qty_raw = order_data.get('dealt_qty', None)
            if dealt_qty_raw is None:
                dealt_qty_raw = order_data.get('qty', 0)
            dealt_qty = int(dealt_qty_raw) if dealt_qty_raw is not None else 0

            return status, avg_price, dealt_qty

        except (OSError, IOError) as e:
            raise ConnectionError(f"查询订单状态网络错误: {e}")
        except (KeyError, IndexError, TypeError) as e:
            raise OrderError(f"查询订单状态数据错误: {e}", order_id=order_id)
        except TradingError:
            raise
        except Exception as e:
            logger.error(f"查询订单状态异常: {type(e).__name__}: {e}", exc_info=True)
            raise OrderError(f"查询订单状态异常: {e}", order_id=order_id)

    def cancel_order(self, order_id: str) -> bool:
        """
        取消订单

        Args:
            order_id: 订单ID

        Returns:
            是否成功取消
        """
        if not self._connected:
            raise ConnectionError("未连接到交易服务器")

        try:
            ret, data = self.trade_ctx.modify_order(
                modify_order_op=3,  # 取消订单
                order_id=order_id,
                qty=0,
                price=0,
                trd_env=self.env
            )

            if ret != RET_OK:
                logger.warning(f"取消订单失败: {data}")
                raise OrderError(f"取消订单失败: {data}", order_id=order_id)

            logger.info(f"订单 {order_id} 取消成功")
            return True

        except (OSError, IOError) as e:
            raise ConnectionError(f"取消订单网络错误: {e}")
        except TradingError:
            raise
        except Exception as e:
            logger.error(f"取消订单异常: {type(e).__name__}: {e}", exc_info=True)
            raise OrderError(f"取消订单异常: {e}", order_id=order_id)

    def get_positions(self) -> List[Dict]:
        """获取持仓列表"""
        if not self._connected:
            raise ConnectionError("未连接到交易服务器")

        try:
            ret, data = self.trade_ctx.position_list_query(trd_env=self.env)
            if ret != RET_OK:
                raise OrderError(f"查询持仓失败: {data}")

            positions = []
            for _, row in data.iterrows():
                # 过滤美股持仓
                if row['code'].startswith('US.'):
                    continue
                positions.append({
                    'stock_code': row['code'],
                    'quantity': int(row['qty']),
                    'cost_price': float(row['cost_price']),
                    'market_value': float(row['market_val'])
                })

            return positions

        except (OSError, IOError) as e:
            raise ConnectionError(f"查询持仓网络错误: {e}")
        except (KeyError, TypeError, ValueError) as e:
            raise OrderError(f"查询持仓数据错误: {e}")
        except TradingError:
            raise
        except Exception as e:
            logger.error(f"查询持仓异常: {type(e).__name__}: {e}", exc_info=True)
            raise OrderError(f"查询持仓异常: {e}")

    def get_account_info(self) -> Dict:
        """获取账户信息"""
        if not self._connected:
            raise ConnectionError("未连接到交易服务器")

        try:
            ret, data = self.trade_ctx.accinfo_query(trd_env=self.env)
            if ret != RET_OK:
                raise OrderError(f"查询账户信息失败: {data}")

            # 港股优先读取 hk_cash，美股优先读取 us_cash，兜底读 cash
            cash = 0.0
            if 'hk_cash' in data.columns:
                cash = float(data['hk_cash'][0])
            elif 'us_cash' in data.columns:
                cash = float(data['us_cash'][0])
            else:
                cash = float(data['cash'][0]) if len(data) > 0 and 'cash' in data.columns else 0.0

            return {
                'cash': cash,
                'total_assets': float(data['total_assets'][0]),
                'market_value': float(data['market_val'][0])
            }

        except (OSError, IOError) as e:
            raise ConnectionError(f"查询账户信息网络错误: {e}")
        except (KeyError, TypeError, ValueError) as e:
            raise OrderError(f"查询账户信息数据错误: {e}")
        except TradingError:
            raise
        except Exception as e:
            logger.error(f"查询账户信息异常: {type(e).__name__}: {e}", exc_info=True)
            raise OrderError(f"查询账户信息异常: {e}")

    def _convert_order_type(self, order_type: OrderType) -> FutuOrderType:
        """转换订单类型"""
        mapping = {
            OrderType.MARKET: FutuOrderType.MARKET,
            OrderType.LIMIT: FutuOrderType.NORMAL,
        }
        return mapping.get(order_type, FutuOrderType.MARKET)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()