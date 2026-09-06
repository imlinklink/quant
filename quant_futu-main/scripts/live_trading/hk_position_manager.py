"""
港股持仓管理模块 - 基于基类实现
"""
import logging
import numpy as np
import pandas as pd
from typing import Dict, Tuple
from datetime import datetime, timedelta

try:
    from .position_manager_base import PositionManagerBase
except ImportError:
    from position_manager_base import PositionManagerBase

logger = logging.getLogger(__name__)


def _bottom_time_stop_hit(buy_date, kline_df, current_price, highest_price,
                          entry_price, window_days, rebound_pct) -> tuple:
    """bottom_fish 观察窗时间止损（纯函数，便于测试）。

    规则：持有超过 window_days 个交易日，且期间最高价从未达到
    买入价×(1+rebound_pct) → 反弹未发生，判定逻辑失效。
    Returns: (hit, holding_trading_days)
    """
    if (not buy_date or kline_df is None or len(kline_df) < 2
            or window_days <= 0):
        return False, 0
    try:
        bd = pd.to_datetime(str(buy_date)).normalize()
        after = kline_df[kline_df['date'] > bd]
        held = int(len(after))
    except Exception:
        return False, 0
    if held < window_days:
        return False, held
    try:
        peak_after = float(after['high'].max())
    except Exception:
        peak_after = 0.0
    peak = max(float(highest_price or 0), float(current_price or 0), peak_after)
    if entry_price and entry_price > 0 and peak < entry_price * (1 + rebound_pct):
        return True, held
    return False, held


class HKPositionManager(PositionManagerBase):
    """港股持仓管理器"""

    def __init__(self, config: Dict, trader, state_persistence, price_fetcher):
        super().__init__(config, trader, state_persistence, price_fetcher, market_type='HK')

    def _get_capital_config(self) -> float:
        """获取港股资金配置"""
        return self.config.get('strategy', {}).get('initial_capital', 100000)

    def _load_positions_from_db(self):
        """从数据库加载港股持仓"""
        # 恢复止损冷却期（进程重启后仍需遵守冷却期，避免刚止损的票被立即买回）
        try:
            saved_state = self.state_persistence.load_state()
            saved_cooldowns = (saved_state or {}).get('cooldowns') or {}
            if isinstance(saved_cooldowns, dict):
                restored = {str(k): str(v) for k, v in saved_cooldowns.items() if v}
                if restored:
                    self.recently_stopped = restored
                    logger.warning(f"[HK] 已恢复 {len(restored)} 条止损冷却期记录")
        except Exception as e:
            logger.warning(f"[HK] 恢复冷却期记录失败（不影响启动）: {e}")

        # 先从富途同步实际持仓
        try:
            logger.info("[HK] 正在从富途同步实际持仓...")
            futu_positions = self.trader.get_positions()
            
            if futu_positions is not None:
                # get_positions 成功返回空列表 = 账户确实无持仓（今天已全部卖出等），
                # 必须清空本地，不能回退 trading_state 造成“幽灵持仓”；
                # 只有查询抛异常（None 兜底/降级）才走 trading_state 恢复。
                logger.info(f"[HK] 富途实际持仓: {len(futu_positions)}只")
                for pos in futu_positions:
                    logger.info(f"[HK] 富途持仓: {pos['stock_code']} = {pos['quantity']}股, 成本: {pos['cost_price']:.3f}")
                
                # 过滤掉已平仓的股票（quantity=0）
                active_positions = [pos for pos in futu_positions if pos['quantity'] > 0]
                zero_positions = [pos for pos in futu_positions if pos['quantity'] == 0]
                
                if zero_positions:
                    logger.warning(f"[HK] 检测到{len(zero_positions)}只已平仓股票: {[pos['stock_code'] for pos in zero_positions]}")
                
                # 获取数据库持仓明细（manual / highest_price / buy_time）
                db_positions = self.state_persistence.get_positions() or []
                db_by_code = {}
                for dp in db_positions:
                    if dp.get('stock_code'):
                        db_by_code.setdefault(dp['stock_code'], dp)

                # 从交易记录反查“策略买入过”的代码：
                # positions 明细表可能被清理/丢失，但 trades 里有 BUY 记录就说明
                # 是策略仓而不是手动买入，避免策略资金被永久低估（错判为 manual 不计资金）。
                strategy_bought_codes = set()
                try:
                    storage = getattr(self.state_persistence, 'yaml_storage', None)
                    if storage is not None:
                        from mutifactor.infra.yaml_storage import TradingEnv
                        env_str = str(self.config.get('trading', {}).get('env', 'SIMULATE')).upper()
                        env_enum = TradingEnv.REAL if env_str == 'REAL' else TradingEnv.SIMULATE
                        trades = storage.get_trades(env=env_enum) or []
                        strategy_bought_codes = {
                            t.get('stock_code') for t in trades
                            if t.get('stock_code')
                            and 'BUY' in str(t.get('trade_type', '')).upper()
                        }
                except Exception as e:
                    logger.warning(f"[HK] 读取买入交易记录失败（不影响启动）: {e}")
                
                # 用富途的有效持仓覆盖数据库中的持仓
                self.strategy_positions = {}
                self.strategy_used_capital = 0.0
                manual_positions = []
                
                for pos in active_positions:
                    stock_code = pos['stock_code']
                    db_rec = db_by_code.get(stock_code, {})
                    # 从数据库读 manual 字段；不在 DB 中但交易记录里有系统 BUY → 策略仓；
                    # 既不在 DB 也没有系统买入记录 → 视为新手动买入
                    is_manual = (
                        bool(db_rec.get('manual'))
                        if db_rec
                        else stock_code not in strategy_bought_codes
                    )
                    cost_price = float(pos.get('cost_price') or 0)
                    # 重启后保留历史最高价锚点（>= 成本价），避免吊灯止盈/止损线被重置
                    try:
                        db_highest = float(db_rec.get('highest_price') or 0)
                    except (TypeError, ValueError):
                        db_highest = 0.0
                    highest_price = max(cost_price, db_highest)
                    # 重启后恢复买入时间（持仓天数 → 时间退出/RSRS 豁免期判定）
                    buy_time = str(db_rec.get('buy_time') or '')

                    self.strategy_positions[stock_code] = {
                        'quantity': pos['quantity'],
                        'cost_price': cost_price,
                        'highest_price': highest_price,
                        'manual': is_manual,  # 标记是否手动买入
                        'buy_time': buy_time,
                        'buy_date': buy_time[:10] if buy_time else '',
                    }
                    
                    # 手动买入的股票不计入策略资金
                    if not is_manual:
                        self.strategy_used_capital += pos['quantity'] * cost_price
                    else:
                        manual_positions.append(stock_code)
                
                if manual_positions:
                    logger.warning(f"[HK] 检测到 {len(manual_positions)} 只手动买入股票: {manual_positions} (仅监控，不影响策略资金)")
                
                logger.info(f"[HK] 已从富途同步持仓: {len(self.strategy_positions)}只, "
                           f"策略已用资金: HKD {self.strategy_used_capital:.2f}")
                
            else:
                # 正常 get_positions 不会返回 None（失败会抛异常）；
                # 这里仅兜底兼容异常/降级场景
                logger.info("[HK] 富途持仓查询返回 None，尝试从 trading_state 恢复...")
                state = self.state_persistence.load_state()
                self._restore_from_state(state)
                
        except Exception as e:
            logger.error(f"[HK] 从富途同步持仓失败: {e}", exc_info=True)
            logger.warning("[HK] 继续使用数据库中的持仓数据")
            # 如果同步失败，回退到 trading_state（并过滤无效持仓）
            state = self.state_persistence.load_state()
            self._restore_from_state(state)

    def _restore_from_state(self, state: dict):
        """从 trading_state 恢复持仓：过滤 demo/无效仓，排除手动仓资金与运行时标记。"""
        if not state:
            self.strategy_positions = {}
            self.strategy_used_capital = 0.0
            logger.info("[HK] trading_state 无持仓记录")
            return
        all_positions = state.get('positions', {}) or {}
        clean = {}
        for code, pos in all_positions.items():
            if not isinstance(pos, dict):
                continue
            p = dict(pos)
            p.pop('_selling', None)  # 运行时标记不应跨重启存活
            if p.get('quantity', 0) > 0 and not p.get('demo'):
                clean[code] = p
        self.strategy_positions = clean
        # 手动买入不占策略资金（与 Futu 同步加载/卖出路径语义一致）
        self.strategy_used_capital = sum(
            pos.get('quantity', 0) * pos.get('cost_price', 0)
            for pos in clean.values()
            if not pos.get('manual')
        )
        invalid_count = len(all_positions) - len(clean)
        if invalid_count > 0:
            logger.warning(f"[HK] 过滤掉{invalid_count}只无效持仓")
        logger.info(f"[HK] 已从 trading_state 恢复持仓: {len(clean)}只, "
                    f"策略已用资金: HKD {self.strategy_used_capital:.2f}")

    def _save_positions_to_db(self):
        """保存港股持仓到数据库"""
        # 保存状态（只保存有效持仓，quantity>0；剔除 _selling 运行时标记）
        valid_positions = {}
        for code, pos in self.strategy_positions.items():
            if pos.get('quantity', 0) > 0 and not pos.get('demo'):
                p = dict(pos)
                p.pop('_selling', None)
                valid_positions[code] = p
        # 手动买入不占策略资金（与 load / broker-sync / 卖出路径语义一致）
        self.strategy_used_capital = sum(
            pos.get('quantity', 0) * pos.get('cost_price', 0)
            for pos in valid_positions.values()
            if not pos.get('manual')
        )
        
        self.state_persistence.save_state(
            positions=valid_positions,
            used_capital=self.strategy_used_capital,
            capital=self.strategy_capital,
            last_buy_execution=int(__import__('time').time()),
            cooldowns=dict(self.recently_stopped)
        )
        # 保存明细和资金记录
        self._save_positions_detail()
        self._save_capital_record()

    def _save_positions_detail(self):
        """保存持仓明细"""
        # 如果策略持仓为空，跳过写入（避免清空 positions 表）
        real_positions = {}
        for c, p in self.strategy_positions.items():
            if not p.get('demo'):
                p2 = dict(p)
                p2.pop('_selling', None)
                real_positions[c] = p2
        if not real_positions:
            logger.info("[HK] 策略持仓为空，跳过写入 positions 表（保留现有手动标记）")
            return
        self.state_persistence.clear_positions()
        for stock_code, pos in real_positions.items():
            self.state_persistence.save_position(
                stock_code=stock_code,
                stock_name=self._get_stock_name(stock_code),
                quantity=pos.get('quantity', 0),
                cost_price=pos.get('cost_price', 0),
                highest_price=pos.get('highest_price', 0),
                manual=pos.get('manual', False),
                buy_time=pos.get('buy_time') or ''
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
        if not lot_size:
            logger.warning(f"[HK] 无法获取 {stock['code']} 每手股数，跳过该票买入")
            stock['quantity'] = 0
            return
        stock['quantity'] = int(stock['quantity'] * scale // lot_size) * lot_size

    def _check_exit_signals_impl(self, stock_code: str, quantity: int,
                                 cost_price: float, price: float,
                                 highest_price: float,
                                 entry_mode: str = 'bottom_fish') -> Tuple[bool, str, float, float, float]:
        """检查港股止盈止损信号 - 使用与回测一致的ATR策略"""
        from mutifactor.strategies.exit_strategy import ExitStrategyFactory
        from datetime import datetime, timedelta

        # 构建持仓信息：优先复用内存中的持久持仓记录。
        # 关键：buy_date 决定持仓天数（时间退出/RSRS 豁免/止损收紧），
        # _prev_rsrs 需要跨检查轮保留；若每次重建临时 dict，这些状态全部丢失，
        # 会导致 RSRS/时间退出在实盘永不生效。
        live_pos = self.strategy_positions.get(stock_code)
        if isinstance(live_pos, dict) and live_pos.get('quantity') == quantity:
            position = live_pos
            position.setdefault('stock_code', stock_code)
            position.setdefault('cost_price', cost_price)
            position['highest_price'] = highest_price
            if not position.get('buy_date'):
                position['buy_date'] = position.get('buy_time') or ''
        else:
            position = {
                'stock_code': stock_code,
                'quantity': quantity,
                'cost_price': cost_price,
                'highest_price': highest_price,
                'buy_date': '',
            }

        # 获取当日高低价
        today_high, today_low = self.price_fetcher.get_today_high_low(stock_code)

        # 检查是否为手动买入
        is_manual = self.strategy_positions.get(stock_code, {}).get('manual', False)

        # 结构止损（bottom_fish 阶段低点入场锚定）：参考低点 - buffer×日线ATR，
        # 只作为初始硬止损，不随价格上涨（锁盈交给吊顶/后续移动止盈）
        structure_stop = 0.0
        try:
            structure_stop = float((position or {}).get('structure_stop') or 0)
        except (TypeError, ValueError):
            structure_stop = 0.0
        if structure_stop > 0 and price <= structure_stop and not is_manual:
            logger.warning(
                f"[HK] {stock_code} 结构止损触发: 现价 {price:.3f} "
                f"<= 结构止损 {structure_stop:.3f}"
            )
            return True, 'structure_stop', 0.0, 0.0, structure_stop

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

        # bottom_fish 观察窗时间止损：结构止损入场的“博反弹”仓，
        # 若 window_days 个交易日内从未达到 买入价×(1+rebound_pct) → 逻辑失效
        live_rec = self.strategy_positions.get(stock_code, {})
        if (not is_manual and entry_mode == 'bottom_fish'
                and float(live_rec.get('structure_stop') or 0) > 0
                and kline_df is not None):
            try:
                dip_cfg = (self.config.get('trading', {})
                           .get('live_trading', {})
                           .get('buy_timing', {})
                           .get('smart', {})
                           .get('bottom_fish_daily') or {})
                if dip_cfg.get('short_time_stop_enabled', True):
                    window = int(dip_cfg.get('short_time_stop_days', 5))
                    rebound = float(dip_cfg.get('short_time_stop_rebound_pct', 0.03))
                    hit, held = _bottom_time_stop_hit(
                        live_rec.get('buy_date'), kline_df, price,
                        live_rec.get('highest_price'), cost_price,
                        window, rebound,
                    )
                    if hit:
                        logger.warning(
                            f"[HK] {stock_code} 观察窗时间止损: 持仓 {held} 个交易日"
                            f"未反弹 ≥{rebound * 100:.0f}%（成本 {cost_price:.3f}）"
                        )
                        return True, 'bottom_time_stop', 0.0, 0.0, float(price)
            except Exception as e:
                logger.debug(f"[HK] {stock_code} 观察窗时间止损计算失败: {e}")

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
