# -*- coding: utf-8 -*-
"""
回测引擎模块 - 核心回测逻辑
"""
import os
from datetime import datetime, timedelta
import pandas as pd

from mutifactor import BacktestRunner
from mutifactor.data import FutuHKDataFetcher
from mutifactor.data.base_fetcher import KLineType
from mutifactor.config import Config
from mutifactor.infra.yaml_storage import yaml_storage
from mutifactor.strategies.momentum import MomentumStrategy

try:
    from .data_preparer import prepare_data
except ImportError:
    from data_preparer import prepare_data


class BacktestEngine:
    """回测引擎"""

    def __init__(self, config: dict, logger):
        self.config = config
        self.logger = logger
        self.fetcher = None

    def run(self, stock_codes: list = None, start_date: str = None,
            end_date: str = None, strategy_name: str = 'momentum') -> dict:
        """执行回测"""

        # 设置默认值
        if end_date is None:
            end_date = self.config.get('strategy', {}).get('end_date')
            if end_date is None:
                end_date = datetime.now().strftime('%Y-%m-%d')

        if start_date is None:
            start_date = self.config.get('strategy', {}).get('start_date', '2020-01-01')

        # 计算数据获取起始日期
        start_date_obj = datetime.strptime(start_date, '%Y-%m-%d')
        data_start_date = (start_date_obj - timedelta(days=100)).strftime('%Y-%m-%d')

        self.logger.info(f"为了市场状态分析，将提前获取数据: {data_start_date} 至 {end_date}")
        self.logger.info("=" * 80)
        self.logger.info(f"策略回测 - {strategy_name.upper()}策略")
        self.logger.info(f"开始日期: {start_date}")
        self.logger.info(f"结束日期: {end_date}")
        self.logger.info(f"初始资金: {self.config.get('strategy', {}).get('initial_capital', 100000):,.2f} HKD")
        self.logger.info("=" * 80)

        # 从YAML文件获取股票代码（支持离线回测）
        if stock_codes is None:
            stock_codes = yaml_storage.get_all_stock_codes(market='HK')
            self.logger.info(f"从YAML文件获取股票代码,共{len(stock_codes)}只")

        if not stock_codes:
            self.logger.error("没有获取到股票代码")
            return None

        # 从YAML文件获取股票名称
        stock_names = {}
        for code in stock_codes:
            name = yaml_storage.get_stock_name(code)
            stock_names[code] = name if name else code

        # 获取数据（优先从数据库读取）
        price_data = self._fetch_price_data(stock_codes, data_start_date, end_date)

        if not price_data:
            self.logger.error("未能获取到任何数据")
            return None

        # 准备数据
        self.logger.info("正在准备回测数据...")
        strategy_data = prepare_data(price_data, self.logger)

        if not strategy_data:
            self.logger.error("没有有效的数据")
            return None

        # 更新股票名称（只保留有数据的）
        stock_names = {code: stock_names.get(code, code) for code in strategy_data.keys()}

        # 运行回测
        self.logger.info(f"开始运行 {strategy_name.upper()} 策略回测...")
        results = self._run_backtest(strategy_data, stock_names, start_date, end_date)

        return results

    def _fetch_price_data(self, stock_codes: list, start_date: str, end_date: str) -> dict:
        """获取价格数据 - 优先从数据库读取，缺失时连接富途补充"""
        price_data = {}
        missing_codes = []  # 记录数据库中缺失或数据不够的股票

        end_d = datetime.strptime(end_date, '%Y-%m-%d').date()
        start_d = datetime.strptime(start_date, '%Y-%m-%d').date()

        # 恒生指数代码 - 用于熊市判断
        HSI_CODE = 'HK.800000'
        
        # 确保恒生指数在股票列表中
        if HSI_CODE not in stock_codes:
            stock_codes = [HSI_CODE] + list(stock_codes)
            self.logger.info(f"已添加恒生指数 {HSI_CODE} 到股票池")

        # 获取上市日期映射，避免对未上市股票拉取无意义数据
        listing_dates = yaml_storage.get_all_listing_dates('HK')

        # 直接从富途获取所有股票数据（不缓存到YAML）
        self.logger.info(f"准备从富途获取 {len(stock_codes)} 只股票的数据...")
        # MySQL→YAML迁移：不再过滤未上市股票
        # 原因：股票上市后应该参与回测，只是在上市前没有数据
        # 数据获取层会自动处理（上市前返回空数据，策略会自动跳过）
        
        missing_codes = list(enumerate(stock_codes))
        self.logger.info(f"将尝试获取 {len(missing_codes)} 只股票的数据（包括新股）")

        # 从富途获取所有有效股票的数据
        price_data = {}
        
        try:
            with FutuHKDataFetcher(host=Config.futu.HOST, port=Config.futu.PORT) as fetcher:
                self.logger.info(f"\n正在从富途获取 {len(missing_codes)} 只股票数据...")
                
                stock_codes = [code for idx, code in missing_codes]
                price_data = fetcher.fetch_multiple_stocks(stock_codes, start_date, end_date, ktype=KLineType.DAY)
                
                # 格式化日期和数值类型
                for code, df in price_data.items():
                    df['date'] = pd.to_datetime(df['date'])
                    for col in ['open', 'high', 'low', 'close', 'volume']:
                        if col in df.columns:
                            df[col] = df[col].astype(float)
                
                self.logger.info(f"\n✅ 数据获取完成: {len(price_data)}/{len(missing_codes)} 只股票")
        
        except Exception as e:
            self.logger.error(f"❌ 连接富途失败: {e}")
            self.logger.info("请检查富途OpenD是否运行！")
            return {}
        
        return price_data

        self.logger.info(f"数据获取完成,共{len(price_data)}只股票")
        return price_data

    def _run_backtest(self, strategy_data: dict, stock_names: dict,
                     start_date: str, end_date: str) -> dict:
        """执行回测核心逻辑"""
        momentum_config = self.config.get('momentum', {})
        momentum_method = momentum_config.get('momentum_method', 'exponential')

        runner = BacktestRunner('momentum')

        # 保存原始方法
        original_init = MomentumStrategy.__init__
        original_momentum_method = MomentumStrategy.calculate_momentum_score_linear

        # 打补丁应用配置 - 所有变量必须使用闭包捕获
        _engine_config = self.config
        _momentum_config = momentum_config
        _stock_names = stock_names

        def patched_init(self, initial_capital=None, market_type=None):
            from mutifactor.data.base_fetcher import MarketType as MT
            if market_type is None:
                market_type = MT.HK
            original_init(self, initial_capital, config=_engine_config, market_type=market_type)
            # 直接设置股票名称映射（覆盖 BaseStrategy 初始化的空字典）
            self.stock_names = _stock_names

            # 应用配置参数
            for key in ['momentum_window', 'rsrs_window', 'rsrs_long_window',
                       'rsrs_rolling_window', 'strong_trend_threshold',
                       'weak_trend_threshold', 'min_momentum_score', 'min_r2',
                       'max_positions']:
                if key in _momentum_config:
                    setattr(self, key, _momentum_config[key])

        MomentumStrategy.__init__ = patched_init

        try:
            if momentum_method == 'exponential':
                self.logger.info("使用指数加权动量计算方法...")
                MomentumStrategy.calculate_momentum_score_linear = MomentumStrategy.calculate_momentum_score_exponential
                results = runner.run_backtest(strategy_data, start_date, end_date)
            else:
                self.logger.info("使用线性加权动量计算方法...")
                results = runner.run_backtest(strategy_data, start_date, end_date)
        finally:
            # 恢复原始方法
            MomentumStrategy.__init__ = original_init
            MomentumStrategy.calculate_momentum_score_linear = original_momentum_method

        if not results:
            self.logger.error("回测失败")
            return None

        results['start_date'] = start_date
        results['end_date'] = end_date
        results['stock_names'] = stock_names
        results['strategy'] = 'momentum'

        self.logger.info("\n回测完成!")
        return results
