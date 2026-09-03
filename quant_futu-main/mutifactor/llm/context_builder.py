"""
Context Builder - 系统状态快照打包
===================================

把量化系统已有的计算结果打包成紧凑文本/JSON，给 LLM 当输入。
核心价值：
  - 一份固定格式模板，每个接入点都能用
  - 压缩 K 线数据（只传摘要不传完整 DataFrame，省 token）
  - 统一 prompt 输入格式，便于对比不同接入点效果
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger('llm')


class ContextBuilder:
    """快照打包器"""

    # ========== 接入点 A: 候选股快照 ==========

    def build_candidate_snapshot(
        self,
        candidates: List[str],
        stocks_data: Dict[str, Any],
        date: str,
    ) -> str:
        """
        把候选股的动量指标打包成可读文本。

        Args:
            candidates: 候选股代码列表
            stocks_data: {code: DataFrame} 的 K 线数据
            date: 当前日期

        Returns:
            压缩后的指标文本，格式如:
                HK.01234: 动量得分=0.085 R²=0.72 RSRS斜率=0.018 ATR=0.42 价=23.50
        """
        lines = []
        for code in candidates:
            df = stocks_data.get(code)
            if df is None or len(df) < 30:
                lines.append(f"{code}: 数据不足")
                continue

            try:
                close = df['close'].iloc[-1]
                # ATR 简化计算（和 exit_strategy.py 一致）
                atr = self._calc_atr(df, period=14)
                # 动量斜率（近 25 天）
                momentum_slope = self._calc_recent_slope(df, window=25)
                # RSRS 短斜率
                rsrs_slope = self._calc_rsrs_slope(df, window=18)
                # R² 简化
                r2 = self._calc_r2(df, window=25)

                lines.append(
                    f"{code}: 动量={momentum_slope:+.4f} "
                    f"R²={r2:.2f} RSRS={rsrs_slope:+.4f} "
                    f"ATR={atr:.4f} 价={close:.2f}"
                )
            except Exception as e:
                lines.append(f"{code}: 指标计算失败 ({e})")

        return "\n".join(lines)

    # ========== 接入点 B: 买入列表快照 ==========

    def build_buy_snapshot(
        self,
        buy_list: List[str],
        holdings: Dict[str, Any] = None,
        cash: float = 0.0,
        stock_prices: Dict[str, float] = None,
    ) -> str:
        """打包买入前的市场快照"""
        parts = [f"可用资金: {cash:.2f}"]

        if holdings:
            holdings_info = []
            for code, pos in holdings.items():
                pnl_pct = pos.get('pnl_pct', 0) if isinstance(pos, dict) else 0
                holdings_info.append(f"{code}: 盈亏{pnl_pct:+.1%}")
            parts.append(f"当前持仓: {', '.join(holdings_info) if holdings_info else '无'}")

        if buy_list:
            buy_info = []
            for code in buy_list:
                price = stock_prices.get(code, 0) if stock_prices else 0
                buy_info.append(f"{code}@{price:.2f}" if price else code)
            parts.append(f"待买入: {', '.join(buy_info)}")

        return "\n".join(parts)

    # ========== 内部指标计算（压缩版，和 momentum.py 保持一致逻辑）==========

    @staticmethod
    def _calc_atr(df, period=14):
        """极简 ATR 计算"""
        import numpy as np
        if len(df) < period + 1:
            return 0.0
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values
        tr1 = high - low
        tr2 = np.abs(high - np.roll(close, 1))
        tr3 = np.abs(low - np.roll(close, 1))
        tr = np.maximum(tr1, np.maximum(tr2, tr3))
        return float(np.mean(tr[-period:]))

    @staticmethod
    def _calc_recent_slope(df, window=25):
        """近 N 天动量对数回归斜率"""
        import numpy as np
        import math
        if len(df) < window:
            return 0.0
        recent = df.tail(window)['close'].values
        prices = recent[recent > 0]
        if len(prices) < window * 0.8:
            return 0.0
        log_p = np.log(prices)
        x = np.arange(len(log_p))
        try:
            slope, _ = np.polyfit(x, log_p, 1)
            return float(math.exp(slope * 252) - 1)  # 年化
        except Exception:
            return 0.0

    @staticmethod
    def _calc_rsrs_slope(df, window=18):
        """RSRS 斜率"""
        import numpy as np
        if len(df) < window:
            return 0.0
        recent = df.tail(window)
        try:
            x = np.arange(window)
            s_high, _ = np.polyfit(x, recent['high'].values, 1)
            s_low, _ = np.polyfit(x, recent['low'].values, 1)
            return float((s_high + s_low) / 2)
        except Exception:
            return 0.0

    @staticmethod
    def _calc_r2(df, window=25):
        """R² 拟合度"""
        import numpy as np
        if len(df) < window:
            return 0.0
        recent = df.tail(window)['close'].values
        prices = recent[recent > 0]
        if len(prices) < window * 0.8:
            return 0.0
        log_p = np.log(prices)
        x = np.arange(len(log_p))
        try:
            slope, intercept = np.polyfit(x, log_p, 1)
            y_pred = slope * x + intercept
            ss_res = np.sum((log_p - y_pred) ** 2)
            ss_tot = np.sum((log_p - np.mean(log_p)) ** 2)
            return float(1 - ss_res / ss_tot) if ss_tot > 0 else 0.0
        except Exception:
            return 0.0
