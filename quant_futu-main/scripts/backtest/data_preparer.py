# -*- coding: utf-8 -*-
"""
数据预处理模块
"""
import pandas as pd


def prepare_data(data: dict, logger=None) -> dict:
    """准备数据 - 清洗和格式化"""
    strategy_data = {}

    if logger:
        logger.info(f"开始准备数据,共{len(data)}只股票")

    for i, (stock_code, df) in enumerate(data.items()):
        try:
            if 'date' not in df.columns:
                if logger and i % 10 == 0:
                    logger.debug(f"跳过 {stock_code}: 缺少date列")
                continue

            required_cols = ['date', 'open', 'high', 'low', 'close', 'volume']
            if not all(col in df.columns for col in required_cols):
                if logger and i % 10 == 0:
                    logger.debug(f"跳过 {stock_code}: 缺少必要列")
                continue

            df_copy = df.copy()
            df_copy['date'] = pd.to_datetime(df_copy['date'])
            # 转换数值列为float，避免Decimal类型导致计算错误
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col in df_copy.columns:
                    df_copy[col] = df_copy[col].astype(float)
            df_copy = df_copy.sort_values('date').reset_index(drop=True)
            df_copy = df_copy.dropna(subset=required_cols)

            if len(df_copy) < 30:
                if logger and i % 10 == 0:
                    logger.debug(f"跳过 {stock_code}: 数据不足({len(df_copy)}天)")
                continue

            strategy_data[stock_code] = df_copy

            if logger and (i + 1) % 10 == 0:
                logger.info(f"已处理 {i+1}/{len(data)} 只股票")

        except Exception as e:
            if logger:
                logger.debug(f"处理 {stock_code} 异常: {e}")
            continue

    if logger:
        logger.info(f"数据准备完成,有效股票: {len(strategy_data)}只")

    return strategy_data
