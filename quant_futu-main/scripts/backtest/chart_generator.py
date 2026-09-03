# -*- coding: utf-8 -*-
"""
图表生成模块
"""
import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# 显式设置中文字体（macOS用Heiti TC）
CHINESE_FONT = fm.FontProperties(family='Heiti TC')
# 设置中文字体（实际用FontProperties，rcParams作备用）
plt.rcParams['font.sans-serif'] = ['Heiti TC', 'PingFang SC', 'Arial Unicode MS', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False
import matplotlib.dates as mdates


def _get_index_data_from_db(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """从本地MySQL数据库读取指数数据，避免富途API调用"""
    try:
        from mutifactor.infra.yaml_storage import YAMLStorage
        db = YAMLStorage()
        df = db.get_kline_data(code, start_date=start_date, end_date=end_date)
        if df is not None and len(df) > 0:
            # DB返回的是 datetime.date，转为 pd.Timestamp 保持一致
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)
            return df
        return pd.DataFrame()
    except Exception as e:
        return pd.DataFrame()


class ChartGenerator:
    """图表生成器"""

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def generate_comparison_chart(self, equity_curve: pd.DataFrame, start_date: str, end_date: str,
                                  fetcher, logger):
        """
        生成策略收益与指数对比图表
        """
        try:
            logger.info("正在生成对比图表...")

            # 指数代码（只用恒生指数作为对照基准）
            indices = {
                '恒生指数': 'HK.800000',
            }

            # 获取指数数据：优先从本地MySQL数据库读取，避免富途API调用
            index_data = {}
            for name, code in indices.items():
                # 1. 先尝试从本地数据库读取
                df = _get_index_data_from_db(code, start_date, end_date)
                if df is not None and len(df) > 0:
                    df['date'] = pd.to_datetime(df['date'])
                    index_data[name] = df
                    logger.info(f"从本地DB获取{name}数据成功: {len(df)}天")
                else:
                    # 2. Fallback: 从富途API获取
                    try:
                        logger.info(f"本地DB无数据，从富途API获取 {name} ({code}) ...")
                        df = fetcher.fetch_stock_kline(code, start_date, end_date)
                        if df is not None and len(df) > 0:
                            df['date'] = pd.to_datetime(df['date'])
                            df = df.sort_values('date').reset_index(drop=True)
                            index_data[name] = df
                            logger.info(f"从API获取{name}数据成功: {len(df)}天")
                        else:
                            logger.warning(f"获取{name}数据为空")
                    except Exception as e:
                        logger.warning(f"获取{name}数据失败: {e}")

            # 创建图表（上方主图 + 下方水平三栏统计框）
            fig = plt.figure(figsize=(14, 8))
            fig.subplots_adjust(top=0.88, bottom=0.08, left=0.08, right=0.96)
            gs = fig.add_gridspec(2, 1, height_ratios=[3.0, 1], hspace=0.05)
            ax1 = fig.add_subplot(gs[0])

            # 准备策略数据
            equity_curve['date'] = pd.to_datetime(equity_curve['date'])
            initial_value = equity_curve['value'].iloc[0]
            strategy_returns = (equity_curve['value'] / initial_value - 1) * 100

            # 画策略收益
            ax1.plot(equity_curve['date'], strategy_returns, label='策略收益',
                    linewidth=2.5, color='#1E88E5', zorder=3)

            # 画指数收益（从回测开始日期对齐，只截取回测时间段内的数据）
            colors = {'恒生指数': '#E53935'}
            linestyles = {'恒生指数': '--'}
            for name, df in index_data.items():
                if len(df) > 0:
                    # 截取回测时间段内的指数数据
                    df_trimmed = df[(df['date'] >= pd.to_datetime(start_date)) &
                                    (df['date'] <= pd.to_datetime(end_date))].copy()
                    if len(df_trimmed) > 0:
                        # 以回测开始日期的收盘价为基准计算收益率
                        base_price = df_trimmed['close'].iloc[0]
                        index_returns = (df_trimmed['close'] / base_price - 1) * 100
                        ax1.plot(df_trimmed['date'], index_returns, label=name,
                                linewidth=1.8, color=colors.get(name, 'gray'),
                                alpha=0.85, linestyle=linestyles.get(name, '--'), zorder=2)

            # 设置标题和标签
            ax1.set_title('策略净值走势', fontsize=16, fontweight='bold', pad=15, fontproperties=CHINESE_FONT)
            ax1.set_xlim(pd.to_datetime(start_date), pd.to_datetime(end_date))
            ax1.margins(x=0.01)
            ax1.set_xlabel('日期', fontsize=12, fontproperties=CHINESE_FONT)
            ax1.set_ylabel('收益率 (%)', fontsize=12, fontproperties=CHINESE_FONT)
            ax1.legend(loc='upper center', fontsize=10, framealpha=0.9, edgecolor='gray',
                       prop=CHINESE_FONT,
                       bbox_to_anchor=(0.5, 1.00), ncol=1)
            ax1.grid(True, alpha=0.3, linestyle='-', zorder=1)
            ax1.axhline(y=0, color='gray', linestyle='--', alpha=0.5, linewidth=1)

            # 计算每年策略收益率（修正：考虑跨年度持仓，使用上一年最后一个交易日收盘价作为起点）
            equity_curve['year'] = equity_curve['date'].dt.year
            years = sorted(equity_curve['year'].unique())
            annual_returns = {}
            for i, year in enumerate(years):
                year_data = equity_curve[equity_curve['year'] == year]
                if len(year_data) >= 1:
                    year_end_val = year_data['value'].iloc[-1]  # 当年最后一个交易日的净值

                    if i == 0:
                        # 第一年：用当年第一天作为起点
                        year_start_val = year_data['value'].iloc[0]
                    else:
                        # 后续年份：用上一年最后一个交易日的净值作为起点（考虑跨年度持仓）
                        prev_year = years[i-1]
                        prev_year_data = equity_curve[equity_curve['year'] == prev_year]
                        year_start_val = prev_year_data['value'].iloc[-1]

                    annual_returns[year] = (year_end_val / year_start_val - 1) * 100

            # 画年度分隔线
            for year in years[1:]:
                year_start = pd.Timestamp(year=year, month=1, day=1)
                ax1.axvline(x=year_start, color='gray', linestyle=':', alpha=0.4, linewidth=0.8, zorder=1)

            # 在图表内部底部标注每年收益率（用 data 坐标定位 y 在数据最低点下方）
            y_min = min(strategy_returns.min(), *[(
                (df['close'] / df['close'].iloc[0] - 1) * 100).min()
                for df in index_data.values() if len(df) > 0
            ]) if index_data else strategy_returns.min()
            label_y = y_min - abs(y_min) * 0.12  # 在最低点下方留间距
            for year, ret in annual_returns.items():
                year_data = equity_curve[equity_curve['year'] == year]
                first_date = year_data['date'].iloc[0]
                color = '#E53935' if ret < 0 else '#2E7D32'
                ax1.text(first_date, label_y, f'{year} {ret:+.1f}%',
                         fontsize=7, fontweight='bold', color=color,
                         ha='left', va='top', clip_on=False,
                         bbox=dict(boxstyle='round,pad=0.15', facecolor='white',
                                   alpha=0.8, edgecolor='none'))

            # 添加最终收益率标注（三个标注都往下移，避免和日期重叠）
            final_strategy = strategy_returns.iloc[-1]

            # 为指数添加最终收益率标注（垂直分散，往下移避免和日期重叠）
            # offsets = {'恒生指数': (-90, -50)}
            # colors = {'恒生指数': '#E53935'}
            # for name, df in index_data.items():
            #     if len(df) > 0:
            #         final_index = (df['close'].iloc[-1] / df['close'].iloc[0] - 1) * 100
            #         ax1.annotate(f'{name}: {final_index:.1f}%',
            #                     xy=(df['date'].iloc[-1], final_index),
            #                     xytext=offsets.get(name, (-90, -15)),
            #                     textcoords='offset points',
            #                     fontsize=9, color=colors.get(name, 'gray'))

            # 设置日期格式
            ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
            ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
            plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right')

            # 计算更多统计指标
            # 策略年化波动率
            daily_returns = equity_curve['value'].pct_change().dropna()
            strategy_vol = daily_returns.std() * np.sqrt(252) * 100  # 年化波动率
            
            # 策略最大回撤
            strategy_max_dd = ((equity_curve['value'] / equity_curve['value'].cummax() - 1).min() * 100)

            # 策略年化收益率和夏普比率
            years_held = (equity_curve['date'].iloc[-1] - equity_curve['date'].iloc[0]).days / 365.25
            first_val = equity_curve['value'].iloc[0]
            last_val = equity_curve['value'].iloc[-1]
            strategy_annual = ((last_val / first_val) ** (1 / max(years_held, 0.01)) - 1) * 100
            strategy_sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(252)) if daily_returns.std() > 0 else 0

            # 共同日期变量
            first_date = pd.to_datetime(equity_curve['date'].iloc[0])
            last_date = pd.to_datetime(equity_curve['date'].iloc[-1])

            # 逐年计算指数年化收益、最大回撤、波动率、超额收益（全部用截断到回测时间段的数据）
            excess_stats = {}
            for name, df_full in index_data.items():
                if len(df_full) <= 0:
                    continue

                # 截取回测时间段内的指数数据（与作图和策略数据保持一致）
                df = df_full[(df_full['date'] >= pd.to_datetime(start_date)) &
                             (df_full['date'] <= pd.to_datetime(end_date))].copy()
                if len(df) <= 1:
                    continue

                # 指数收益率（以回测开始日收盘价为基准）
                base_idx_price = df['close'].iloc[0]
                idx_return = (df['close'].iloc[-1] / base_idx_price - 1) * 100

                # 指数年化波动率（回测期间）
                idx_daily = df['close'].pct_change().dropna()
                idx_vol = idx_daily.std() * np.sqrt(252) * 100

                # 指数最大回撤（回测期间）
                idx_cummax = df['close'].cummax()
                idx_max_dd = ((df['close'] / idx_cummax - 1).min() * 100)

                # 超额收益 = 策略收益 - 指数收益
                excess_return = final_strategy - idx_return

                # 超额回撤：策略净值相对指数的超额净值的最大回撤
                eq_merged = equity_curve[['date', 'value']].copy()
                eq_merged.columns = ['date', 'value_strat']
                idx_aligned = df[['date', 'close']].copy()
                idx_aligned.columns = ['date', 'close_idx']
                merged = pd.merge(eq_merged, idx_aligned, on='date', how='inner')
                if len(merged) > 1:
                    # 超额净值 = 策略净值 / 指数净值（相对表现）
                    strat_rel = merged['value_strat'] / merged['value_strat'].iloc[0]
                    idx_rel = merged['close_idx'] / merged['close_idx'].iloc[0]
                    excess_equity = strat_rel / idx_rel

                    # 超额最大回撤
                    excess_max_dd = ((excess_equity / excess_equity.cummax() - 1).min() * 100)

                    # 超额夏普：日超额收益 = 策略日收益 - 指数日收益
                    strat_daily_ret = merged['value_strat'].pct_change().dropna()
                    idx_daily_ret = merged['close_idx'].pct_change().dropna()
                    excess_daily_ret = strat_daily_ret.values - idx_daily_ret.values

                    # 超额年化波动率
                    excess_vol = np.std(excess_daily_ret, ddof=1) * np.sqrt(252) * 100

                    # 超额年化收益（日均超额收益 × 252）
                    excess_annual_from_daily = np.mean(excess_daily_ret) * 252 * 100

                    # 超额夏普
                    excess_sharpe = excess_annual_from_daily / excess_vol if excess_vol > 0 else 0
                    excess_annual_return = excess_annual_from_daily
                else:
                    excess_max_dd = 0
                    excess_vol = 0
                    excess_annual_return = excess_return / max(years_held, 0.01)
                    excess_sharpe = 0

                excess_stats[name] = {
                    'idx_return': idx_return,
                    'idx_vol': idx_vol,
                    'idx_max_dd': idx_max_dd,
                    'excess_return': excess_return,
                    'excess_annual_return': excess_annual_return,
                    'excess_vol': excess_vol,
                    'excess_max_dd': excess_max_dd,
                    'excess_sharpe': excess_sharpe,
                }
            
            # 水平三栏统计框
            ax2 = fig.add_subplot(gs[1])
            ax2.axis('off')

            col1_x = 0.0   # 策略
            col2_x = 0.33  # 恒生指数
            col3_x = 0.66  # 超额收益

            # 左栏：策略（蓝色标题）
            ax2.text(col1_x + 0.02, 0.45, '策略', transform=ax2.transAxes,
                    fontsize=11, fontweight='bold', color='#1565C0',
                    fontproperties=CHINESE_FONT)
            strategy_items = [
                f'总收益率: {final_strategy:+.1f}%',
                f'年化收益率: {strategy_annual:+.1f}%',
                f'年化波动率: {strategy_vol:.1f}%',
                f'最大回撤: {strategy_max_dd:.1f}%',
                f'夏普比率: {strategy_sharpe:.2f}',
            ]
            for i, item in enumerate(strategy_items):
                ax2.text(col1_x + 0.02, 0.32 - i * 0.16, item,
                        transform=ax2.transAxes, fontsize=10,
                        fontproperties=CHINESE_FONT)

            # 中栏：恒生指数（红色标题）
            if '恒生指数' in excess_stats:
                s = excess_stats['恒生指数']
                ax2.text(col2_x + 0.02, 0.45, '恒生指数', transform=ax2.transAxes,
                        fontsize=11, fontweight='bold', color='#C62828',
                        fontproperties=CHINESE_FONT)
                idx_items = [
                    f'指数收益: {s["idx_return"]:+.1f}%',
                    f'指数年化: {s["idx_return"]/max((last_date-first_date).days/365.25,0.01):+.1f}%',
                    f'指数波动率: {s["idx_vol"]:.1f}%',
                    f'指数最大回撤: {s["idx_max_dd"]:.1f}%',
                ]
                for i, item in enumerate(idx_items):
                    ax2.text(col2_x + 0.02, 0.32 - i * 0.16, item,
                            transform=ax2.transAxes, fontsize=10,
                            fontproperties=CHINESE_FONT)

            # 右栏：超额收益（绿色标题）
            if '恒生指数' in excess_stats:
                s = excess_stats['恒生指数']
                ax2.text(col3_x + 0.02, 0.45, '超额收益', transform=ax2.transAxes,
                        fontsize=11, fontweight='bold', color='#2E7D32',
                        fontproperties=CHINESE_FONT)
                excess_items = [
                    f'超额总收益: {s["excess_return"]:+.1f}%',
                    f'超额年化: {s["excess_annual_return"]:+.1f}%',
                    f'超额波动率: {s["excess_vol"]:.1f}%',
                    f'超额最大回撤: {s["excess_max_dd"]:.1f}%',
                    f'超额夏普: {s["excess_sharpe"]:.2f}',
                ]
                for i, item in enumerate(excess_items):
                    ax2.text(col3_x + 0.02, 0.32 - i * 0.16, item,
                            transform=ax2.transAxes, fontsize=10,
                            fontproperties=CHINESE_FONT)

            # 保存图表
            chart_path = os.path.join(self.output_dir, 'backtest_comparison.png')
            fig.savefig(chart_path, dpi=150, bbox_inches='tight', facecolor='white')
            plt.close()

            logger.info(f"对比图表已保存到: {chart_path}")
            print(f"\n📊 对比图表已保存到: {chart_path}")

            return chart_path

        except Exception as e:
            logger.error(f"生成对比图表失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
