"""
日内K线技术分析器 - 薄封装版本
所有评分逻辑收敛到 mutifactor/utils/intraday_scoring.py
"""
import logging
from typing import Dict, Optional
import pandas as pd
from mutifactor.utils.intraday_scoring import analyze_score

logger = logging.getLogger(__name__)


class IntradayAnalyzer:
    """
    日内K线技术分析器

    所有评分逻辑委托给 canonical 函数 analyze_score()
    """
    def __init__(self, config: Optional[Dict] = None):
        # 读 dip_buy 段（canonical ATR 自适应评分）
        dip_cfg = (config or {}).get('dip_buy', {})
        self.strong_buy_threshold = dip_cfg.get('buy_threshold', dip_cfg.get('strong_buy_threshold', 13))
        self.watch_threshold       = dip_cfg.get('watch_threshold', 2)

    def analyze(self, stock_code: str, bars: pd.DataFrame,
                current_price: float) -> Dict:
        """
        分析一只股票的5分钟K线，返回信号评分

        Args:
            stock_code: 股票代码（仅用于日志）
            bars: 5分钟K线 DataFrame（当日日内）
            current_price: 当前价格

        Returns:
            dict: canonical 返回结构
        """
        result = analyze_score(
            df=bars,
            current_price=current_price,
            buy_threshold=self.strong_buy_threshold,
        )
        # 信号映射（与旧接口兼容）
        if result['signal'] == 'buy':
            result['signal'] = 'strong_buy'
        elif result['signal'] == 'watch':
            result['signal'] = 'watch'
        else:
            result['signal'] = 'no_buy'
        return result
