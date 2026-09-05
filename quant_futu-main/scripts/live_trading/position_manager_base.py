"""
持仓管理基类 - 统一港股和美股持仓管理
"""
import logging
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Tuple

from mutifactor.trading import FutuTrader, OrderType
from mutifactor.base_market_adapter import MarketAdapter
from mutifactor.data.base_fetcher import MarketType

logger = logging.getLogger(__name__)

# ANSI颜色常量配置
class ColorConstants:
    """终端颜色常量"""
    RESET = '\033[0m'
    
    # 现价颜色 - 青色
    CURRENT_PRICE = '\033[96m'
    
    # 盈亏颜色
    PROFIT_POSITIVE = '\033[92m'  # 绿色 - 盈利
    PROFIT_NEGATIVE = '\033[91m'  # 红色 - 亏损
    
    # 状态颜色
    STATUS_EXIT = ''  # 退出状态使用emoji，不需要额外颜色
    STATUS_OBSERVE = ''


class PositionManagerBase(ABC):
    """持仓管理基类 - 支持港股和美股"""

    def __init__(self, config: Dict, trader: FutuTrader,
                 state_persistence, price_fetcher, market_type: str,
                 live_manager=None):
        """
        初始化持仓管理器

        Args:
            config: 配置字典
            trader: 交易器
            state_persistence: 状态持久化对象
            price_fetcher: 价格获取器
            market_type: 市场类型 'HK'
            live_manager: 实盘管理器引用,用于获取K线缓存
        """
        self.config = config
        self.trader = trader
        self.state_persistence = state_persistence
        self.price_fetcher = price_fetcher
        self.market_type = market_type
        self.live_manager = live_manager

        # 策略资金
        self.strategy_capital = float(self._get_capital_config())
        self.strategy_positions: Dict[str, Dict] = {}
        self.strategy_used_capital = 0.0

        # 止损冷却期记录 {stock_code: sell_date_str}，冷却期内不重买
        self.recently_stopped: Dict[str, str] = {}
        self.stop_loss_cooldown_days = config.get('risk', {}).get('stop_loss_cooldown_days', 20)

        # 市场适配器 - 用于计算交易费用
        market_type_enum = MarketType.HK
        self.market_adapter = MarketAdapter(market_type=market_type_enum)

        # 锁
        self._position_lock = threading.Lock()

        # 订单超时
        trading_config = config.get('trading', {}).get('live_trading', {})
        self.order_timeout = trading_config.get('order_timeout', 60)
        self.position_check_interval = trading_config.get('position_check_interval', 30)

    # ==================== 止损冷却期管理 ====================
    
    def is_in_cooldown(self, stock_code: str, current_date: str = None) -> bool:
        """
        检查股票是否在冷却期内
        
        Args:
            stock_code: 股票代码
            current_date: 当前日期 (YYYY-MM-DD)，None则自动获取
            
        Returns:
            True if in cooldown, False otherwise
        """
        if stock_code not in self.recently_stopped:
            return False
        
        if current_date is None:
            current_date = datetime.now().strftime('%Y-%m-%d')
        
        stop_date_str = self.recently_stopped[stock_code]
        if not stop_date_str:
            return False
        
        try:
            stop_date = datetime.strptime(stop_date_str, '%Y-%m-%d').date()
            current_dt = datetime.strptime(current_date, '%Y-%m-%d').date()
            days_passed = (current_dt - stop_date).days
            
            if days_passed >= self.stop_loss_cooldown_days:
                # 冷却期已过，移除记录
                del self.recently_stopped[stock_code]
                logger.debug(f"[{self.market_type}] 冷却期: {stock_code} 冷却期已过期，移除")
                return False
            return True
        except (ValueError, TypeError) as e:
            logger.warning(f"[{self.market_type}] 冷却期日期解析错误 {stock_code}: {stop_date_str}, {e}")
            return False

    # ==================== 周末演示模式（只进内存，不碰券商/持久化） ====================

    def demo_open_position(self, stock_code: str, quantity: int, price: float,
                           entry_mode: str = 'momentum') -> bool:
        """周末演示：登记一笔“演示持仓”（demo=True）。
        只写入 strategy_positions 与监控快照，不保存 trading_state/positions 表，
        避免污染周一恢复状态；正常检查与券商同步会自动跳过/清理演示仓。
        """
        if not stock_code or quantity <= 0 or price <= 0:
            return False
        with self._position_lock:
            if stock_code in self.strategy_positions:
                return False
            self.strategy_positions[stock_code] = {
                'quantity': int(quantity),
                'cost_price': float(price),
                'highest_price': float(price),
                'buy_time': datetime.now().isoformat(),
                'entry_mode': entry_mode or 'momentum',
                'order_id': 'DEMO_WEEKEND',
                'demo': True,
            }
        logger.warning(
            f"[{self.market_type}] [周末演示] 登记演示持仓: {stock_code} x {quantity} "
            f"@{price:.3f}（不写券商、不落恢复状态）"
        )
        if self.live_manager is not None and hasattr(self.live_manager, 'update_positions_snapshot'):
            try:
                stock_name = self._get_stock_name(stock_code)
                self.live_manager.update_positions_snapshot(stock_code, {
                    'stock_code': stock_code,
                    'stock_name': stock_name,
                    'manual': False,
                    'entry_mode': entry_mode or 'momentum',
                    'quantity': int(quantity),
                    'cost_price': float(price),
                    'price': float(price),
                    'highest_price': float(price),
                    'return_pct': 0.0,
                    'profit_amount': 0.0,
                    'atr': 0.0,
                    'take_profit_price': None,
                    'stop_loss_price': None,
                    'should_exit': False,
                    'reason': '周末演示持仓（未连接券商，仅本地模拟）',
                    'status_text': '📝 演示持仓（观察中）',
                    'demo': True,
                })
            except Exception as e:
                logger.warning(f"[{self.market_type}] 演示持仓快照推送失败: {e}")
        return True

    def demo_close_position(self, stock_code: str, exit_price: float) -> bool:
        """周末演示：按演示价平掉一笔演示持仓，记录账本（dry_run），不落恢复状态。"""
        with self._position_lock:
            pos = self.strategy_positions.get(stock_code)
            if not pos or not pos.get('demo'):
                return False
            quantity = int(pos.get('quantity', 0))
            cost_price = float(pos.get('cost_price', 0))
            entry_mode = pos.get('entry_mode', 'momentum')
            self.strategy_positions.pop(stock_code, None)
        exit_price = float(exit_price)
        pnl_pct = (exit_price - cost_price) / cost_price if cost_price > 0 else 0.0
        logger.warning(
            f"[{self.market_type}] [周末演示] 平仓 {stock_code} x {quantity} "
            f"@{exit_price:.3f}，模拟盈亏 {pnl_pct * 100:+.2f}%"
        )
        try:
            self._ledger_record(
                'position_closed',
                stock_code=stock_code,
                order_id='DEMO_WEEKEND',
                quantity=quantity,
                cost_price=cost_price,
                exit_price=exit_price,
                pnl_pct=pnl_pct,
                reason='weekend_demo_exit',
                entry_mode=entry_mode,
                dry_run=True,
                note='周末演示：未连接券商',
            )
        except Exception:
            pass
        if self.live_manager is not None and hasattr(self.live_manager, 'prune_positions_snapshot'):
            self.live_manager.prune_positions_snapshot()
        return True

    def add_cooldown(self, stock_code: str, stop_date: str = None):
        """
        添加股票到冷却期
        
        Args:
            stock_code: 股票代码
            stop_date: 止损日期 (YYYY-MM-DD)，None则使用今天
        """
        if stop_date is None:
            stop_date = datetime.now().strftime('%Y-%m-%d')
        self.recently_stopped[stock_code] = stop_date
        logger.info(f"[{self.market_type}] 冷却期: {stock_code} 加入冷却期至 {stop_date}，冷却期{self.stop_loss_cooldown_days}天")

    def cleanup_cooldown(self, current_date: str = None):
        """
        清理已过期的冷却期记录
        
        Args:
            current_date: 当前日期 (YYYY-MM-DD)，None则自动获取
        """
        if current_date is None:
            current_date = datetime.now().strftime('%Y-%m-%d')
        
        expired = []
        for stock_code, stop_date_str in self.recently_stopped.items():
            if not stop_date_str:
                expired.append(stock_code)
                continue
            try:
                stop_date = datetime.strptime(stop_date_str, '%Y-%m-%d').date()
                current_dt = datetime.strptime(current_date, '%Y-%m-%d').date()
                days_passed = (current_dt - stop_date).days
                if days_passed >= self.stop_loss_cooldown_days:
                    expired.append(stock_code)
            except (ValueError, TypeError):
                expired.append(stock_code)
        
        for stock_code in expired:
            del self.recently_stopped[stock_code]
            logger.info(f"[{self.market_type}] 冷却期: {stock_code} 冷却期已过期，移除")

    def get_cooldown_status(self) -> Dict[str, int]:
        """
        获取所有冷却期股票的剩余天数
        
        Returns:
            {stock_code: remaining_days}
        """
        current_date = datetime.now().strftime('%Y-%m-%d')
        result = {}
        for stock_code, stop_date_str in self.recently_stopped.items():
            if not stop_date_str:
                continue
            try:
                stop_date = datetime.strptime(stop_date_str, '%Y-%m-%d').date()
                current_dt = datetime.strptime(current_date, '%Y-%m-%d').date()
                days_passed = (current_dt - stop_date).days
                remaining = self.stop_loss_cooldown_days - days_passed
                if remaining > 0:
                    result[stock_code] = remaining
            except (ValueError, TypeError):
                pass
        return result

    @abstractmethod
    def _get_capital_config(self) -> float:
        """获取资金配置（子类实现）"""
        pass

    @abstractmethod
    def _load_positions_from_db(self):
        """从数据库加载持仓（子类实现）"""
        pass

    @abstractmethod
    def _save_positions_to_db(self):
        """保存持仓到数据库（子类实现）"""
        pass

    @abstractmethod
    def _save_trade_record(self, stock_code: str, quantity: int, price: float,
                          direction: str, order_id: str):
        """保存交易记录（子类实现）"""
        pass

    @abstractmethod
    def _get_stock_name(self, stock_code: str) -> str:
        """获取股票名称（子类实现）"""
        pass

    @abstractmethod
    def _adjust_quantity_for_capital(self, stock: Dict, scale: float):
        """根据资金调整数量（子类实现，港股需考虑手数）"""
        pass

    @abstractmethod
    def _check_exit_signals_impl(self, stock_code: str, quantity: int,
                                 cost_price: float, price: float,
                                 highest_price: float,
                                 entry_mode: str = 'bottom_fish') -> Tuple[bool, str, float, float, float]:
        """检查止盈止损信号的具体实现（子类实现）
        
        Returns:
            (should_exit, reason, atr, take_profit_price, stop_loss_price)
        """
        pass

    def _ledger_record(self, event_type: str, **fields):
        """写入评估账本（写失败不影响交易）。"""
        try:
            from scripts.live_trading.decision_ledger import ledger
            env = str(self.config.get('trading', {}).get('env', 'SIMULATE'))
            ledger.record(event_type, env=env, market_type=self.market_type, **fields)
        except Exception:
            pass

    def _get_cached_kline_data(self, stock_code: str):
        """获取缓存的K线数据"""
        if self.live_manager and hasattr(self.live_manager, 'get_cached_kline_data'):
            return self.live_manager.get_cached_kline_data(stock_code)
        return None

    def load_positions(self):
        """从数据库加载持仓"""
        try:
            self._load_positions_from_db()
            # 加载后立即写入 positions 表（含 manual 字段）
            self.save_positions()
        except Exception as e:
            logger.error(f"[{self.market_type}] 加载持仓失败: {e}", exc_info=True)

    def save_positions(self):
        """保存持仓到数据库"""
        try:
            self._save_positions_to_db()
        except Exception as e:
            logger.error(f"[{self.market_type}] 保存持仓失败: {e}", exc_info=True)

    def get_remaining_capital(self) -> float:
        """获取剩余可用资金"""
        return self.strategy_capital - self.strategy_used_capital

    def execute_buy(self, stocks_to_buy: List[Dict]):
        """
        执行买入

        Args:
            stocks_to_buy: [{'code': 'HK.00700', 'quantity': 1000, 'price': 350.0}, ...]
        """
        if not stocks_to_buy:
            return

        # 阶段1: 在锁内检查资金和调整数量（快速操作）
        with self._position_lock:
            strategy_remaining = self.get_remaining_capital()

            # 计算总需求资金
            total_required = sum(s.get('quantity', 0) * s.get('price', 0.0) for s in stocks_to_buy)
            currency = 'HKD' if self.market_type == 'HK' else 'USD'
            logger.info(f"[{self.market_type}] 买入计划: {len(stocks_to_buy)}只股票, "
                       f"需资金 {currency} {total_required:.2f}, 策略剩余 {currency} {strategy_remaining:.2f}")

            # 检查资金并调整
            if total_required > strategy_remaining:
                logger.warning(f"[{self.market_type}] 策略资金不足，按比例缩减")
                scale = strategy_remaining / total_required if total_required > 0 else 0
                for stock in stocks_to_buy:
                    self._adjust_quantity_for_capital(stock, scale)

        # 阶段2: 在锁外执行买入（网络IO，可能耗时30-60秒）
        successful_buys = []
        failed_stocks = []
        
        for stock in stocks_to_buy:
            if stock['quantity'] <= 0:
                continue

            try:
                order_id, avg_price, dealt_qty = self.trader.place_order(
                    stock_code=stock['code'],
                    quantity=stock['quantity'],
                    order_type=OrderType.MARKET,
                    side='buy',
                    timeout=self.order_timeout
                )

                # 使用实际成交数量
                if dealt_qty <= 0:
                    logger.warning(f"[{self.market_type}] 买入 {stock['code']} 成交数量为0，跳过持仓更新")
                    failed_stocks.append(stock['code'])
                    continue
                
                actual_cost = dealt_qty * avg_price
                successful_buys.append({
                    'code': stock['code'],
                    'order_id': order_id,
                    'quantity': dealt_qty,
                    'cost': actual_cost,
                    'avg_price': avg_price,
                    'entry_mode': stock.get('entry_mode', 'bottom_fish'),
                    'proposal_id': stock.get('proposal_id'),
                })

                currency = 'HKD' if self.market_type == 'HK' else 'USD'
                logger.info(f"[{self.market_type}] 买入成功: {order_id}, {stock['code']} x {dealt_qty}股, "
                           f"均价 {currency} {avg_price:.2f}")
                self._ledger_record(
                    'position_opened',
                    stock_code=stock['code'],
                    order_id=order_id,
                    quantity=dealt_qty,
                    cost_price=avg_price,
                    entry_mode=stock.get('entry_mode', 'bottom_fish'),
                    proposal_id=stock.get('proposal_id'),
                    dry_run=False,
                )

            except (ValueError, TypeError, KeyError) as e:
                logger.error(f"[{self.market_type}] 买入失败 - 数据错误 {stock['code']}: {e}")
                failed_stocks.append(stock['code'])
                # 继续买入下一只
            except Exception as e:
                logger.error(f"[{self.market_type}] 买入失败 - 未知错误 {stock['code']}: {e}", exc_info=True)
                failed_stocks.append(stock['code'])
                # 继续买入下一只，不回滚（避免锁内长时间操作）

        # 阶段3: 在锁内更新持仓状态（快速操作）
        if successful_buys:
            with self._position_lock:
                for buy in successful_buys:
                    self.strategy_positions[buy['code']] = {
                        'quantity': buy['quantity'],
                        'cost_price': buy['avg_price'],
                        'highest_price': buy['avg_price'],
                        'order_id': buy['order_id'],
                        'buy_time': datetime.now().isoformat(),
                        'entry_mode': buy.get('entry_mode', 'bottom_fish'),
                        'proposal_id': buy.get('proposal_id'),
                    }
                    self.strategy_used_capital += buy['cost']

                # 保存状态到数据库
                self.save_positions()

            # 在锁外保存交易记录（数据库操作）
            for buy in successful_buys:
                try:
                    self._save_trade_record(
                        stock_code=buy['code'],
                        quantity=buy['quantity'],
                        price=buy['avg_price'],
                        direction='BUY',
                        order_id=buy['order_id']
                    )
                except Exception as e:
                    logger.error(f"[{self.market_type}] 保存交易记录失败 {buy['code']}: {e}")

        # 记录失败的股票
        if failed_stocks:
            logger.warning(f"[{self.market_type}] 以下股票买入失败: {failed_stocks}")

    def execute_sell(self, sell_list: list):
        """
        执行卖出操作

        Args:
            sell_list: 卖出列表 [{'code': 'HK.00700', 'quantity': 100}, ...]
        """
        for stock in sell_list:
            with self._position_lock:
                if stock['code'] not in self.strategy_positions:
                    logger.warning(f"[{self.market_type}] 持仓不存在: {stock['code']}")
                    continue

                pos = self.strategy_positions[stock['code']]
                cost_price = pos.get('cost_price', 0.0)
                if cost_price is None or cost_price <= 0:
                    logger.error(f"[{self.market_type}] 持仓成本价无效: {stock['code']}, cost_price={cost_price}")
                    continue

            # 调用内部卖出方法
            self._execute_exit(stock['code'], stock['quantity'], cost_price, '强制平仓')

    def check_and_exit_positions(self):
        """检查持仓并执行止盈止损"""
        with self._position_lock:
            if not self.strategy_positions:
                logger.info(f"[{self.market_type}] 当前无持仓，跳过止盈止损检查")
                if self.live_manager is not None and hasattr(self.live_manager, 'prune_positions_snapshot'):
                    self.live_manager.prune_positions_snapshot()
                return

            # 创建副本以避免在迭代时修改
            positions_snapshot = list(self.strategy_positions.items())

        logger.info(f"[{self.market_type}] ====== 开始检查 {len(positions_snapshot)} 只持仓的止盈止损 ======")

        exits_to_execute = []

        for stock_code, pos in positions_snapshot:
            try:
                if pos.get('demo'):
                    # 周末演示持仓不参与自动止盈止损（用演示平仓接口手动收尾）
                    continue
                if pos.get('_selling'):
                    # 已有卖出在执行（审批线程/强制平仓），本轮跳过避免重复提案/重复下单
                    continue
                quantity = pos['quantity']
                cost_price = pos['cost_price']
                highest_price = pos['highest_price']
                
                # 验证持仓数据
                if quantity is None or quantity <= 0:
                    logger.warning(f"[{self.market_type}] 持仓数量无效: {stock_code}, quantity={quantity}, 跳过检查")
                    continue
                
                logger.debug(f"[{self.market_type}] 检查持仓: {stock_code}, 数量: {quantity}, 成本: {cost_price}")

                # 获取当前价格
                price = self.price_fetcher.get_current_price(stock_code)
                if price is None:
                    logger.warning(f"[{self.market_type}] 无法获取 {stock_code} 当前价格，跳过检查")
                    continue

                # 成本价无效（0/None，如转入股、无成本仓）时无法计算盈亏/止盈止损：
                # 记日志并跳过本只，避免除零导致整轮退出检查中断
                if cost_price is None or cost_price <= 0:
                    logger.warning(
                        f"[{self.market_type}] {stock_code} 成本价无效 "
                        f"({cost_price})，跳过止盈止损检查（仅监控价格）"
                    )
                    if self.live_manager is not None and hasattr(
                            self.live_manager, 'update_positions_snapshot'):
                        try:
                            stock_name = self._get_stock_name(stock_code)
                            self.live_manager.update_positions_snapshot(stock_code, {
                                'stock_code': stock_code,
                                'stock_name': stock_name,
                                'manual': bool(pos.get('manual')),
                                'entry_mode': pos.get('entry_mode', 'manual'),
                                'quantity': int(quantity),
                                'cost_price': cost_price,
                                'price': price,
                                'highest_price': highest_price,
                                'return_pct': None,
                                'profit_amount': None,
                                'atr': None,
                                'take_profit_price': None,
                                'stop_loss_price': None,
                                'should_exit': False,
                                'reason': '成本价无效，无法计算止盈止损（仅监控）',
                                'status_text': '⚠️ 成本无效（仅监控）',
                            })
                        except Exception:
                            pass
                    continue

                # 计算当前盈亏
                return_pct = (price - cost_price) / cost_price
                profit_amount = (price - cost_price) * quantity

                # 更新最高价
                with self._position_lock:
                    if price > highest_price and stock_code in self.strategy_positions:
                        self.strategy_positions[stock_code]['highest_price'] = price
                        highest_price = price
                        logger.info(f"[{self.market_type}] {stock_code} 更新最高价: {highest_price:.3f}")

                # 检查退出信号
                entry_mode = pos.get('entry_mode', 'bottom_fish')
                should_exit, reason, atr, take_profit_price, stop_loss_price = self._check_exit_signals(
                    stock_code, quantity, cost_price, price, highest_price, entry_mode
                )

                # 港股评估闭环：每次止盈止损/卖出决策检查都记账（失败不影响交易）
                if (self.live_manager is not None
                        and hasattr(self.live_manager, 'record_exit_check')):
                    try:
                        self.live_manager.record_exit_check(
                            stock_code=stock_code,
                            price=price,
                            return_pct=return_pct,
                            should_exit=bool(should_exit),
                            reason=str(reason or ''),
                            atr=atr,
                            take_profit_price=take_profit_price,
                            stop_loss_price=stop_loss_price,
                            entry_mode=entry_mode,
                            quantity=quantity,
                            cost_price=cost_price,
                        )
                    except Exception:
                        pass

                # 记录持仓状态 - 现价和盈亏带颜色（突出显示经常变动的数据）
                is_manual = pos.get('manual', False)
                status = "🔴 触发退出" if should_exit else "🟢 观察中"
                if is_manual:
                    status = "📝 手动|" + status
                if should_exit and (self.live_manager is not None
                                    and hasattr(self.live_manager, 'sell_approval_enabled')
                                    and self.live_manager.sell_approval_enabled()):
                    status = "🔴 触发卖出（等待确认）" + (("📝手动|") if is_manual else "")
                profit_color = ColorConstants.PROFIT_NEGATIVE if return_pct < 0 else ColorConstants.PROFIT_POSITIVE
                
                # 构建基础日志信息
                stock_name = self._get_stock_name(stock_code)
                log_msg = (
                    f"[{self.market_type}] {stock_code} ({stock_name}) | "
                    f"持仓: {quantity}股 | "
                    f"成本: {cost_price:.3f} | "
                    f"现价: {ColorConstants.CURRENT_PRICE}{price:.3f}{ColorConstants.RESET} | "
                    f"最高: {highest_price:.3f} | "
                    f"盈亏: {profit_color}{return_pct*100:+.2f}% ({profit_amount:+.0f}){ColorConstants.RESET} | "
                    f"状态: {status}"
                )
                
                # 添加退出原因（如果有）
                if should_exit:
                    log_msg += f" | 原因: {reason}"
                
                # 添加ATR和止盈止损信息（只在ATR有效且有止盈价时显示）
                if atr > 0:
                    log_msg += f" | 波动率: {atr:.3f}"
                    if take_profit_price > 0:
                        log_msg += f" | 止盈: {take_profit_price:.3f}"
                    if stop_loss_price > 0:
                        log_msg += f" | 止损: {stop_loss_price:.3f}"
                
                logger.info(log_msg)

                # 发布最新持仓状态给监控页（价格/盈亏/止盈止损线都是本轮回测结果）
                if self.live_manager is not None and hasattr(self.live_manager, 'update_positions_snapshot'):
                    self.live_manager.update_positions_snapshot(stock_code, {
                        'stock_code': stock_code,
                        'stock_name': stock_name,
                        'manual': is_manual,
                        'entry_mode': entry_mode,
                        'quantity': quantity,
                        'cost_price': cost_price,
                        'price': price,
                        'highest_price': highest_price,
                        'return_pct': return_pct,
                        'profit_amount': profit_amount,
                        'atr': atr,
                        'take_profit_price': take_profit_price,
                        'stop_loss_price': stop_loss_price,
                        'should_exit': bool(should_exit),
                        'reason': reason or '',
                        'status_text': status,
                    })

                if should_exit:
                    # 卖出人工确认开启时：先推确认页（LLM+用户），不再直接执行
                    if (self.live_manager is not None
                            and hasattr(self.live_manager, 'sell_approval_enabled')
                            and self.live_manager.sell_approval_enabled()):
                        try:
                            self.live_manager.queue_sell_proposal(
                                stock_code, reason, quantity=quantity,
                                pnl_pct=return_pct,
                                position_info={'price': price},
                            )
                        except Exception as e:
                            logger.error(
                                f"[{self.market_type}] 推送卖出确认失败，"
                                f"仍按自动退出处理 {stock_code}: {e}"
                            )
                            exits_to_execute.append(
                                (stock_code, quantity, cost_price, reason)
                            )
                    else:
                        exits_to_execute.append((stock_code, quantity, cost_price, reason))

            except (KeyError, ValueError, TypeError, ZeroDivisionError) as e:
                logger.error(f"[{self.market_type}] 检查持仓失败 - 数据错误 {stock_code}: {e}")
            except Exception as e:
                logger.error(f"[{self.market_type}] 检查持仓失败 - 未知错误 {stock_code}: {e}", exc_info=True)
                raise

        # 执行退出操作
        for stock_code, quantity, cost_price, reason in exits_to_execute:
            self._execute_exit(stock_code, quantity, cost_price, reason)

        # 同步监控页快照：本轮结束后移除已平仓的股票
        if self.live_manager is not None and hasattr(self.live_manager, 'prune_positions_snapshot'):
            self.live_manager.prune_positions_snapshot()

        logger.info(f"[{self.market_type}] 持仓检查完成")

    def _check_exit_signals(self, stock_code: str, quantity: int,
                           cost_price: float, price: float,
                           highest_price: float,
                           entry_mode: str = 'bottom_fish') -> Tuple[bool, str, float, float, float]:
        """检查止盈止损信号"""
        try:
            return self._check_exit_signals_impl(stock_code, quantity, cost_price, price, highest_price, entry_mode)
        except (KeyError, ValueError, TypeError, IndexError, ZeroDivisionError) as e:
            logger.error(f"[{self.market_type}] 检查退出信号失败 - 数据错误 {stock_code}: {e}")
            return False, '', 0.0, 0.0, 0.0
        except Exception as e:
            logger.error(f"[{self.market_type}] 检查退出信号失败 - 未知错误 {stock_code}: {e}", exc_info=True)
            raise

    def _execute_exit(self, stock_code: str, quantity: int,
                      cost_price: float, reason: str):
        """执行卖出"""
        logger.warning(f"[{self.market_type}] 触发退出: {stock_code}, 原因: {reason}, 数量: {quantity}")

        # 验证参数
        if quantity <= 0:
            logger.error(f"[{self.market_type}] 卖出数量无效: {quantity}, 跳过卖出")
            return

        # 阶段1: 在锁内获取持仓信息（快速操作）
        with self._position_lock:
            position_info = self.strategy_positions.get(stock_code, {})
            is_manual = position_info.get('manual', False)
            entry_mode = position_info.get('entry_mode')
            proposal_id = position_info.get('proposal_id')
            buy_time = position_info.get('buy_time')
            
            # 检查持仓是否还存在
            if stock_code not in self.strategy_positions:
                logger.warning(f"[{self.market_type}] 卖出时持仓已不存在: {stock_code}")
                return

            # 另一笔卖出正在执行中（审批线程/强制平仓/检查循环并发）→ 防重复下单
            if position_info.get('_selling'):
                logger.warning(
                    f"[{self.market_type}] {stock_code} 已有卖出在执行，跳过本次重复卖出"
                )
                return
            
            # 标记为正在卖出（防止重复卖出）
            self.strategy_positions[stock_code]['_selling'] = True

        # 阶段2: 在锁外执行卖出（网络IO，可能耗时）
        try:
            order_id, avg_price, dealt_qty = self.trader.place_order(
                stock_code=stock_code,
                quantity=quantity,
                order_type=OrderType.MARKET,
                side='sell',
                timeout=self.order_timeout
            )
            
            if dealt_qty <= 0:
                logger.warning(f"[{self.market_type}] 卖出 {stock_code} 成交数量为0")
                # 清除卖出标记
                with self._position_lock:
                    if stock_code in self.strategy_positions:
                        self.strategy_positions[stock_code].pop('_selling', None)
                return
            
            # 使用实际成交数量计算
            actual_sell_amount = dealt_qty * avg_price
            actual_cost_amount = dealt_qty * cost_price
            
        except Exception as e:
            logger.error(f"[{self.market_type}] 卖出失败 {stock_code}: {e}", exc_info=True)
            # 清除卖出标记
            with self._position_lock:
                if stock_code in self.strategy_positions:
                    self.strategy_positions[stock_code].pop('_selling', None)
            return

        # 阶段3: 在锁内更新持仓状态（仅内存快速操作）
        with self._position_lock:
            try:
                # 计算交易费用
                trading_cost = self.market_adapter.calculate_trading_cost(
                    amount=actual_sell_amount,
                    direction='sell',
                    stock_code=stock_code
                )
                total_fee = trading_cost['total']
                
                # 计算净利润
                gross_profit = actual_sell_amount - actual_cost_amount
                net_profit = gross_profit - total_fee

                # 手动买入的股票不更新策略资金
                if is_manual:
                    logger.info(f"[{self.market_type}] {stock_code} 为手动买入股票，不更新策略资金")
                else:
                    # 更新已用资金（减去成本）
                    self.strategy_used_capital -= actual_cost_amount
                    if self.strategy_used_capital < 0:
                        self.strategy_used_capital = 0.0

                    # 将净利润加入总资金
                    self.strategy_capital += net_profit

                # 止损卖出 → 加入冷却期（冷却期内不重买）
                stop_loss_reasons = [
                    'atr_stop_loss', 'decline_stop',
                    'rsrs_stop', 'early_hard_stop'
                ]
                if reason in stop_loss_reasons and not is_manual:
                    self.add_cooldown(stock_code)

                # 删除持仓记录（放最后：前面任何一步失败都不会出现“已 pop 但没保存”）
                self.strategy_positions.pop(stock_code, None)
            except Exception as e:
                # 内存更新失败时持仓保持原状（不 pop、不早退），避免“仓已丢但没保存”
                logger.error(f"[{self.market_type}] 更新持仓状态失败 {stock_code}: {e}", exc_info=True)
                return

        # 阶段3.5: 锁外持久化。save_positions 内部已吞掉写盘异常并记日志；
        # 若确实写失败，内存已与券商成交一致，下次同步/重启会以券商持仓自愈。
        self.save_positions()

        # 评估账本：记录平仓结果
        try:
            holding_days = 0
            if buy_time:
                try:
                    buy_dt = datetime.fromisoformat(str(buy_time))
                    holding_days = max(0, (datetime.now() - buy_dt).days)
                except Exception:
                    pass
            pnl_pct = net_profit / actual_cost_amount if actual_cost_amount > 0 else 0.0
            self._ledger_record(
                'position_closed',
                stock_code=stock_code,
                order_id=order_id,
                quantity=dealt_qty,
                cost_price=cost_price,
                exit_price=avg_price,
                gross_profit=gross_profit,
                net_profit=net_profit,
                pnl_pct=pnl_pct,
                reason=reason,
                entry_mode=entry_mode,
                proposal_id=proposal_id,
                holding_days=holding_days,
                dry_run=False,
            )
        except Exception as e:
            logger.debug(f"[{self.market_type}] 账本平仓记录失败: {e}")

        # 阶段4: 在锁外保存交易记录和日志（数据库操作）
        try:
            self._save_trade_record(
                stock_code=stock_code,
                quantity=dealt_qty,
                price=avg_price,
                direction='SELL',
                order_id=order_id
            )

            currency = 'HKD' if self.market_type == 'HK' else 'USD'
            profit_color = '\033[92m' if net_profit >= 0 else '\033[91m'
            reset_color = '\033[0m'

            if is_manual:
                logger.info(
                    f"[{self.market_type}] 卖出完成 (手动): {stock_code}, "
                    f"成交 {dealt_qty}股, 金额 {currency} {actual_sell_amount:.2f}, "
                    f"交易费用 {currency} {total_fee:.2f}, "
                    f"净盈亏: {profit_color}{net_profit:+.2f}{reset_color}"
                )
            else:
                logger.info(
                    f"[{self.market_type}] 卖出完成: {stock_code}, "
                    f"成交 {dealt_qty}股, 金额 {currency} {actual_sell_amount:.2f}, "
                    f"交易费用 {currency} {total_fee:.2f} "
                    f"(佣金:{trading_cost['commission']:.2f}, 印花税:{trading_cost['stamp_duty']:.2f}), "
                    f"净盈亏: {profit_color}{net_profit:+.2f}{reset_color}, "
                    f"总资金更新为: {currency} {self.strategy_capital:.2f}"
                )

        except Exception as e:
            logger.error(f"[{self.market_type}] 保存交易记录失败 {stock_code}: {e}")

    def sync_with_broker(self) -> bool:
        """与券商同步持仓"""
        try:
            broker_positions = self.trader.get_positions()
            # 过滤掉已平仓的股票（quantity=0）
            active_broker_positions = [p for p in broker_positions if p.get('quantity', 0) > 0]
            broker_codes = {p['stock_code'] for p in active_broker_positions}

            # 锁内先做一次只读快照，计算差异（避免与卖出线程并发修改冲突）
            with self._position_lock:
                local_codes = set(self.strategy_positions.keys())

            to_remove = local_codes - broker_codes
            # 预取新增持仓的现价放在锁外（网络IO不持锁，避免阻塞卖出/买入线程）
            to_add = [bp for bp in active_broker_positions if bp['stock_code'] not in local_codes]
            add_prices = {}
            for bp in to_add:
                code = bp['stock_code']
                try:
                    add_prices[code] = self.price_fetcher.get_current_price(code, force_refresh=True)
                except Exception:
                    add_prices[code] = None

            with self._position_lock:
                # 处理已卖出的（锁内重新核对，防止快照过期误删刚买入/刚登记的持仓）
                for code in list(to_remove):
                    if code in broker_codes or code not in self.strategy_positions:
                        continue
                    position = self.strategy_positions.get(code, {})
                    if position.get('_selling'):
                        # 卖出流程正在执行（订单已下/等待成交），由 _execute_exit
                        # 负责移除持仓与资金回冲，同步跳过避免双重回冲
                        logger.info(f"[{self.market_type}] 同步跳过卖出执行中的持仓: {code}")
                        continue
                    self.strategy_positions.pop(code, None)
                    if position.get('demo'):
                        logger.info(f"[{self.market_type}] [周末演示] 同步清理演示持仓: {code}")
                        continue
                    if position.get('manual'):
                        # 手动买入不占策略资金，卖出/移除时无需回冲（与 _execute_exit 语义一致）
                        logger.info(f"[{self.market_type}] 同步移除手动持仓(不回冲策略资金): {code}")
                        continue
                    qty = position.get('quantity', 0)
                    cost = position.get('cost_price', 0.0)
                    if isinstance(qty, (int, float)) and isinstance(cost, (int, float)):
                        released = qty * cost
                        self.strategy_used_capital -= released
                        if self.strategy_used_capital < 0:
                            self.strategy_used_capital = 0.0
                    logger.info(f"[{self.market_type}] 同步移除持仓: {code}")

                # 处理新增的
                for bp in to_add:
                    code = bp['stock_code']
                    if code in self.strategy_positions:
                        # 预取价格期间可能已被买入/演示登记，跳过
                        continue
                    current_price = add_prices.get(code)
                    cost_price = bp.get('cost_price', 0.0)
                    # 如果获取不到当前价格，使用成本价作为最高价
                    if current_price is None or current_price <= 0:
                        logger.warning(f"[{self.market_type}] 同步持仓时无法获取 {code} 当前价格，使用成本价作为最高价")
                        highest = cost_price
                    else:
                        highest = current_price if current_price > cost_price else cost_price

                    self.strategy_positions[code] = {
                        'quantity': bp['quantity'],
                        'cost_price': bp['cost_price'],
                        'highest_price': highest,
                        'synced': True
                    }
                    self.strategy_used_capital += bp['quantity'] * bp['cost_price']
                    logger.info(f"[{self.market_type}] 同步新增持仓: {code}")

            self.save_positions()
            return True

        except Exception as e:
            logger.error(f"[{self.market_type}] 同步持仓失败: {e}", exc_info=True)
            return False
