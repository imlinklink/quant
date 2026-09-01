"""
双吊灯策略单股回测引擎
输入: 股票代码 + 买入日期 + 方向
输出: 止盈/止损触发点、盈亏、K线图
"""
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field

from mutifactor.data.base_fetcher import KLineType
from mutifactor.strategies.dual_chandelier import DualChandelierExitStrategy, PositionExitState

logger = logging.getLogger(__name__)

# 设置matplotlib字体
import matplotlib
matplotlib.use('Agg')  # 设置非交互式后端
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# 全局设置中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示问题


# K线类型映射: config字符串 → KLineType
KLINE_TYPE_MAP = {
    '1min': KLineType.MIN_1,
    '5min': KLineType.MIN_5,
    '15min': KLineType.MIN_15,
    '30min': KLineType.MIN_30,
    '60min': KLineType.MIN_60,
}


@dataclass
class TradeResult:
    """单次回测结果"""
    stock_code: str
    direction: str  # 'long' or 'short'
    entry_date: str
    entry_price: float
    exit_date: Optional[str] = None
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: str = "HOLD"  # STOP_LOSS / TAKE_PROFIT / HOLD / TIMEOUT
    pnl_pct: float = 0.0
    holding_bars: int = 0
    holding_days: int = 0
    max_profit_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    activated_profit: bool = False
    # 通道线历史（用于绘图）
    stop_line_history: List[float] = field(default_factory=list)
    profit_line_history: List[Optional[float]] = field(default_factory=list)
    price_history: List[float] = field(default_factory=list)
    time_history: List[datetime] = field(default_factory=list)


class ChandelierBacktester:
    """双吊灯策略单股回测器"""

    def __init__(self, fetcher, config: Dict = None):
        """
        Args:
            fetcher: 数据获取器（FutuUSDataFetcher 实例）
            config: 回测配置，来自 config.yaml 的 backtest_chandelier 段
        """
        self.fetcher = fetcher
        self.config = config or {}

        # 策略参数
        chandelier_cfg = self.config.get('chandelier', {})
        self.atr_period = chandelier_cfg.get('atr_period', 14)
        # 4阶段混合策略参数
        self.fixed_stop_pct = chandelier_cfg.get('fixed_stop_pct', 0.05)
        self.breakeven_pct = chandelier_cfg.get('breakeven_pct', 0.03)
        self.trailing_activate_pct = chandelier_cfg.get('trailing_activate_pct', 0.06)
        self.trailing_pullback_pct = chandelier_cfg.get('trailing_pullback_pct', 0.03)
        self.atr_threshold_pct = chandelier_cfg.get('atr_threshold_pct', 0.20)
        self.atr_trailing_mult = chandelier_cfg.get('atr_trailing_mult', 2.0)

        # 回测参数
        self.holding_days = self.config.get('holding_days', 22)
        self.kline_type_str = self.config.get('kline_type', '5min')
        self.kline_type = KLINE_TYPE_MAP.get(self.kline_type_str, KLineType.MIN_5)
        self.direction = self.config.get('direction', 'long')

        logger.info(f"回测器初始化: K线={self.kline_type_str}, ATR周期={self.atr_period}, "
                    f"固定止损-{self.fixed_stop_pct*100:.0f}% | 保本+{self.breakeven_pct*100:.0f}% | "
                    f"Trailing+{self.trailing_activate_pct*100:.0f}%↓{self.trailing_pullback_pct*100:.0f}% | "
                    f"ATR模式≥{self.atr_threshold_pct*100:.0f}%({self.atr_trailing_mult}x), 持仓{self.holding_days}天")

    def _compute_atr_series(self, df: pd.DataFrame, period: int) -> pd.Series:
        """计算ATR序列（基于分钟K线）"""
        high = df['high']
        low = df['low']
        close = df['close']

        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = tr.rolling(window=period).mean()
        return atr

    def _fetch_minute_klines(self, stock_code: str, start_date: str,
                              end_date: str) -> Optional[pd.DataFrame]:
        """
        分段拉取分钟K线数据
        富途每次最多返回约1000条，需要按周分段拉取
        """
        start_dt = pd.Timestamp(start_date)
        end_dt = pd.Timestamp(end_date)

        # 根据K线类型确定分段大小
        if self.kline_type == KLineType.MIN_1:
            chunk_days = 4  # 1分钟K线每次4天
        elif self.kline_type == KLineType.MIN_5:
            chunk_days = 15  # 5分钟K线每次15天
        elif self.kline_type == KLineType.MIN_15:
            chunk_days = 40  # 15分钟K线每次40天
        else:
            chunk_days = 20

        all_dfs = []
        current_start = start_dt

        while current_start <= end_dt:
            current_end = min(current_start + timedelta(days=chunk_days), end_dt)

            start_str = current_start.strftime('%Y-%m-%d')
            end_str = current_end.strftime('%Y-%m-%d')

            logger.info(f"  拉取K线: {start_str} ~ {end_str} ({self.kline_type_str})")
            df = self.fetcher.fetch_stock_kline(
                stock_code, start_str, end_str, ktype=self.kline_type
            )

            if df is not None and len(df) > 0:
                all_dfs.append(df)
            else:
                logger.warning(f"  段无数据: {start_str} ~ {end_str}")

            current_start = current_end + timedelta(days=1)

        if not all_dfs:
            return None

        result = pd.concat(all_dfs, ignore_index=True)
        result = result.drop_duplicates(subset=['date']).sort_values('date').reset_index(drop=True)
        logger.info(f"  共获取 {len(result)} 条K线数据")
        return result

    def run(self, stock_code: str, entry_date: str,
            direction: str = None, entry_price: float = None) -> TradeResult:
        """
        运行单股回测

        Args:
            stock_code: 股票代码，如 'US.AAPL'
            entry_date: 买入日期，如 '2026-03-01'
            direction: 'long' 或 'short'，默认用配置值
            entry_price: 指定买入价，None则用次日开盘价

        Returns:
            TradeResult 回测结果
        """
        direction = direction or self.direction

        # 计算数据拉取范围：买入日前7天（预热ATR）到买入日+holding_days
        entry_dt = pd.Timestamp(entry_date)
        warmup_start = (entry_dt - timedelta(days=10)).strftime('%Y-%m-%d')
        end_date = (entry_dt + timedelta(days=self.holding_days + 5)).strftime('%Y-%m-%d')

        logger.info(f"=== 回测 {stock_code} [{direction}] 买入日={entry_date} ===")
        logger.info(f"数据范围: {warmup_start} ~ {end_date}")

        # 拉取分钟K线
        df = self._fetch_minute_klines(stock_code, warmup_start, end_date)
        if df is None or len(df) == 0:
            logger.error(f"无法获取K线数据: {stock_code}")
            return TradeResult(
                stock_code=stock_code, direction=direction,
                entry_date=entry_date, entry_price=0
            )

        # 计算ATR
        df['atr'] = self._compute_atr_series(df, self.atr_period)

        # 找到买入日次日的K线起点
        entry_date_ts = pd.Timestamp(entry_date)
        trading_mask = df['date'] > entry_date_ts
        trading_df = df[trading_mask].reset_index(drop=True)

        if len(trading_df) == 0:
            logger.error(f"买入日之后无K线数据")
            return TradeResult(
                stock_code=stock_code, direction=direction,
                entry_date=entry_date, entry_price=0
            )

        # 确定买入价
        if entry_price is None:
            entry_price = trading_df.iloc[0]['open']

        # 计算持仓截止时间
        cutoff_dt = entry_dt + timedelta(days=self.holding_days)

        logger.info(f"买入价={entry_price:.2f}, 方向={direction}")

        # 初始化策略
        strategy = DualChandelierExitStrategy({
            'atr_period': self.atr_period,
            'fixed_stop_pct': self.fixed_stop_pct,
            'breakeven_pct': self.breakeven_pct,
            'trailing_activate_pct': self.trailing_activate_pct,
            'trailing_pullback_pct': self.trailing_pullback_pct,
            'atr_threshold_pct': self.atr_threshold_pct,
            'atr_trailing_mult': self.atr_trailing_mult,
        })

        # 找到买入日当天最后一个ATR作为初始ATR
        warmup_mask = df['date'] <= entry_date_ts
        warmup_df = df[warmup_mask]
        initial_atr = warmup_df['atr'].iloc[-1] if len(warmup_df) > 0 and not warmup_df['atr'].isna().all() else None

        if initial_atr is None or np.isnan(initial_atr):
            # 用交易数据的前几根K线算
            initial_atr = trading_df['atr'].iloc[0] if not trading_df['atr'].isna().all() else entry_price * 0.02
            logger.warning(f"预热ATR不可用，使用: {initial_atr:.4f}")

        # 建仓
        strategy.on_entry(stock_code, entry_price, initial_atr, direction)
        state = strategy.positions[stock_code]

        result = TradeResult(
            stock_code=stock_code, direction=direction,
            entry_date=entry_date, entry_price=entry_price
        )

        # 逐根K线模拟
        max_profit = 0.0
        max_drawdown = 0.0
        bar_count = 0
        exited = False

        for idx, row in trading_df.iterrows():
            current_time = row['date']
            current_price = row['close']
            current_atr = row['atr']

            # 超过持仓期限
            if pd.Timestamp(current_time) > cutoff_dt:
                result.exit_reason = "TIMEOUT"
                result.exit_date = pd.Timestamp(current_time).strftime('%Y-%m-%d')
                result.exit_time = str(current_time)
                result.exit_price = current_price
                if direction == 'long':
                    result.pnl_pct = (current_price - entry_price) / entry_price * 100
                else:
                    result.pnl_pct = (entry_price - current_price) / entry_price * 100
                exited = True
                break

            # ATR为NaN时跳过
            if pd.isna(current_atr):
                result.stop_line_history.append(state.stop_line)
                result.profit_line_history.append(state.profit_line)
                result.price_history.append(current_price)
                result.time_history.append(current_time)
                bar_count += 1
                continue

            # 喂给策略
            should_exit, reason, exit_price = strategy.on_tick(
                stock_code, current_price, current_atr
            )

            # 记录通道线
            if stock_code in strategy.positions:
                s = strategy.positions[stock_code]
                result.stop_line_history.append(s.stop_line)
                result.profit_line_history.append(s.profit_line)
                result.activated_profit = s.profit_line_active
            else:
                # 已退出，用最后已知值
                result.stop_line_history.append(state.stop_line)
                result.profit_line_history.append(state.profit_line)

            result.price_history.append(current_price)
            result.time_history.append(current_time)
            bar_count += 1

            # 计算最大浮盈/浮亏
            if direction == 'long':
                pnl = (current_price - entry_price) / entry_price * 100
            else:
                pnl = (entry_price - current_price) / entry_price * 100

            max_profit = max(max_profit, pnl)
            max_drawdown = min(max_drawdown, pnl)

            if should_exit:
                result.exit_reason = reason
                result.exit_date = pd.Timestamp(current_time).strftime('%Y-%m-%d')
                result.exit_time = str(current_time)
                result.exit_price = exit_price
                if direction == 'long':
                    result.pnl_pct = (exit_price - entry_price) / entry_price * 100
                else:
                    result.pnl_pct = (entry_price - exit_price) / entry_price * 100
                exited = True
                break

        if not exited:
            # 持仓到结束
            last_price = trading_df.iloc[-1]['close']
            last_time = trading_df.iloc[-1]['date']
            result.exit_reason = "HOLD"
            result.exit_date = pd.Timestamp(last_time).strftime('%Y-%m-%d')
            result.exit_time = str(last_time)
            result.exit_price = last_price
            if direction == 'long':
                result.pnl_pct = (last_price - entry_price) / entry_price * 100
            else:
                result.pnl_pct = (entry_price - last_price) / entry_price * 100

        result.holding_bars = bar_count
        result.max_profit_pct = max_profit
        result.max_drawdown_pct = max_drawdown

        # 计算持仓天数
        if result.exit_date:
            exit_dt = pd.Timestamp(result.exit_date)
            entry_ts = pd.Timestamp(entry_date)
            result.holding_days = (exit_dt - entry_ts).days

        logger.info(f"=== 回测结果: {result.exit_reason} | "
                    f"盈亏={result.pnl_pct:+.2f}% | "
                    f"持仓{result.holding_days}天/{bar_count}根K线 | "
                    f"最大浮盈={result.max_profit_pct:+.2f}% | "
                    f"最大回撤={result.max_drawdown_pct:+.2f}%")

        return result

    def run_batch(self, trades: List[Dict]) -> List[TradeResult]:
        """
        批量回测多只股票

        Args:
            trades: [{'stock_code': 'US.AAPL', 'entry_date': '2026-03-01', 'direction': 'long'}]

        Returns:
            TradeResult 列表
        """
        results = []
        for i, trade in enumerate(trades, 1):
            logger.info(f"\n[{i}/{len(trades)}] 回测: {trade['stock_code']}")
            result = self.run(
                stock_code=trade['stock_code'],
                entry_date=trade['entry_date'],
                direction=trade.get('direction', self.direction),
                entry_price=trade.get('entry_price')
            )
            results.append(result)
        return results


def print_results(results: List[TradeResult]):
    """打印回测结果表格"""
    print("\n" + "=" * 100)
    print(f"{'股票':<12} {'方向':<6} {'买入日':<12} {'买入价':>8} "
          f"{'退出原因':<12} {'退出日':<12} {'盈亏%':>8} "
          f"{'持仓天':>6} {'最大浮盈':>8} {'最大回撤':>8}")
    print("-" * 100)

    for r in results:
        direction_str = "多" if r.direction == 'long' else "空"
        reason_map = {
            'STOP_LOSS': '止损',
            'TAKE_PROFIT': '止盈',
            'HOLD': '仍持仓',
            'TIMEOUT': '到期',
        }
        reason_str = reason_map.get(r.exit_reason, r.exit_reason)
        print(f"{r.stock_code:<12} {direction_str:<6} {r.entry_date:<12} "
              f"{r.entry_price:>8.2f} {reason_str:<12} "
              f"{r.exit_date or '-':<12} {r.pnl_pct:>+8.2f}% "
              f"{r.holding_days:>6} {r.max_profit_pct:>+8.2f}% {r.max_drawdown_pct:>+8.2f}%")

    # 汇总
    if len(results) > 1:
        pnls = [r.pnl_pct for r in results]
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        avg_pnl = np.mean(pnls)
        print("-" * 100)
        print(f"汇总: {len(results)}笔 | 胜率={win_rate:.0f}% | "
              f"平均盈亏={avg_pnl:+.2f}% | "
              f"总盈亏={sum(pnls):+.2f}%")


def plot_result(result: TradeResult, output_path: str = None):
    """
    绘制K线图 + 通道线
    """
    if len(result.price_history) == 0:
        logger.warning("无数据可绘图")
        return

    fig, ax = plt.subplots(figsize=(16, 8))

    times = result.time_history
    prices = result.price_history
    stop_lines = result.stop_line_history
    profit_lines = result.profit_line_history

    # 价格线
    ax.plot(times, prices, color='#333333', linewidth=0.8, label='价格')

    # 止损线
    ax.plot(times, stop_lines, color='red', linewidth=1.0, linestyle='--', alpha=0.7, label='止损线')

    # 止盈线（只在激活后画）
    profit_active_idx = None
    for i, pl in enumerate(profit_lines):
        if pl is not None:
            profit_active_idx = i
            break

    if profit_active_idx is not None:
        profit_times = times[profit_active_idx:]
        profit_values = [pl for pl in profit_lines[profit_active_idx:]]
        # 替换None为NaN
        profit_values = [v if v is not None else np.nan for v in profit_values]
        ax.plot(profit_times, profit_values, color='green', linewidth=1.0,
                linestyle='--', alpha=0.7, label='止盈线(trailing)')

    # 标记买入点
    ax.axhline(y=result.entry_price, color='blue', linewidth=0.5, linestyle=':', alpha=0.5, label=f'买入价 {result.entry_price:.2f}')

    # 标记退出点
    if result.exit_reason in ('STOP_LOSS', 'TAKE_PROFIT') and result.exit_time:
        exit_idx = None
        for i, t in enumerate(times):
            if str(t) == result.exit_time:
                exit_idx = i
                break
        if exit_idx is not None:
            color = 'green' if result.exit_reason == 'TAKE_PROFIT' else 'red'
            marker = '^' if result.exit_reason == 'TAKE_PROFIT' else 'v'
            ax.scatter([times[exit_idx]], [prices[exit_idx]], color=color,
                      marker=marker, s=100, zorder=5, label=f'{result.exit_reason}')

    direction_str = "做多" if result.direction == 'long' else "做空"
    reason_map = {'STOP_LOSS': '止损', 'TAKE_PROFIT': '止盈', 'HOLD': '仍持仓', 'TIMEOUT': '到期'}
    title = (f"{result.stock_code} {direction_str} | "
             f"买入={result.entry_date} @ {result.entry_price:.2f} | "
             f"{reason_map.get(result.exit_reason, result.exit_reason)} "
             f"{result.pnl_pct:+.2f}% | "
             f"持仓{result.holding_days}天")

    ax.set_title(title, fontsize=12)
    ax.set_xlabel('时间')
    ax.set_ylabel('价格')
    ax.legend(loc='best', fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
    ax.grid(True, alpha=0.3)

    plt.tight_layout()

    if output_path:
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        logger.info(f"图表已保存: {output_path}")
    else:
        import os
        default_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'backtest.png')
        plt.savefig(default_path, dpi=150, bbox_inches='tight')

    plt.close()