"""
策略基类 - 抽象通用策略逻辑
方便未来测试新的基于富途牛牛数据的模型
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
import logging

try:
    from numba import jit, njit
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    # 创建假的装饰器
    def jit(*args, **kwargs):
        return lambda f: f
    def njit(*args, **kwargs):
        return lambda f: f

from mutifactor.base_market_adapter import MarketAdapter
from mutifactor.data.base_fetcher import MarketType


class BaseStrategy(ABC):
    """策略基类"""

    # ==================== 颜色常量 ====================
    COLOR_GREEN = "\033[92m"
    COLOR_RED = "\033[91m"
    COLOR_RESET = "\033[0m"

    # ==================== 图标常量 ====================
    ICON_PROFIT = "✅"
    ICON_LOSS = "❌"

    # ==================== 技术指标常量 ====================
    MIN_HISTORICAL_DAYS = 30  # 技术指标计算所需最小历史天数
    DEFAULT_VOLATILITY = 0.3  # 默认年化波动率（30%）
    VOLATILITY_CALCULATION_DAYS = 252  # 年化交易日数
    MIN_DATA_COVERAGE_RATIO = 0.8  # 波动率计算最小数据覆盖率


    # ==================== 卖出原因常量（与 exit_strategy.py 保持一致） ====================
    EXIT_REASON_STOP_LOSS = 'atr_stop_loss'
    EXIT_REASON_TAKE_PROFIT = 'atr_take_profit'
    EXIT_REASON_TIME_EXIT = 'time_exit'

    # 卖出原因中文映射
    EXIT_REASON_NAMES = {
        EXIT_REASON_STOP_LOSS: 'ATR止损',
        EXIT_REASON_TAKE_PROFIT: 'ATR止盈',
        EXIT_REASON_TIME_EXIT: '到期平仓',
    }

    def __init__(self, initial_capital: float = None,
                 market_type: MarketType = MarketType.HK,
                 config: Dict = None):
        """
        初始化策略

        Args:
            initial_capital: 初始资金,None则使用配置文件默认值
            market_type: 市场类型,默认港股
            config: 配置字典
        """
        self.initial_capital = initial_capital if initial_capital is not None else (config or {}).get('strategy', {}).get('initial_capital', 100000.0)
        self.market_type = market_type
        self.config = config or {}

        self.cash = self.initial_capital
        self.margin_multiplier = self.config.get('risk', {}).get('margin_multiplier', 1.0)  # 融资倍数
        self.positions = {}        # 所有在仓位的股票 {stock_code: position_dict}
        self.buy_records = {}      # 量化买入记录 {stock_code: buy_info}，有记录才算量化仓位
        self.recently_stopped = {}  # 止损冷却期 {stock_code: sell_date}，冷却期内不重买
        # 使用统一的'backtest' logger,确保日志输出
        self.logger = logging.getLogger('backtest')

        # 市场适配器(处理不同市场的交易规则)
        self.market_adapter = MarketAdapter(market_type)

        # 股票名称映射（由外部设置）
        self.stock_names = {}

        # 累计交易成本
        self.total_commission = 0.0
        self.total_trading_fee = 0.0
        self.total_settlement_fee = 0.0
        self.total_slippage = 0.0
        self.total_stamp_duty = 0.0
        self.total_turnover = 0.0  # 累计总成交额（买入+卖出）

    def calculate_trading_cost(self, amount: float, direction: str = 'buy',
                              stock_code: str = None) -> dict:
        """
        计算交易成本(通用接口,支持不同市场)

        Args:
            amount: 交易金额
            direction: 方向 'buy' 或 'sell'
            stock_code: 股票代码

        Returns:
            成本字典 {commission, stamp_duty, trading_fee, settlement_fee, slippage, total}
        """
        return self.market_adapter.calculate_trading_cost(amount, direction, stock_code)

    @abstractmethod
    def select_stocks(self, stocks_data: Dict[str, pd.DataFrame],
                      current_date: str) -> List[str]:
        """
        选股逻辑(需子类实现)

        Args:
            stocks_data: 股票数据字典 {stock_code: DataFrame}
            current_date: 当前日期

        Returns:
            做多候选列表
        """
        pass

    @abstractmethod
    def calculate_position_size(self, stock_code: str, price: float,
                                 available_cash: float) -> int:
        """
        计算仓位大小(需子类实现)

        Args:
            stock_code: 股票代码
            price: 价格
            available_cash: 可用资金

        Returns:
            交易股数
        """
        pass

    @abstractmethod
    def check_exit_signal(self, stock_code: str, position: dict,
                          current_price: float, current_date: str) -> Optional[str]:
        """
        检查平仓信号(需子类实现)

        Args:
            stock_code: 股票代码
            position: 持仓信息
            current_price: 当前价格
            current_date: 当前日期

        Returns:
            平仓原因,None则不平仓
        """
        pass

    def calculate_volatility(self, df: pd.DataFrame, window: int = 20) -> float:
        """
        计算股票波动率

        Args:
            df: 股票历史数据
            window: 计算窗口（天）

        Returns:
            年化波动率
        """
        if len(df) < window:
            return self.DEFAULT_VOLATILITY

        try:
            # 使用numba优化的版本（如果可用）
            if NUMBA_AVAILABLE:
                return self._calculate_volatility_numba(df['close'].values, window,
                                                      self.VOLATILITY_CALCULATION_DAYS,
                                                      self.MIN_DATA_COVERAGE_RATIO,
                                                      self.DEFAULT_VOLATILITY)
            else:
                # 使用收益率的标准差
                returns = df['close'].pct_change().tail(window).dropna()
                if len(returns) < window * self.MIN_DATA_COVERAGE_RATIO:
                    return self.DEFAULT_VOLATILITY

                # 年化波动率
                volatility = returns.std() * np.sqrt(self.VOLATILITY_CALCULATION_DAYS)
                # 检查 nan 和无效值
                if np.isnan(volatility) or volatility <= 0:
                    return self.DEFAULT_VOLATILITY
                return volatility
        except (ValueError, TypeError, ZeroDivisionError):
            return self.DEFAULT_VOLATILITY

    @staticmethod
    @njit
    def _calculate_volatility_numba(prices: np.ndarray, window: int,
                                   annualization_factor: float,
                                   min_coverage_ratio: float,
                                   default_volatility: float) -> float:
        """
        Numba优化的波动率计算

        Args:
            prices: 收盘价数组
            window: 计算窗口
            annualization_factor: 年化因子（sqrt(252)）
            min_coverage_ratio: 最小数据覆盖率
            default_volatility: 默认波动率

        Returns:
            年化波动率
        """
        if len(prices) < window:
            return default_volatility

        # 计算收益率
        returns = np.diff(prices) / prices[:-1]

        # 取最近window个数据
        if len(returns) < window:
            return default_volatility

        recent_returns = returns[-window:]

        # 检查数据覆盖率
        valid_returns = recent_returns[~np.isnan(recent_returns)]
        if len(valid_returns) < window * min_coverage_ratio:
            return default_volatility

        # 计算标准差
        if len(valid_returns) == 0:
            return default_volatility

        mean_return = np.mean(valid_returns)
        variance = np.mean((valid_returns - mean_return) ** 2)
        std_dev = np.sqrt(variance)

        # 年化
        volatility = std_dev * annualization_factor

        # 检查无效值
        if np.isnan(volatility) or volatility <= 0:
            return default_volatility

        return volatility

    def calculate_volatility_weights(self, selected_stocks: List[str],
                                     stocks_data: Dict[str, pd.DataFrame],
                                     window: int = 20,
                                     min_weight: float = 0.1) -> Dict[str, float]:
        """
        基于波动率计算仓位权重（波动率越低，权重越大）

        Args:
            selected_stocks: 选中的股票列表
            stocks_data: 股票数据
            window: 波动率计算窗口
            min_weight: 最小权重

        Returns:
            股票权重字典 {stock_code: weight}
        """
        if len(selected_stocks) == 0:
            return {}

        # 向量化计算波动率
        volatilities = self._calculate_volatilities_vectorized(
            selected_stocks, stocks_data, window
        )

        # 使用波动率的倒数作为权重（波动率越低，权重越大）
        inverse_vols = {code: 1.0 / vol for code, vol in volatilities.items()}

        # 归一化权重
        total_inverse_vol = sum(inverse_vols.values())
        weights = {code: inv_vol / total_inverse_vol for code, inv_vol in inverse_vols.items()}

        # 应用最小权重限制
        if min_weight > 0:
            min_weight_value = min_weight / len(selected_stocks)
            weights = {code: max(w, min_weight_value) for code, w in weights.items()}

            # 重新归一化
            total_weight = sum(weights.values())
            weights = {code: w / total_weight for code, w in weights.items()}

        self.logger.info(f"波动率加权分配:")
        for code, w in sorted(weights.items(), key=lambda x: x[1], reverse=True):
            vol = volatilities.get(code, 0)
            self.logger.info(f"  {code}: 权重={w*100:.1f}%, 波动率={vol*100:.1f}%")

        return weights

    def _calculate_volatilities_vectorized(self, selected_stocks: List[str],
                                           stocks_data: Dict[str, pd.DataFrame],
                                           window: int = 20) -> Dict[str, float]:
        """
        向量化计算多个股票的波动率

        Args:
            selected_stocks: 选中的股票列表
            stocks_data: 股票数据（必须是已截断到当前日期的数据）
            window: 波动率计算窗口

        Returns:
            股票波动率字典
        """
        volatilities = {}

        for stock_code in selected_stocks:
            df = stocks_data.get(stock_code)
            if df is not None and len(df) > 0:
                # 直接使用传入的已截断数据，不再使用self._price_data
                vol = self.calculate_volatility(df, window)
                volatilities[stock_code] = vol
            else:
                volatilities[stock_code] = self.DEFAULT_VOLATILITY

        return volatilities

    def execute_long_trade(self, selected_stocks: List[str],
                           stocks_data: Dict[str, pd.DataFrame],
                           current_date: str,
                           historical_data: Dict[str, pd.DataFrame] = None,
                           next_day_data: Dict[str, pd.DataFrame] = None) -> List[Dict]:
        """执行做多交易"""
        if not selected_stocks:
            return []

        buy_orders = []

        if len(selected_stocks) == 0:
            return []

        # 从YAML配置中获取风险参数
        max_single_position_ratio = self.config.get('risk', {}).get('max_single_position_ratio', 0.5)
        volatility_weighted_enabled = self.config.get('risk', {}).get('volatility_weighted_enabled', False)

        if volatility_weighted_enabled and len(selected_stocks) > 1:
            # 使用波动率加权分配资金
            vol_window = self.config.get('risk', {}).get('volatility_window', 20)
            min_weight = self.config.get('risk', {}).get('volatility_min_weight', 0.1)

            weights = self.calculate_volatility_weights(
                selected_stocks, historical_data or stocks_data, vol_window, min_weight
            )

            # 根据权重分配资金
            stock_cash = {}
            for stock_code in selected_stocks:
                weight = weights.get(stock_code, 1.0 / len(selected_stocks))
                cash_for_stock = self.cash * weight
                # 限制单只股票最大仓位
                cash_for_stock = min(cash_for_stock, self.cash * max_single_position_ratio)
                stock_cash[stock_code] = cash_for_stock
        else:
            # 原有的平均分配逻辑
            total_position_cash = self.cash
            avg_cash_per_stock = total_position_cash / len(selected_stocks)
            cash_per_stock = min(avg_cash_per_stock, self.cash * max_single_position_ratio)

            stock_cash = {code: cash_per_stock for code in selected_stocks}

        for stock_code in selected_stocks:
            df = stocks_data.get(stock_code)
            if df is None or len(df) == 0:
                continue

            try:
                # 优先使用次日开盘价，其次当天开盘价，最后收盘价
                exec_price = 0
                if next_day_data:
                    next_df = next_day_data.get(stock_code)
                    if next_df is not None and len(next_df) > 0:
                        next_open = next_df['open'].values[-1]
                        if next_open > 0:
                            exec_price = next_open
                
                # fallback: 当天开盘价
                if exec_price <= 0:
                    exec_price = df['open'].values[-1]
                
                # fallback: 当天收盘价
                if exec_price <= 0:
                    exec_price = df['close'].values[-1]
                if exec_price <= 0:
                    continue

                close_price = exec_price  # 保持变量名不变，兼容后续逻辑

                # 使用波动率加权后的资金分配
                cash_for_stock = stock_cash.get(stock_code, 0)
                shares = self.calculate_position_size(stock_code, close_price, cash_for_stock)
                if shares == 0:
                    continue

                cost = shares * close_price

                # 计算买入成本(含手续费和滑点)
                buy_cost = self.calculate_trading_cost(cost, direction='buy', stock_code=stock_code)
                total_cost = cost + buy_cost['total']

                # 止损冷却期检查（每次循环清理过期记录）
                cooldown_days = self.config.get('risk', {}).get('stop_loss_cooldown_days', 5)
                for sc in list(self.recently_stopped.keys()):
                    stop_date = self.recently_stopped.get(sc)
                    if stop_date and current_date:
                        days_since = (pd.to_datetime(current_date) - pd.to_datetime(stop_date)).days
                        if days_since >= cooldown_days:
                            del self.recently_stopped[sc]

                if stock_code in self.recently_stopped:
                    self.logger.debug(f"跳过 {stock_code}: 止损冷却期({cooldown_days}天)")
                    continue

                max_affordable = self.cash * self.margin_multiplier
                if total_cost > max_affordable:
                    shortfall = total_cost - self.cash
                    self.logger.warning(f"资金不足,无法买入 {stock_code} (需要:{total_cost:.2f}, 可用:{self.cash:.2f}, 缺口:{shortfall:.2f})")
                    continue

                self.cash -= total_cost
                self.total_turnover += cost
                self.total_commission += buy_cost['commission']
                self.total_trading_fee += buy_cost['trading_fee']
                self.total_settlement_fee += buy_cost['settlement_fee']
                self.total_slippage += buy_cost['slippage']
                self.positions[stock_code] = {
                    'shares': shares,
                    'cost_price': close_price,
                    'buy_date': current_date,
                    'direction': 'long',
                    'highest_price': close_price  # 用于移动止盈
                }

                # 记录量化买入：有记录才算量化仓位（占用本金）
                self.buy_records[stock_code] = {
                    'shares': shares,
                    'cost_price': close_price,
                    'buy_date': current_date
                }

                buy_orders.append({
                    'stock_code': stock_code,
                    'shares': shares,
                    'price': close_price,
                    'cost': cost
                })

            except (KeyError, ValueError, TypeError, IndexError) as e:
                self.logger.warning(f"买入股票数据错误 {stock_code}: {e}")
                continue
            except Exception as e:
                self.logger.error(f"买入股票未知错误 {stock_code}: {e}", exc_info=True)
                raise

        return buy_orders

    def close_positions(self, stocks_data: Dict[str, pd.DataFrame],
                         current_date: str) -> List[Dict]:
        """平仓检查"""
        stocks_to_close = []

        # 将 current_date 转换为字符串用于匹配
        if isinstance(current_date, str):
            date_str = current_date
        else:
            date_str = current_date.strftime('%Y-%m-%d')

        for stock_code, position in self.positions.items():
            df = stocks_data.get(stock_code)
            if df is None or len(df) == 0:
                continue

            try:
                current_price = df['open'].values[-1]  # open
                if current_price <= 0:
                    current_price = df['close'].values[-1]

                # 获取截断到当前日期的历史数据（避免未来数据泄露）
                historical_data = None
                if hasattr(self, '_price_data'):
                    full_df = self._price_data.get(stock_code)
                    if full_df is not None and len(full_df) > 0:
                        date_match = full_df[full_df['date'].dt.strftime('%Y-%m-%d') == date_str]
                        if len(date_match) > 0:
                            idx = date_match.index[0]
                            # 截断到当前日期（含当天），用于ATR等指标计算
                            historical_data = full_df.iloc[:idx + 1]

                # 如果策略需要历史数据，提供截断后的数据
                if hasattr(self, '_stock_data_cache'):
                    self._stock_data_cache[stock_code] = historical_data

                close_reason = self.check_exit_signal(
                    stock_code, position, current_price, current_date
                )

                if close_reason:
                    # 计算收益率和持有天数
                    cost_price = position.get('cost_price', 0)
                    buy_date = position.get('buy_date', '')
                    sell_date = current_date

                    # 计算收益率
                    if cost_price > 0:
                        profit_pct = (current_price - cost_price) / cost_price * 100
                    else:
                        profit_pct = 0

                    # 计算持有天数
                    if buy_date and sell_date:
                        try:
                            buy_dt = pd.to_datetime(buy_date)
                            sell_dt = pd.to_datetime(sell_date)
                            holding_days = (sell_dt - buy_dt).days
                        except (ValueError, TypeError):
                            holding_days = 0
                    else:
                        holding_days = 0

                    stocks_to_close.append({
                        'stock_code': stock_code,
                        'shares': position['shares'],
                        'buy_price': cost_price,
                        'sell_price': current_price,
                        'reason': close_reason,
                        'direction': position.get('direction', 'long'),
                        'buy_date': buy_date,
                        'sell_date': sell_date,
                        'profit_pct': profit_pct,
                        'holding_days': holding_days
                    })

            except (KeyError, ValueError, TypeError, IndexError) as e:
                self.logger.warning(f"平仓检查数据错误 {stock_code}: {e}")
                continue
            except Exception as e:
                self.logger.error(f"平仓检查未知错误 {stock_code}: {e}", exc_info=True)
                raise

        for close_info in stocks_to_close:
            self._execute_close(close_info, current_date, stocks_data)

        return stocks_to_close

    def _execute_close(self, close_info: Dict, current_date: str,
                       stocks_data: Dict[str, pd.DataFrame]):
        """执行单个平仓操作"""
        stock_code = close_info['stock_code']
        position = self.positions.get(stock_code)
        if not position:
            return

        price = close_info['sell_price']  # 使用sell_price字段
        direction = close_info['direction']
        reason = close_info['reason']

        if direction == 'long':
            revenue = position['shares'] * price
            cost_price = position['cost_price']
            buy_date = position['buy_date']

            # 计算盈亏
            profit_pct = (price - cost_price) / cost_price * 100

            # 计算持仓天数
            buy_date_obj = pd.to_datetime(buy_date)
            current_date_obj = pd.to_datetime(current_date)
            holding_days = (current_date_obj - buy_date_obj).days

            # 计算卖出手续费
            sell_cost = self.calculate_trading_cost(revenue, direction='sell', stock_code=stock_code)
            net_revenue = revenue - sell_cost['total']

            # 只有量化仓位（有买入记录）才回现金、统计手续费
            if stock_code in self.buy_records:
                self.cash += net_revenue
                del self.buy_records[stock_code]
                self.total_turnover += revenue
                self.total_commission += sell_cost['commission']
                self.total_stamp_duty += sell_cost['stamp_duty']
                self.total_trading_fee += sell_cost['trading_fee']
                self.total_settlement_fee += sell_cost['settlement_fee']
                self.total_slippage += sell_cost['slippage']
            # 无买入记录的仓位（手动监控）卖出不回现金

            # 计算盈亏金额
            profit_amount = revenue - (cost_price * position['shares'])

            # 打印卖出信息 - 添加颜色图标和股票名称
            profit_icon = self.ICON_PROFIT if profit_pct >= 0 else self.ICON_LOSS
            profit_color = self.COLOR_GREEN if profit_pct >= 0 else self.COLOR_RED

            # 获取股票名称
            stock_name = self.stock_names.get(stock_code, '')
            stock_display = f"[{stock_code} {stock_name}]" if stock_name else f"[{stock_code}]"

            # 原因映射为中文
            reason_cn = self.EXIT_REASON_NAMES.get(reason, reason)

            # 根据市场类型确定货币单位
            currency = 'HKD'
            if self.market_type == MarketType.SH or self.market_type == MarketType.SZ:
                currency = 'CNY'

            self.logger.info(
                f"卖出 {stock_display} {position['shares']}股 @ {price:.3f} | "
                f"买入价: {cost_price:.3f} | "
                f"盈亏: {profit_icon} {profit_color}{profit_pct:+.2f}% ({profit_amount:+,.0f} {currency}){self.COLOR_RESET} | "
                f"持仓: {holding_days}天 ({buy_date} -> {current_date}) | "
                f"原因: {reason_cn}"
            )

        del self.positions[stock_code]

        # 止损卖出 → 加入冷却期（冷却期内不重买）
        if reason == self.EXIT_REASON_STOP_LOSS:
            self.recently_stopped[stock_code] = current_date
            self.logger.debug(f"冷却期: {stock_code} 止损卖出，加入冷却期至 {current_date}")

    def get_portfolio_value(self, stocks_data: Dict[str, pd.DataFrame], current_date: str = None) -> float:
        """计算量化组合价值（只算有买入记录的仓位）"""
        position_value = 0.0

        # 只统计量化仓位（有买入记录的）
        for stock_code in self.buy_records:
            position = self.positions.get(stock_code)
            if not position:
                continue
            df = stocks_data.get(stock_code)
            if df is None or len(df) == 0:
                # fallback: 从完整历史数据中取当前日期之前的最近收盘价
                if hasattr(self, '_price_data') and current_date:
                    full_df = self._price_data.get(stock_code)
                    if full_df is not None and len(full_df) > 0:
                        # 筛选当前日期及之前的记录，取最后一条收盘价
                        prior_data = full_df[full_df['date'].dt.strftime('%Y-%m-%d') <= current_date]
                        if len(prior_data) > 0:
                            current_price = float(prior_data['close'].iloc[-1])
                            if current_price > 0:
                                direction = position.get('direction', 'long')
                                if direction == 'long':
                                    position_value += position['shares'] * current_price
                continue

            try:
                prices = df[['open', 'close']].values
                last_prices = [p for p in prices if p[0] > 0 or p[1] > 0]
                if last_prices:
                    current_price = last_prices[-1][1]  # close
                    if current_price <= 0:
                        current_price = last_prices[-1][0]  # fallback open
                direction = position.get('direction', 'long')
                if direction == 'long':
                    position_value += position['shares'] * current_price
            except (KeyError, ValueError, TypeError, IndexError):
                continue

        return self.cash + position_value

    def add_manual_position(self, stock_code: str, shares: int, cost_price: float,
                            current_date: str):
        """
        添加手动仓位（不占量化本金，只进ATR监控）
        不记录到buy_records，卖出不回现金，市值不计入组合价值
        """
        self.positions[stock_code] = {
            'shares': shares,
            'cost_price': cost_price,
            'buy_date': current_date,
            'direction': 'long',
            'highest_price': cost_price
        }
        self.logger.info(f"[手动仓位] 添加监控 {stock_code} {shares}股 @{cost_price}")

    def run_backtest(self, price_data: Dict[str, pd.DataFrame],
                     start_date: str = None, end_date: str = None) -> Dict:
        """运行回测"""
        self.logger.info("开始回测")

        # 保存完整的价格数据供子策略使用（ATR计算需要）
        self._price_data = price_data
        
        # 初始化K线数据缓存（用于止盈止损计算）
        if not hasattr(self, '_stock_data_cache'):
            self._stock_data_cache = {}

        for stock_code, df in price_data.items():
            if 'date' not in df.columns:
                if df.index.name == 'date':
                    df = df.reset_index()
                else:
                    df['date'] = pd.to_datetime(df.index)

        all_dates = set()
        for df in price_data.values():
            # 只取有实际成交量的交易日，过滤掉非交易日的假数据
            valid_df = df[df['volume'] > 0] if 'volume' in df.columns else df
            all_dates.update(valid_df['date'].dt.strftime('%Y-%m-%d').tolist())
        all_dates = sorted(list(all_dates))

        if start_date:
            all_dates = [d for d in all_dates if d >= start_date]
        if end_date:
            all_dates = [d for d in all_dates if d <= end_date]

        self.logger.info(f"回测日期范围: {all_dates[0]} 至 {all_dates[-1]}, 共{len(all_dates)}个交易日")

        equity_curve = []
        positions_history = []
        trade_history = []

        last_selected_long = []
        total_dates = len(all_dates)
        for i, date in enumerate(all_dates):
            if isinstance(date, str):
                date_str = date
            else:
                date_str = date.strftime('%Y-%m-%d')

            daily_data = {}
            for stock_code, df in price_data.items():
                stock_date_data = df[df['date'].dt.strftime('%Y-%m-%d') == date_str]
                if len(stock_date_data) > 0:
                    daily_data[stock_code] = stock_date_data

            if not daily_data:
                continue

            # 上午交易时间: 先执行卖出(9:50)
            close_orders = self.close_positions(daily_data, date)
            trade_history.extend(close_orders)

            # 选股(使用历史数据)
            historical_data = {}
            for stock_code, df in price_data.items():
                date_idx = df[df['date'].dt.strftime('%Y-%m-%d') == date_str].index
                if len(date_idx) > 0:
                    idx = date_idx[0]
                    # 提供至少30天的历史数据用于技术指标计算(策略需要)
                    if idx >= self.MIN_HISTORICAL_DAYS:
                        historical_data[stock_code] = df.iloc[:idx]

            if historical_data:
                last_selected_long = self.select_stocks(historical_data, date_str)


            # 获取次日数据用于买入成交价
            next_day_data = {}
            if i + 1 < total_dates:
                next_date_str = all_dates[i + 1]
                for stock_code, df in price_data.items():
                    stock_date_data = df[df['date'].dt.strftime('%Y-%m-%d') == next_date_str]
                    if len(stock_date_data) > 0:
                        next_day_data[stock_code] = stock_date_data

            self.execute_long_trade(last_selected_long, daily_data, date, historical_data, next_day_data)

            # 每50个交易日输出一次进度
            if (i + 1) % 50 == 0:
                progress = (i + 1) / total_dates * 100
                self.logger.info(f"回测进度: {i+1}/{total_dates} ({progress:.1f}%), 当前日期: {date_str}, 持仓: {len(self.positions)}只")

            portfolio_value = self.get_portfolio_value(daily_data, date_str)
            equity_curve.append({
                'date': date,
                'value': portfolio_value,
                'cash': self.cash,
                'position_count': len(self.positions)
            })

            if self.positions:
                positions_history.append({
                    'date': date,
                    'positions': list(self.positions.keys())
                })

        # 回测结束，强制平掉所有持仓
        if self.positions:
            self.logger.info(f"\n回测结束，强制平仓 {len(self.positions)} 只持仓...")
            last_date = all_dates[-1]
            if isinstance(last_date, str):
                last_date_str = last_date
            else:
                last_date_str = last_date.strftime('%Y-%m-%d')

            # 使用最后一天的数据
            daily_data = {}
            for stock_code, df in price_data.items():
                stock_date_data = df[df['date'].dt.strftime('%Y-%m-%d') == last_date_str]
                if len(stock_date_data) > 0:
                    daily_data[stock_code] = stock_date_data

            # 强制平仓
            for stock_code in list(self.positions.keys()):
                position = self.positions[stock_code]
                df = daily_data.get(stock_code)
                if df is not None and len(df) > 0:
                    current_price = df['open'].values[-1]  # open
                    if current_price <= 0:
                        current_price = df['close'].values[-1]
                    close_info = {
                        'stock_code': stock_code,
                        'shares': position['shares'],
                        'sell_price': current_price,
                        'reason': self.EXIT_REASON_TIME_EXIT,
                        'direction': position.get('direction', 'long')
                    }
                    self._execute_close(close_info, last_date_str, daily_data)
                    trade_history.append(close_info)

        return self._calculate_results(equity_curve, trade_history, positions_history)

    def _calculate_results(self, equity_curve: List[Dict],
                           trade_history: List[Dict],
                           positions_history: List[Dict]) -> Dict:
        """计算回测结果"""
        currency = 'HKD'
        equity_df = pd.DataFrame(equity_curve)

        if len(equity_df) == 0:
            self.logger.warning("回测无数据")
            return {}

        equity_df['return'] = equity_df['value'].pct_change()
        total_return = (equity_df['value'].iloc[-1] / self.initial_capital) - 1
        annual_return = (1 + total_return) ** (252 / len(equity_df)) - 1

        equity_df['cummax'] = equity_df['value'].cummax()
        equity_df['drawdown'] = (equity_df['value'] - equity_df['cummax']) / equity_df['cummax']
        max_drawdown = equity_df['drawdown'].min()

        equity_df['return'] = equity_df['return'].fillna(0)
        return_std = equity_df['return'].std()
        sharpe_ratio = equity_df['return'].mean() / return_std * np.sqrt(self.VOLATILITY_CALCULATION_DAYS) if return_std > 0 else 0

        total_trades = len(trade_history)
        winning_trades = sum(1 for t in trade_history if t.get('profit_pct', 0) > 0)
        win_rate = winning_trades / total_trades if total_trades > 0 else 0

        results = {
            'initial_capital': self.initial_capital,
            'final_value': equity_df['value'].iloc[-1],
            'total_return': total_return,
            'annual_return': annual_return,
            'max_drawdown': max_drawdown,
            'sharpe_ratio': sharpe_ratio,
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'win_rate': win_rate,
            'equity_curve': equity_df,
            'trade_history': pd.DataFrame(trade_history),
            'positions_history': positions_history,
            # 交易成本
            'total_commission': self.total_commission,
            'total_stamp_duty': self.total_stamp_duty,
            'total_trading_fee': self.total_trading_fee,
            'total_settlement_fee': self.total_settlement_fee,
            'total_slippage': self.total_slippage,
            'total_cost': self.total_commission + self.total_stamp_duty +
                         self.total_trading_fee + self.total_settlement_fee +
                         self.total_slippage
        }

        self.logger.info("=" * 60)
        self.logger.info("回测结果汇总:")
        self.logger.info(f"初始资金: {self.initial_capital:,.2f} {currency}")
        self.logger.info(f"最终价值: {results['final_value']:,.2f} {currency}")
        self.logger.info(f"总收益率: {total_return*100:.2f}%")
        self.logger.info(f"年化收益率: {annual_return*100:.2f}%")
        self.logger.info(f"最大回撤: {max_drawdown*100:.2f}%")
        self.logger.info(f"夏普比率: {sharpe_ratio:.2f}")
        self.logger.info(f"交易次数: {total_trades}")
        self.logger.info(f"胜率: {win_rate*100:.2f}%")
        self.logger.info("-" * 60)
        self.logger.info("交易成本明细:")
        self.logger.info(f"  佣金: {self.total_commission:,.2f} {currency}")
        self.logger.info(f"  印花税: {self.total_stamp_duty:,.2f} {currency}")
        self.logger.info(f"  交易征费: {self.total_trading_fee:,.2f} {currency}")
        self.logger.info(f"  结算费: {self.total_settlement_fee:,.2f} {currency}")
        self.logger.info(f"  滑点: {self.total_slippage:,.2f} {currency}")
        self.logger.info(f"  总成本: {results['total_cost']:,.2f} {currency} ({results['total_cost']/self.total_turnover*100:.4f}% of turnover)" if self.total_turnover > 0 else f"  总成本: {results['total_cost']:,.2f} {currency}")
        self.logger.info("=" * 60)

        return results
