"""
港股持仓管理模块 - 基于基类实现
"""
import logging
import numpy as np
from typing import Dict, Tuple
from datetime import datetime, timedelta

try:
    from .position_manager_base import PositionManagerBase
except ImportError:
    from position_manager_base import PositionManagerBase

logger = logging.getLogger(__name__)


class HKPositionManager(PositionManagerBase):
    """港股持仓管理器"""

    def __init__(self, config: Dict, trader, state_persistence, price_fetcher):
        super().__init__(config, trader, state_persistence, price_fetcher, market_type='HK')

    def _get_capital_config(self) -> float:
        """获取港股资金配置"""
        return self.config.get('strategy', {}).get('initial_capital', 100000)

    def _load_positions_from_db(self):
        """从数据库加载港股持仓"""
        # 先从富途同步实际持仓
        try:
            logger.info("[HK] 正在从富途同步实际持仓...")
            futu_positions = self.trader.get_positions()
            
            if futu_positions:
                logger.info(f"[HK] 富途实际持仓: {len(futu_positions)}只")
                for pos in futu_positions:
                    logger.info(f"[HK] 富途持仓: {pos['stock_code']} = {pos['quantity']}股, 成本: {pos['cost_price']:.3f}")
                
                # 过滤掉已平仓的股票（quantity=0）
                active_positions = [pos for pos in futu_positions if pos['quantity'] > 0]
                zero_positions = [pos for pos in futu_positions if pos['quantity'] == 0]
                
                if zero_positions:
                    logger.warning(f"[HK] 检测到{len(zero_positions)}只已平仓股票: {[pos['stock_code'] for pos in zero_positions]}")
                
                # 获取数据库中标记为手动的持仓（用于区分手动买入）
                db_positions = self.state_persistence.get_positions()
                db_manual_codes = {pos['stock_code'] for pos in db_positions if pos.get('manual')} if db_positions else set()
                db_quant_codes = {pos['stock_code'] for pos in db_positions if not pos.get('manual')} if db_positions else set()
                
                # 用富途的有效持仓覆盖数据库中的持仓
                self.strategy_positions = {}
                self.strategy_used_capital = 0.0
                manual_positions = []
                
                for pos in active_positions:
                    stock_code = pos['stock_code']
                    # 从数据库读 manual 字段；不在 DB 中的视为新手动买入
                    is_manual = stock_code in db_manual_codes or stock_code not in db_quant_codes
                    
                    self.strategy_positions[stock_code] = {
                        'quantity': pos['quantity'],
                        'cost_price': pos['cost_price'],
                        'highest_price': pos['cost_price'],  # 初始化为成本价
                        'manual': is_manual  # 标记是否手动买入
                    }
                    
                    # 手动买入的股票不计入策略资金
                    if not is_manual:
                        self.strategy_used_capital += pos['quantity'] * pos['cost_price']
                    else:
                        manual_positions.append(stock_code)
                
                if manual_positions:
                    logger.warning(f"[HK] 检测到 {len(manual_positions)} 只手动买入股票: {manual_positions} (仅监控，不影响策略资金)")
                
                logger.info(f"[HK] 已从富途同步持仓: {len(self.strategy_positions)}只, "
                           f"策略已用资金: HKD {self.strategy_used_capital:.2f}")
                
            else:
                # 富途返回空列表（未抛异常），同样需要 fallback 到 trading_state
                logger.info("[HK] 富途返回空持仓，尝试从 trading_state 恢复...")
                state = self.state_persistence.load_state()
                if state:
                    all_positions = state.get('positions', {})
                    self.strategy_positions = {code: pos for code, pos in all_positions.items()
                                              if pos.get('quantity', 0) > 0}
                    self.strategy_used_capital = sum(
                        pos.get('quantity', 0) * pos.get('cost_price', 0)
                        for pos in self.strategy_positions.values()
                    )
                    invalid_count = len(all_positions) - len(self.strategy_positions)
                    if invalid_count > 0:
                        logger.warning(f"[HK] 过滤掉{invalid_count}只无效持仓")
                    logger.info(f"[HK] 已从 trading_state 恢复持仓: {len(self.strategy_positions)}只, "
                               f"已用资金: HKD {self.strategy_used_capital:.2f}")
                else:
                    self.strategy_positions = {}
                    self.strategy_used_capital = 0.0
                    logger.info("[HK] trading_state 无持仓记录")
                
        except Exception as e:
            logger.error(f"[HK] 从富途同步持仓失败: {e}", exc_info=True)
            logger.warning("[HK] 继续使用数据库中的持仓数据")
            # 如果同步失败，回退到 trading_state（并过滤无效持仓）
            state = self.state_persistence.load_state()
            if state:
                all_positions = state.get('positions', {})
                self.strategy_positions = {code: pos for code, pos in all_positions.items()
                                          if pos.get('quantity', 0) > 0}
                self.strategy_used_capital = sum(
                    pos.get('quantity', 0) * pos.get('cost_price', 0)
                    for pos in self.strategy_positions.values()
                )
                invalid_count = len(all_positions) - len(self.strategy_positions)
                if invalid_count > 0:
                    logger.warning(f"[HK] 过滤掉{invalid_count}只无效持仓(quantity=0)")
                logger.info(f"[HK] 已加载数据库持仓: {len(self.strategy_positions)}只, "
                           f"已用资金: HKD {self.strategy_used_capital:.2f}")
            else:
                self.strategy_positions = {}
                self.strategy_used_capital = 0.0
                logger.info("[HK] 数据库无持仓记录")

    def _save_positions_to_db(self):
        """保存港股持仓到数据库"""
        # 保存状态（只保存有效持仓，quantity>0）
        valid_positions = {code: pos for code, pos in self.strategy_positions.items() 
                          if pos.get('quantity', 0) > 0}
        
        self.state_persistence.save_state(
            positions=valid_positions,
            used_capital=self.strategy_used_capital,
            capital=self.strategy_capital,
            last_buy_execution=int(__import__('time').time())
        )
        # 保存明细和资金记录
        self._save_positions_detail()
        self._save_capital_record()

    def _save_positions_detail(self):
        """保存持仓明细"""
        # 如果策略持仓为空，跳过写入（避免清空 positions 表）
        if not self.strategy_positions:
            logger.info("[HK] 策略持仓为空，跳过写入 positions 表（保留现有手动标记）")
            return
        self.state_persistence.clear_positions()
        for stock_code, pos in self.strategy_positions.items():
            self.state_persistence.save_position(
                stock_code=stock_code,
                stock_name=self._get_stock_name(stock_code),
                quantity=pos.get('quantity', 0),
                cost_price=pos.get('cost_price', 0),
                highest_price=pos.get('highest_price', 0),
                manual=pos.get('manual', False)
            )

    def _cleanup_invalid_positions(self):
        """清理数据库中无效持仓（不在当前持仓列表中的股票）"""
        try:
            # 获取数据库中所有持仓
            db_positions = self.state_persistence.get_positions()
            if not db_positions:
                return
            
            # 找出不在当前持仓中的股票
            current_codes = set(self.strategy_positions.keys())
            db_codes = set(pos['stock_code'] for pos in db_positions)
            invalid_codes = db_codes - current_codes
            
            if invalid_codes:
                logger.warning(f"[HK] 清理数据库中的无效持仓: {len(invalid_codes)}只")
                from mutifactor.trading import TradingEnv
                env = TradingEnv.REAL if self.config.get('trading', {}).get('env', 'SIMULATE').upper() == 'REAL' else TradingEnv.SIMULATE
                for code in invalid_codes:
                    logger.info(f"[HK] 删除无效持仓: {code}")
                    self.state_persistence._yaml_storage.delete_position(code, env)
        except Exception as e:
            logger.warning(f"[HK] 清理无效持仓失败: {e}")

    def _save_capital_record(self):
        """保存资金记录"""
        self.state_persistence.save_capital(
            total_capital=self.strategy_capital,
            used_capital=self.strategy_used_capital
        )

    def _save_trade_record(self, stock_code: str, quantity: int, price: float,
                          direction: str, order_id: str):
        """保存港股交易记录"""
        self.state_persistence.save_trade(
            stock_code=stock_code,
            stock_name=self._get_stock_name(stock_code),
            quantity=quantity,
            price=price,
            direction=direction,
            order_id=order_id
        )

    def _get_stock_name(self, stock_code: str) -> str:
        """获取港股名称"""
        from mutifactor.data import get_hk_stock_name
        return get_hk_stock_name(stock_code)

    def _check_momentum_stop(self, stock_code: str, cost_price: float,
                                highest_price: float, current_price: float) -> Tuple[bool, str, float]:
        """
        追涨止损保护（Layer 1）：对追涨入仓的持仓，取固定止损和追踪止损的更大值

        止损触发线 = max(
            固定止损: entry_price × (1 - momentum_stop_pct)
            追踪止损: highest_since_entry - trail_mult × ATR
        )

        Args:
            stock_code: 股票代码
            cost_price: 入仓价
            highest_price: 入仓后最高价
            current_price: 当前价格

        Returns:
            (should_exit, reason, atr)
        """
        risk_cfg = self.config.get('risk', {})
        momentum_stop_pct = risk_cfg.get('momentum_stop_pct', 0.02)  # 默认2%
        trail_mult = risk_cfg.get('momentum_trail_mult', 1.5)        # 默认1.5×ATR

        # 1. 计算固定止损线
        fixed_stop = cost_price * (1 - momentum_stop_pct)

        # 2. 获取ATR计算追踪止损线
        atr = 0.0
        trail_stop = fixed_stop  # 拿不到ATR时 fallback
        try:
            end_date = datetime.now().date()
            start_date = end_date - timedelta(days=60)

            # 尝试用缓存的K线
            kline_df = None
            if self.live_manager and hasattr(self.live_manager, 'kline_cache'):
                kline_df = self.live_manager.kline_cache.get(stock_code)

            if kline_df is None or len(kline_df) < 14:
                if self.live_manager and hasattr(self.live_manager, '_shared_fetcher') and self.live_manager._shared_fetcher:
                    data_fetcher = self.live_manager._shared_fetcher
                else:
                    from mutifactor.data import FutuHKDataFetcher
                    data_fetcher = FutuHKDataFetcher(
                        host=self.price_fetcher.host,
                        port=self.price_fetcher.port
                    )
                kline_df = data_fetcher.fetch_stock_kline(
                    stock_code=stock_code,
                    start_date=start_date.strftime('%Y-%m-%d'),
                    end_date=end_date.strftime('%Y-%m-%d')
                )

            if kline_df is not None and len(kline_df) >= 14:
                high = kline_df['high'].values
                low = kline_df['low'].values
                close = kline_df['close'].values
                tr = np.maximum(
                    high[1:] - low[1:],
                    np.abs(high[1:] - np.roll(close, 1)[1:])
                )
                tr = np.maximum(tr, np.abs(np.roll(close, 1)[1:] - low[1:]))
                atr = np.mean(tr[-14:])
                if atr > 0:
                    trail_stop = highest_price - trail_mult * atr
        except Exception as e:
            logger.debug(f"[HK] {stock_code} 追涨ATR计算失败: {e}")

        # 3. 取最大值（更紧保护）
        stop_line = max(fixed_stop, trail_stop)

        if current_price <= stop_line:
            if stop_line == fixed_stop:
                reason = f"追涨止损|固定{fixed_stop:.3f}(-{momentum_stop_pct*100:.0f}%)"
            else:
                reason = f"追涨止损|追踪{trail_stop:.3f}"
            logger.warning(f"[HK] 🚨 {stock_code} 追涨止损触发: 现价{current_price:.3f} ≤ {stop_line:.3f} | 固定={fixed_stop:.3f} 追踪={trail_stop:.3f}")
            return True, reason, atr

        return False, '', atr

    def _adjust_quantity_for_capital(self, stock: Dict, scale: float):
        """港股按手数调整数量"""
        lot_size = self.trader.get_lot_size(stock['code'])
        stock['quantity'] = int(stock['quantity'] * scale // lot_size) * lot_size

    def _check_exit_signals_impl(self, stock_code: str, quantity: int,
                                 cost_price: float, price: float,
                                 highest_price: float,
                                 entry_mode: str = 'bottom_fish') -> Tuple[bool, str, float, float, float]:
        """检查港股止盈止损信号 - 使用与回测一致的ATR策略"""
        from mutifactor.strategies.exit_strategy import ExitStrategyFactory
        from datetime import datetime, timedelta

        # 构建持仓信息
        position = {
            'stock_code': stock_code,
            'quantity': quantity,
            'cost_price': cost_price,
            'highest_price': highest_price
        }

        # 获取当日高低价
        today_high, today_low = self.price_fetcher.get_today_high_low(stock_code)

        # 检查是否为手动买入
        is_manual = self.strategy_positions.get(stock_code, {}).get('manual', False)

        # ==================== Layer 1: 追涨止损保护 ====================
        if entry_mode == 'momentum' and not is_manual:
            should_exit, reason, atr = self._check_momentum_stop(
                stock_code, cost_price, highest_price, price
            )
            if should_exit:
                return True, reason, atr, 0.0, 0.0

        # ==================== 获取K线数据（数据库优先，不够再补） ====================

        # ==================== 获取K线数据（数据库优先，不够再补） ====================
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=100)
        
        # 优先使用 live_manager 中缓存的 K 线数据
        kline_df = None
        if self.live_manager and hasattr(self.live_manager, 'kline_cache'):
            kline_df = self.live_manager.kline_cache.get(stock_code)
            if kline_df is not None:
                logger.debug(f"[HK] 使用缓存的K线数据: {stock_code}（{len(kline_df)}条记录）")
        
        # 如果缓存中没有，直接从 OpenD 拉取
        if kline_df is None or len(kline_df) < 30:
            # 复用 live_manager 的共享数据获取器
            if self.live_manager and hasattr(self.live_manager, '_shared_fetcher') and self.live_manager._shared_fetcher:
                data_fetcher = self.live_manager._shared_fetcher
            else:
                from mutifactor.data import FutuHKDataFetcher
                data_fetcher = FutuHKDataFetcher(
                    host=self.price_fetcher.host,
                    port=self.price_fetcher.port
                )
            
            # 直接从 OpenD 拉取（无 DB 层），捕获异常避免崩溃
            try:
                kline_df = data_fetcher.fetch_stock_kline(
                    stock_code=stock_code,
                    start_date=start_date.strftime('%Y-%m-%d'),
                    end_date=end_date.strftime('%Y-%m-%d')
                )
            except Exception as e:
                # 新股/未订阅/无历史数据时，直接降级到简单止损
                logger.warning(f"[HK] {stock_code} 获取K线失败（可能是新股或未订阅）: {e}，使用固定止损")
                kline_df = None

        if kline_df is not None and len(kline_df) >= 30:
            # 使用K线数据进行止盈止损检查
            logger.debug(f"[HK] 使用K线数据检查止盈止损: {stock_code}（{len(kline_df)}条记录）")
            should_exit, reason, atr, take_profit_price, stop_loss_price = ExitStrategyFactory.check_exit_with_dataframe(
                position=position,
                current_price=price,
                config=self.config,
                kline_df=kline_df,
                today_high=today_high,
                today_low=today_low
            )
        else:
            # 无法获取足够的K线数据
            if is_manual:
                # 手动买入的股票（如打新股），无K线数据是正常的，使用固定百分比止损
                logger.info(f"[HK] {stock_code} 无历史K线（可能是新股），使用固定百分比止损")
                should_exit, reason, atr, take_profit_price, stop_loss_price = ExitStrategyFactory.check_exit_simple(
                    position=position,
                    current_price=price,
                    config=self.config
                )
                reason = f"新股止损|{reason}" if should_exit else reason
            else:
                # 策略买入的股票，无K线数据是异常情况
                logger.error(f"[HK] 🚨 严重错误：无法获取K线数据: {stock_code}")
                logger.error(f"[HK]    数据条数: {len(kline_df) if kline_df is not None else 0}（需要至少22条）")
                
                # 降级到简化止损（但明确记录为错误）
                should_exit, reason, atr, take_profit_price, stop_loss_price = ExitStrategyFactory.check_exit_simple(
                    position=position,
                    current_price=price,
                    config=self.config
                )
                reason = f"🚨NO_KLINE_DATA|{reason}"
                
                # 在日志中突出显示
                logger.error(f"[HK] ⚠️  已降级到简化止损逻辑: {stock_code}（盈亏计算可能不准确）")

        return should_exit, reason, atr, take_profit_price, stop_loss_price

